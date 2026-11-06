/* siql._speedups: C versions of siql's search primitives (see siql/helpers/search.py for the Python versions).
 *
 * Built against the stable ABI for Python 3.11+, so one compiled module works on every later version.
 */
#ifndef Py_LIMITED_API
#define Py_LIMITED_API 0x030B0000
#endif
#include <Python.h>
#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define MAX_HEIGHT 16

enum { OP_EQ, OP_NE, OP_LT, OP_LE, OP_GT, OP_GE, OP_IS_NULL, OP_NOT_NULL };
static const int RICH_OPS[] = {Py_EQ, Py_NE, Py_LT, Py_LE, Py_GT, Py_GE};

static PyObject *str_value; /* interned "_value", the attribute Cell keeps its value in */

/* ---- SkipList -------------------------------------------------------------------------------------------------- */

typedef struct Node {
    PyObject *key; /* owned; NULL for the head */
    PyObject *ids; /* owned set of cell ids holding this key; NULL for the head */
    int height;
    struct Node *next[1]; /* `height` entries */
} Node;

typedef struct {
    PyObject_HEAD
    Node *head;
    int height;
    uint64_t rng;
} SkipList;

static Node *node_new(PyObject *key, int height)
{
    Node *node = PyMem_Malloc(offsetof(Node, next) + (size_t)height * sizeof(Node *));
    if (node == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    node->key = key;
    Py_XINCREF(key);
    node->ids = NULL;
    node->height = height;
    for (int lane = 0; lane < height; lane++) {
        node->next[lane] = NULL;
    }
    return node;
}

static void node_free(Node *node)
{
    Py_XDECREF(node->key);
    Py_XDECREF(node->ids);
    PyMem_Free(node);
}

/* Each extra lane is joined with probability 1/4 (xorshift64; not security sensitive). */
static int random_height(SkipList *self)
{
    int height = 1;
    while (height < MAX_HEIGHT) {
        self->rng ^= self->rng << 13;
        self->rng ^= self->rng >> 7;
        self->rng ^= self->rng << 17;
        if ((self->rng & 3) != 0) {
            break;
        }
        height++;
    }
    return height;
}

/* path[lane] = the last node before `key` in each lane. Returns -1 if a comparison raised. */
static int find_path(SkipList *self, PyObject *key, Node **path, PyObject *trace)
{
    Node *node = self->head;
    for (int lane = MAX_HEIGHT - 1; lane >= self->height; lane--) {
        path[lane] = self->head;
    }
    for (int lane = self->height - 1; lane >= 0; lane--) {
        Node *next = node->next[lane];
        while (next != NULL) {
            if (trace != NULL && PyDict_SetItem(trace, next->key, next->ids) < 0) return -1;
            int less = PyObject_RichCompareBool(next->key, key, Py_LT);
            if (less < 0) {
                return -1;
            }
            if (!less) {
                break;
            }
            node = next;
            next = node->next[lane];
        }
        path[lane] = node;
    }
    return 0;
}

/* The node holding `key`, or NULL (check PyErr_Occurred) */
static Node *node_at(Node **path, PyObject *key)
{
    Node *node = path[0]->next[0];
    if (node == NULL) {
        return NULL;
    }
    int equal = PyObject_RichCompareBool(node->key, key, Py_EQ);
    return equal > 0 ? node : NULL;
}

static PyObject *SkipList_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    if (PyTuple_Size(args) != 0 || (kwargs != NULL && PyDict_Size(kwargs) != 0)) {
        PyErr_SetString(PyExc_TypeError, "SkipList() takes no arguments");
        return NULL;
    }
    SkipList *self = (SkipList *)PyType_GenericAlloc(type, 0);
    if (self == NULL) {
        return NULL;
    }
    self->head = node_new(NULL, MAX_HEIGHT);
    if (self->head == NULL) {
        Py_DECREF(self);
        return NULL;
    }
    self->height = 1;
    self->rng = ((uint64_t)(uintptr_t)self * 0x9E3779B97F4A7C15ULL) | 1;
    return (PyObject *)self;
}

