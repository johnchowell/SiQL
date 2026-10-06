/* siql._speedups: C versions of siql's search primitives (see siql/helpers/search.py for the Python versions).
 *
 * Built against the stable ABI for Python 3.11+, so one compiled module works on every later version.
 */
#ifndef Py_LIMITED_API
#define Py_LIMITED_API 0x030B0000
#endif
#include <Python.h>
#include <stddef.h>
#include <stdint.h>

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
static int find_path(SkipList *self, PyObject *key, Node **path)
{
    Node *node = self->head;
    for (int lane = MAX_HEIGHT - 1; lane >= self->height; lane--) {
        path[lane] = self->head;
    }
    for (int lane = self->height - 1; lane >= 0; lane--) {
        Node *next = node->next[lane];
        while (next != NULL) {
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
    if (find_path(self, key, path) < 0) {
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

static PyObject *SkipList_insert(PyObject *op, PyObject *args)
{
    SkipList *self = (SkipList *)op;
    PyObject *key, *cell_id;
    if (!PyArg_ParseTuple(args, "OO:insert", &key, &cell_id)) {
        return NULL;
    }
    Node *path[MAX_HEIGHT];
    if (find_path(self, key, path) < 0) {
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
    if (find_path(self, key, path) < 0) {
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

/* ---- Module ---------------------------------------------------------------------------------------------------- */

static PyMethodDef module_methods[] = {
    {"scan", scan, METH_VARARGS,
     "scan(cells, op, value, value_first=False) -> indices of the cells whose value matches."},
    {"scan_match", scan_match, METH_VARARGS,
     "scan_match(cells, fullmatch, negate=False) -> indices of string cells where fullmatch(value) != negate."},
    {"values", values, METH_O, "values(cells) -> list of the cells' values."},
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