static void SkipList_dealloc(PyObject *op)
{
    SkipList *self = (SkipList *)op;
    Node *node = self->head;
    while (node != NULL) {
        Node *next = node->next[0];
        node_free(node);
        node = next;
    }
    PyTypeObject *type = Py_TYPE(op);
    freefunc tp_free = (freefunc)PyType_GetSlot(type, Py_tp_free);
    tp_free(op);
    Py_DECREF(type);
}

static PyObject *SkipList_get(PyObject *op, PyObject *key)
{
    SkipList *self = (SkipList *)op;
    Node *path[MAX_HEIGHT];
    if (find_path(self, key, path, NULL) < 0) {
        return NULL;
    }
    Node *node = node_at(path, key);
    if (node != NULL) {
        return Py_NewRef(node->ids);
    }
    if (PyErr_Occurred()) {
        return NULL;
    }
    return PySet_New(NULL);
}

/* A batch-local finger: ascend only far enough to span the next target, then descend.
 * Decreasing targets start at the head. No node pointers survive the method call. */
static int finger_path(SkipList *self, PyObject *key, Node **path, PyObject *trace)
{
    int top = 0;
    for (; top < self->height - 1; top++) {
        Node *next = path[top]->next[top];
        if (next == NULL) break;
        if (trace != NULL && PyDict_SetItem(trace, next->key, next->ids) < 0) return -1;
        int less = PyObject_RichCompareBool(next->key, key, Py_LT);
        if (less < 0) return -1;
        if (!less) break;
        /* Sparse queries can span most of a branch: use the saved express lanes
         * directly rather than paying for a full ascent followed by descent. */
        if (top == 2) { top = self->height - 1; break; }
    }
    Node *node = path[top];
    for (int lane = top; lane >= 0; lane--) {
        Node *saved = path[lane];
        if (node != saved && saved != self->head) {
            int ahead = node == self->head ? 1 : PyObject_RichCompareBool(node->key, saved->key, Py_LT);
            if (ahead < 0) return -1;
            if (ahead) node = saved;
        }
        Node *next = node->next[lane];
        while (next != NULL) {
            if (trace != NULL && PyDict_SetItem(trace, next->key, next->ids) < 0) return -1;
            int less = PyObject_RichCompareBool(next->key, key, Py_LT);
            if (less < 0) return -1;
            if (!less) break;
            node = next;
            next = node->next[lane];
        }
        path[lane] = node;
    }
    return 0;
}

static PyObject *SkipList_get_many(PyObject *op, PyObject *args, PyObject *kwargs)
{
    SkipList *self = (SkipList *)op;
    PyObject *input;
    int chained = 1, tracing = 0;
    static char *names[] = {"keys", "chained", "trace", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O|pp:get_many", names, &input, &chained, &tracing)) return NULL;
    PyObject *keys = PySequence_List(input);
    if (keys == NULL) return NULL;
    Py_ssize_t count = PyList_Size(keys);
    PyObject *out = PyList_New(count);
    if (out == NULL) { Py_DECREF(keys); return NULL; }
    PyObject *trace = tracing ? PyDict_New() : NULL;
    if (tracing && trace == NULL) { Py_DECREF(keys); Py_DECREF(out); return NULL; }
    Node *path[MAX_HEIGHT];
    for (int lane = 0; lane < MAX_HEIGHT; lane++) path[lane] = self->head;
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *key = PyList_GetItem(keys, i);
        int forward = 0;
        if (chained && i) {
            int backward = PyObject_RichCompareBool(key, PyList_GetItem(keys, i - 1), Py_LT);
            if (backward < 0) goto error;
            forward = !backward;
        }
        if ((forward ? finger_path(self, key, path, trace) : find_path(self, key, path, trace)) < 0) goto error;
        Node *node = node_at(path, key);
        if (PyErr_Occurred()) goto error;
        PyObject *ids = node != NULL ? Py_NewRef(node->ids) : PySet_New(NULL);
        if (ids == NULL) goto error;
        if (PyList_SetItem(out, i, ids) < 0) goto error;
    }
    Py_DECREF(keys);
    if (tracing) {
        PyObject *result = PyTuple_Pack(2, out, trace);
        Py_DECREF(out);
        Py_DECREF(trace);
        return result;
    }
    return out;
error:
    Py_DECREF(keys);
    Py_DECREF(out);
    Py_XDECREF(trace);
    return NULL;
}

static PyObject *SkipList_get_with_trace(PyObject *op, PyObject *key)
{
    SkipList *self = (SkipList *)op;
    PyObject *trace = PyDict_New();
    if (trace == NULL) return NULL;
    Node *path[MAX_HEIGHT];
    if (find_path(self, key, path, trace) < 0) { Py_DECREF(trace); return NULL; }
    Node *node = node_at(path, key);
    if (PyErr_Occurred()) { Py_DECREF(trace); return NULL; }
    PyObject *ids = node != NULL ? Py_NewRef(node->ids) : PySet_New(NULL);
    if (ids == NULL) { Py_DECREF(trace); return NULL; }
    PyObject *result = PyTuple_Pack(2, ids, trace);
    Py_DECREF(ids);
    Py_DECREF(trace);
    return result;
}

/* Id sets of the keys between two bounds (None: unbounded), in order. None when more than `limit` ids match. */
static PyObject *SkipList_between(PyObject *op, PyObject *args, PyObject *kwargs)
{
    SkipList *self = (SkipList *)op;
    PyObject *low = Py_None, *high = Py_None;
    int low_inclusive = 1, high_inclusive = 1;
    Py_ssize_t limit = -1;
    static char *names[] = {"low", "high", "low_inclusive", "high_inclusive", "limit", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|OOppn:between", names, &low, &high, &low_inclusive,
                                     &high_inclusive, &limit)) {
        return NULL;
    }
    Node *node;
    if (low == Py_None) {
        node = self->head->next[0];
    }
    else {
        Node *path[MAX_HEIGHT];
        if (find_path(self, low, path, NULL) < 0) return NULL;
        node = path[0]->next[0];
        if (node != NULL && !low_inclusive) {
            int equal = PyObject_RichCompareBool(node->key, low, Py_EQ);
            if (equal < 0) return NULL;
            if (equal) node = node->next[0];
        }
    }
    PyObject *out = PyList_New(0);
    if (out == NULL) return NULL;
    Py_ssize_t total = 0;
    for (; node != NULL; node = node->next[0]) {
        if (high != Py_None) {
            int inside = PyObject_RichCompareBool(node->key, high, high_inclusive ? Py_LE : Py_LT);
            if (inside < 0) goto error;
            if (!inside) break;
        }
        total += PySet_Size(node->ids);
        if (limit >= 0 && total > limit) {
            Py_DECREF(out);
            Py_RETURN_NONE;
        }
        if (PyList_Append(out, node->ids) < 0) goto error;
    }
    return out;
error:
    Py_DECREF(out);
    return NULL;
}

/* (key, ids) pairs in key order from a bound (None: the first or last key), until they hold `count` ids.
 * Lanes link forward only, so a descending walk repeats the predecessor search: O(log n) per key. */
static PyObject *SkipList_nearest(PyObject *op, PyObject *args, PyObject *kwargs)
{
    SkipList *self = (SkipList *)op;
    PyObject *bound = Py_None;
    Py_ssize_t count = 1;
    int descending = 0, inclusive = 1;
    static char *names[] = {"bound", "count", "descending", "inclusive", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|Onpp:nearest", names, &bound, &count, &descending,
                                     &inclusive)) {
        return NULL;
    }
    Node *path[MAX_HEIGHT];
    Node *node;
    if (bound == Py_None) {
        node = self->head;
        if (descending) {
            for (int lane = self->height - 1; lane >= 0; lane--) {
                while (node->next[lane] != NULL) node = node->next[lane];
            }
        }
        else {
            node = node->next[0];
        }
    }
    else {
        if (find_path(self, bound, path, NULL) < 0) return NULL;
        node = path[0]->next[0];
        int equal = 0;
        if (node != NULL) {
            equal = PyObject_RichCompareBool(node->key, bound, Py_EQ);
            if (equal < 0) return NULL;
        }
        if (descending) {
            node = equal && inclusive ? node : path[0];
        }
        else if (equal && !inclusive) {
            node = node->next[0];
        }
    }
    PyObject *out = PyList_New(0);
    if (out == NULL) return NULL;
    Py_ssize_t total = 0;
    while (node != NULL && node != self->head && total < count) {
        PyObject *pair = PyTuple_Pack(2, node->key, node->ids);
        if (pair == NULL || PyList_Append(out, pair) < 0) {
            Py_XDECREF(pair);
            goto error;
        }
        Py_DECREF(pair);
        total += PySet_Size(node->ids);
        if (descending) {
            if (find_path(self, node->key, path, NULL) < 0) goto error;
            node = path[0];
        }
        else {
            node = node->next[0];
        }
    }
    return out;
error:
    Py_DECREF(out);
    return NULL;
}

static PyObject *SkipList_insert(PyObject *op, PyObject *args)
{
    SkipList *self = (SkipList *)op;
    PyObject *key, *cell_id;
    if (!PyArg_ParseTuple(args, "OO:insert", &key, &cell_id)) {
        return NULL;
    }
    Node *path[MAX_HEIGHT];
    if (find_path(self, key, path, NULL) < 0) {
        return NULL;
    }
    Node *node = node_at(path, key);
    if (node != NULL) {
        if (PySet_Add(node->ids, cell_id) < 0) {
            return NULL;
        }
        Py_RETURN_NONE;
    }
    if (PyErr_Occurred()) {
        return NULL;
    }
    int height = random_height(self);
    node = node_new(key, height);
    if (node == NULL) {
        return NULL;
    }
    node->ids = PySet_New(NULL);
    if (node->ids == NULL || PySet_Add(node->ids, cell_id) < 0) {
        node_free(node);
        return NULL;
    }
    if (height > self->height) {
        self->height = height;  /* path[] above the old height already points at the head */
    }
    for (int lane = 0; lane < height; lane++) {
        node->next[lane] = path[lane]->next[lane];
        path[lane]->next[lane] = node;
    }
    Py_RETURN_NONE;
}

static PyObject *SkipList_remove(PyObject *op, PyObject *args)
{
    SkipList *self = (SkipList *)op;
    PyObject *key, *cell_id;
    if (!PyArg_ParseTuple(args, "OO:remove", &key, &cell_id)) {
        return NULL;
    }
    Node *path[MAX_HEIGHT];
    if (find_path(self, key, path, NULL) < 0) {
        return NULL;
    }
    Node *node = node_at(path, key);
    if (node == NULL) {
        if (!PyErr_Occurred()) {
            PyErr_SetObject(PyExc_KeyError, key);
        }
        return NULL;
    }
    int found = PySet_Discard(node->ids, cell_id);
    if (found < 0) {
        return NULL;
    }
    if (!found) {
        PyErr_SetObject(PyExc_KeyError, cell_id);
        return NULL;
    }
    if (PySet_Size(node->ids) == 0) {
        for (int lane = 0; lane < node->height; lane++) {
            path[lane]->next[lane] = node->next[lane];
        }
        node_free(node);
        while (self->height > 1 && self->head->next[self->height - 1] == NULL) {
            self->height--;
        }
    }
    Py_RETURN_NONE;
}

static PyObject *SkipList_lane_keys(PyObject *op, PyObject *arg)
{
    SkipList *self = (SkipList *)op;
    long lane = PyLong_AsLong(arg);
    if (lane == -1 && PyErr_Occurred()) {
        return NULL;
    }
    if (lane < 0 || lane >= MAX_HEIGHT) {
        PyErr_SetString(PyExc_IndexError, "lane out of range");
        return NULL;
    }
    PyObject *keys = PyList_New(0);
    if (keys == NULL) {
        return NULL;
    }
    for (Node *node = self->head->next[lane]; node != NULL; node = node->next[lane]) {
        if (PyList_Append(keys, node->key) < 0) {
            Py_DECREF(keys);
            return NULL;
        }
    }
    return keys;
}

/* Iterates (key, ids) pairs in order. */
static PyObject *SkipList_iter(PyObject *op)
{
    SkipList *self = (SkipList *)op;
    PyObject *pairs = PyList_New(0);
    if (pairs == NULL) {
        return NULL;
    }
    for (Node *node = self->head->next[0]; node != NULL; node = node->next[0]) {
        PyObject *pair = PyTuple_Pack(2, node->key, node->ids);
        if (pair == NULL || PyList_Append(pairs, pair) < 0) {
            Py_XDECREF(pair);
            Py_DECREF(pairs);
            return NULL;
        }
        Py_DECREF(pair);
    }
    PyObject *iterator = PyObject_GetIter(pairs);
    Py_DECREF(pairs);
    return iterator;
}

static PyObject *SkipList_height(PyObject *op, void *closure)
{
    return PyLong_FromLong(((SkipList *)op)->height);
}

static PyMethodDef SkipList_methods[] = {
    {"get", SkipList_get, METH_O, "Ids of the cells holding a value equal to `key` (an empty set if none)."},
    {"get_with_trace", SkipList_get_with_trace, METH_O, "Return IDs and all inspected hashable keys, with their IDs."},
    {"get_many", (PyCFunction)SkipList_get_many, METH_VARARGS | METH_KEYWORDS,
     "get_many(keys, chained=True, trace=False): exact lookups and optional inspected-node trace."},
    {"between", (PyCFunction)SkipList_between, METH_VARARGS | METH_KEYWORDS,
     "between(low=None, high=None, low_inclusive=True, high_inclusive=True, limit=-1): id sets in key order; "
     "None if more than `limit` ids match."},
    {"nearest", (PyCFunction)SkipList_nearest, METH_VARARGS | METH_KEYWORDS,
     "nearest(bound=None, count=1, descending=False, inclusive=True): (key, ids) pairs in key order from a "
     "bound until they hold `count` ids."},
    {"insert", SkipList_insert, METH_VARARGS, "insert(key, cell_id)"},
    {"remove", SkipList_remove, METH_VARARGS, "remove(key, cell_id); raises KeyError if it isn't there."},
    {"lane_keys", SkipList_lane_keys, METH_O, "Keys in one lane, in order."},
    {NULL, NULL, 0, NULL},
};

static PyGetSetDef SkipList_getset[] = {
    {"height", SkipList_height, NULL, "Number of lanes in use.", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyType_Slot SkipList_slots[] = {
    {Py_tp_new, SkipList_new},
    {Py_tp_dealloc, SkipList_dealloc},
    {Py_tp_iter, SkipList_iter},
    {Py_tp_methods, SkipList_methods},
    {Py_tp_getset, SkipList_getset},
    {Py_tp_doc, "Sorted keys with randomly placed express lanes (C version of siql.helpers.search.PySkipList)."},
    {0, NULL},
};

static PyType_Spec SkipList_spec = {
    "siql._speedups.SkipList", sizeof(SkipList), 0, Py_TPFLAGS_DEFAULT, SkipList_slots,
};

/* ---- Column scans ---------------------------------------------------------------------------------------------- */

static int append_index(PyObject *out, Py_ssize_t i)
{
    PyObject *index = PyLong_FromSsize_t(i);
    if (index == NULL) {
        return -1;
    }
    int result = PyList_Append(out, index);
    Py_DECREF(index);
    return result;
}

/* The value of cells[i] (new reference), or NULL with an error set */
static PyObject *cell_value(PyObject *cells, Py_ssize_t i)
{
    PyObject *cell = PyList_GetItem(cells, i);
    return cell == NULL ? NULL : PyObject_GetAttr(cell, str_value);
}

static PyObject *scan(PyObject *module, PyObject *args)
{
    PyObject *cells, *value;
    int op, value_first = 0;
    if (!PyArg_ParseTuple(args, "O!iO|p:scan", &PyList_Type, &cells, &op, &value, &value_first)) {
        return NULL;
    }
    if (op < OP_EQ || op > OP_NOT_NULL) {
        PyErr_SetString(PyExc_ValueError, "unknown operator");
        return NULL;
    }
    PyObject *out = PyList_New(0);
    if (out == NULL) {
        return NULL;
    }
    for (Py_ssize_t i = 0; i < PyList_Size(cells); i++) {  /* re-read: a comparison may change the list */
        PyObject *v = cell_value(cells, i);
        if (v == NULL) {
            goto error;
        }
        int match;
        if (op == OP_IS_NULL) {
            match = v == Py_None;
        }
        else if (op == OP_NOT_NULL) {
            match = v != Py_None;
        }
        else {
            /* RichCompare + IsTrue rather than RichCompareBool, which treats identical objects as equal (nan) */
            PyObject *result = value_first ? PyObject_RichCompare(value, v, RICH_OPS[op])
                                           : PyObject_RichCompare(v, value, RICH_OPS[op]);
            match = result == NULL ? -1 : PyObject_IsTrue(result);
            Py_XDECREF(result);
            if (match < 0) {
                if (!PyErr_ExceptionMatches(PyExc_TypeError)) {
                    Py_DECREF(v);
                    goto error;
                }
                PyErr_Clear();  /* values that can't be compared don't match */
                match = 0;
            }
        }
        Py_DECREF(v);
        if (match && append_index(out, i) < 0) {
            goto error;
        }
    }
    return out;
error:
    Py_DECREF(out);
    return NULL;
}

static PyObject *scan_match(PyObject *module, PyObject *args)
{
    PyObject *cells, *fullmatch;
    int negate = 0;
    if (!PyArg_ParseTuple(args, "O!O|p:scan_match", &PyList_Type, &cells, &fullmatch, &negate)) {
        return NULL;
    }
    PyObject *out = PyList_New(0);
    if (out == NULL) {
        return NULL;
    }
    for (Py_ssize_t i = 0; i < PyList_Size(cells); i++) {
        PyObject *v = cell_value(cells, i);
        if (v == NULL) {
            goto error;
        }
        int match = 0;
        if (PyUnicode_Check(v)) {
            PyObject *result = PyObject_CallFunctionObjArgs(fullmatch, v, NULL);
            int found = result == NULL ? -1 : PyObject_IsTrue(result);
            Py_XDECREF(result);
            if (found < 0) {
                Py_DECREF(v);
                goto error;
            }
            match = found != negate;
        }
        Py_DECREF(v);
        if (match && append_index(out, i) < 0) {
            goto error;
        }
    }
    return out;
error:
    Py_DECREF(out);
    return NULL;
}

static PyObject *values(PyObject *module, PyObject *cells)
{
    if (!PyList_Check(cells)) {
        PyErr_SetString(PyExc_TypeError, "values() expects a list of cells");
        return NULL;
    }
    Py_ssize_t n = PyList_Size(cells);
    PyObject *out = PyList_New(n);
    if (out == NULL) {
        return NULL;
    }
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *v = cell_value(cells, i);
        if (v == NULL) {
            Py_DECREF(out);
            return NULL;
        }
        PyList_SetItem(out, i, v);  /* steals v */
    }
    return out;
}

/* ---- Vector search --------------------------------------------------------------------------------------------- */

typedef struct {
    float score;
    Py_ssize_t row;
} Scored;

/* 1 if `a` ranks before `b`: the higher score first, then the lower row */
static int ranks_before(const Scored *a, const Scored *b)
{
    return a->score > b->score || (a->score == b->score && a->row < b->row);
}

static int compare_scored(const void *a, const void *b)
{
    return ranks_before((const Scored *)a, (const Scored *)b) ? -1 : ranks_before((const Scored *)b, (const Scored *)a);
}

/* Eight separate sums let the compiler use SIMD lanes without reordering one long chain of additions */
static float dot(const float *a, const float *b, Py_ssize_t dim)
{
    float sum[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    Py_ssize_t i = 0;
    for (; i + 8 <= dim; i += 8) {
        for (int j = 0; j < 8; j++) {
            sum[j] += a[i + j] * b[i + j];
        }
    }
    float total = ((sum[0] + sum[1]) + (sum[2] + sum[3])) + ((sum[4] + sum[5]) + (sum[6] + sum[7]));
    for (; i < dim; i++) {
        total += a[i] * b[i];
    }
    return total;
}

/* heap[0] is the worst kept entry. Move heap[i] down to its place. */
static void sift_down(Scored *heap, Py_ssize_t size, Py_ssize_t i)
{
    for (;;) {
        Py_ssize_t worst = i, left = 2 * i + 1, right = left + 1;
        if (left < size && ranks_before(&heap[worst], &heap[left])) worst = left;
        if (right < size && ranks_before(&heap[worst], &heap[right])) worst = right;
        if (worst == i) return;
        Scored swap = heap[i];
        heap[i] = heap[worst];
        heap[worst] = swap;
        i = worst;
    }
}

static void sift_up(Scored *heap, Py_ssize_t i)
{
    while (i > 0) {
        Py_ssize_t parent = (i - 1) / 2;
        if (!ranks_before(&heap[parent], &heap[i])) return;
        Scored swap = heap[i];
        heap[i] = heap[parent];
        heap[parent] = swap;
        i = parent;
    }
}

static PyObject *vector_top(PyObject *module, PyObject *args)
{
    PyObject *matrix_obj, *query_obj, *candidates_obj = Py_None;
    Py_ssize_t dim, k = -1;
    if (!PyArg_ParseTuple(args, "OnO|nO:vector_top", &matrix_obj, &dim, &query_obj, &k, &candidates_obj)) {
        return NULL;
    }
    Py_buffer matrix, query, candidates;
    candidates.buf = NULL;
    if (PyObject_GetBuffer(matrix_obj, &matrix, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    if (PyObject_GetBuffer(query_obj, &query, PyBUF_SIMPLE) < 0) {
        PyBuffer_Release(&matrix);
        return NULL;
    }
    PyObject *out = NULL;
    Scored *kept = NULL;
    float *q = NULL;
    if (candidates_obj != Py_None && PyObject_GetBuffer(candidates_obj, &candidates, PyBUF_SIMPLE) < 0) {
        goto done;
    }
    Py_ssize_t row_bytes = dim * (Py_ssize_t)sizeof(float);
    if (dim <= 0 || matrix.len % row_bytes != 0 || query.len != row_bytes) {
        PyErr_SetString(PyExc_ValueError, "the matrix and the query must hold rows of `dim` float32 values");
        goto done;
    }
    if (candidates.buf != NULL && candidates.len % (Py_ssize_t)sizeof(int64_t) != 0) {
        PyErr_SetString(PyExc_ValueError, "candidates must be int64 row numbers");
        goto done;
    }
    Py_ssize_t n = matrix.len / row_bytes;
    Py_ssize_t count = candidates.buf == NULL ? n : candidates.len / (Py_ssize_t)sizeof(int64_t);
    /* Copy the query, as a `bytes` object's data need not be aligned for float reads. The matrix is a bytearray,
       whose data is always aligned. */
    q = PyMem_Malloc((size_t)row_bytes);
    if (q == NULL) {
        PyErr_NoMemory();
        goto done;
    }
    memcpy(q, query.buf, (size_t)row_bytes);
    const float *rows = (const float *)matrix.buf;
    const int64_t *picked = (const int64_t *)candidates.buf;
    for (Py_ssize_t c = 0; picked != NULL && c < count; c++) {
        if (picked[c] < 0 || picked[c] >= n) {
            PyErr_Format(PyExc_IndexError, "candidate row %lld is out of range", (long long)picked[c]);
            goto done;
        }
    }
    Py_ssize_t keep = (k < 0 || k > count) ? count : k;
    kept = PyMem_Malloc((size_t)(keep > 0 ? keep : 1) * sizeof(Scored));
    if (kept == NULL) {
        PyErr_NoMemory();
        goto done;
    }
    Py_ssize_t size = 0;
    if (keep > 0) {
        Py_BEGIN_ALLOW_THREADS
        for (Py_ssize_t c = 0; c < count; c++) {
            Scored entry;
            entry.row = picked == NULL ? c : (Py_ssize_t)picked[c];
            entry.score = dot(rows + entry.row * dim, q, dim);
            if (entry.score != entry.score) {
                continue;  /* nan */
            }
            if (size < keep) {
                kept[size] = entry;
                sift_up(kept, size++);
            }
            else if (ranks_before(&entry, &kept[0])) {
                kept[0] = entry;
                sift_down(kept, size, 0);
            }
        }
        qsort(kept, (size_t)size, sizeof(Scored), compare_scored);
        Py_END_ALLOW_THREADS
    }
    out = PyList_New(size);
    if (out == NULL) {
        goto done;
    }
    for (Py_ssize_t i = 0; i < size; i++) {
        PyObject *pair = Py_BuildValue("(nd)", kept[i].row, (double)kept[i].score);
        if (pair == NULL) {
            Py_CLEAR(out);
            goto done;
        }
        PyList_SetItem(out, i, pair);  /* steals pair */
    }
done:
    PyMem_Free(kept);
    PyMem_Free(q);
    if (candidates.buf != NULL) {
        PyBuffer_Release(&candidates);
    }
    PyBuffer_Release(&query);
    PyBuffer_Release(&matrix);
    return out;
}

static PyObject *vector_normalize(PyObject *module, PyObject *args)
{
    PyObject *matrix_obj;
    Py_ssize_t dim;
    if (!PyArg_ParseTuple(args, "On:vector_normalize", &matrix_obj, &dim)) {
        return NULL;
    }
    Py_buffer matrix;
    if (PyObject_GetBuffer(matrix_obj, &matrix, PyBUF_WRITABLE) < 0) {
        return NULL;
    }
    Py_ssize_t row_bytes = dim * (Py_ssize_t)sizeof(float);
    if (dim <= 0 || matrix.len % row_bytes != 0) {
        PyBuffer_Release(&matrix);
        PyErr_SetString(PyExc_ValueError, "the buffer doesn't hold whole rows of `dim` float32 values");
        return NULL;
    }
    float *values = (float *)matrix.buf;
    Py_ssize_t n = matrix.len / row_bytes;
    Py_BEGIN_ALLOW_THREADS
    for (Py_ssize_t r = 0; r < n; r++) {
        float *row = values + r * dim;
        double sum = 0;
        for (Py_ssize_t i = 0; i < dim; i++) {
            sum += (double)row[i] * row[i];
        }
        double norm = sqrt(sum);
        if (norm > 0) {
            for (Py_ssize_t i = 0; i < dim; i++) {
                row[i] = (float)(row[i] / norm);
            }
        }
    }
    Py_END_ALLOW_THREADS
    PyBuffer_Release(&matrix);
    Py_RETURN_NONE;
}

/* ---- Module ---------------------------------------------------------------------------------------------------- */

static PyMethodDef module_methods[] = {
    {"scan", scan, METH_VARARGS,
     "scan(cells, op, value, value_first=False) -> indices of the cells whose value matches."},
    {"scan_match", scan_match, METH_VARARGS,
     "scan_match(cells, fullmatch, negate=False) -> indices of string cells where fullmatch(value) != negate."},
    {"values", values, METH_O, "values(cells) -> list of the cells' values."},
    {"vector_top", vector_top, METH_VARARGS,
     "vector_top(matrix, dim, query, k=-1, candidates=None) -> (row, score) pairs with the highest dot products."},
    {"vector_normalize", vector_normalize, METH_VARARGS,
     "vector_normalize(matrix, dim) -> None; scales each float32 row to unit length in place."},
    {NULL, NULL, 0, NULL},
};

static int module_exec(PyObject *module)
{
    if (str_value == NULL) {
        str_value = PyUnicode_InternFromString("_value");
        if (str_value == NULL) {
            return -1;
        }
    }
    PyObject *type = PyType_FromModuleAndSpec(module, &SkipList_spec, NULL);
    if (type == NULL) {
        return -1;
    }
    int result = PyModule_AddObjectRef(module, "SkipList", type);
    Py_DECREF(type);
    return result;
}

static PyModuleDef_Slot module_slots[] = {
    {Py_mod_exec, module_exec},
    {0, NULL},
};

static struct PyModuleDef module_def = {
    PyModuleDef_HEAD_INIT, "siql._speedups", "C versions of siql's search primitives.", 0, module_methods,
    module_slots, NULL, NULL, NULL,
};

PyMODINIT_FUNC PyInit__speedups(void)
{
    return PyModuleDef_Init(&module_def);
}
