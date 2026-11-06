"""Vector columns: the `Vector` column type, its cell and file forms, and the per-column matrix that
`Table.nearest` scans. The scan runs in `siql._speedups` when it was compiled (see `search.py`)."""
import base64
import sys
from array import array

from .search import values as cell_values, vector_top, vector_normalize

METRICS = ("cosine", "dot")
_BYTE_FORMATS = ("B", "b", "c")


class Vector():
    """Column type for fixed-length float32 vectors, such as embeddings: `table.addCols(vec=Vector)`.

    A cell holds its vector as `bytes`: the float32 values in native byte order, 4 bytes each.
    Assign a list or tuple of numbers, an `array('f')`, `bytes`, or any buffer (a NumPy array works).
    Read a cell back with `Vector.unpack(value)`, or `numpy.frombuffer(value, numpy.float32)`.
    The table file keeps each vector as base64 text of its little-endian float32 bytes.
    """

    @staticmethod
    def pack(value) -> bytes | None:
        """The cell form of a vector (None stays None). Raises TypeError or ValueError for a value that isn't one."""
        return pack(value)

    @staticmethod
    def unpack(value) -> array | None:
        """The float32 values of a cell as an `array('f')` (None stays None)."""
        data = pack(value)
        if data is None:
            return None
        out = array("f")
        out.frombytes(data)
        return out


def pack(value) -> bytes | None:
    """Float32 bytes, native byte order, of a vector given as numbers, a buffer, or the base64 file form."""
    if value is None:
        return None
    if type(value) is bytes:
        data = value
    elif isinstance(value, str):
        data = from_text(value)
    else:
        try:
            view = memoryview(value)
        except TypeError:
            view = None
        if view is None:
            try:
                data = array("f", value).tobytes()
            except TypeError:
                kind = type(value).__name__
                raise TypeError(f"Expected a vector (numbers or a float32 buffer), got {kind}") from None
        elif view.format in _BYTE_FORMATS:
            data = view.tobytes()  # raw float32 bytes, like `bytes`
        elif view.format.lstrip("@=<>!") == "f" and view.itemsize == 4:
            data = view.tobytes()
            if view.format[:1] in "<>!" and (view.format[0] == "<") != (sys.byteorder == "little"):
                data = _swapped(data)
        else:
            if view.ndim > 1:
                raise ValueError(f"Expected a 1-dimensional vector, got shape {view.shape}")
            data = array("f", view.tolist()).tobytes()
    if not data or len(data) % 4:
        raise ValueError(f"A vector needs a whole number of float32 values (4 bytes each), got {len(data)} bytes")
    return data


def _swapped(data: bytes) -> bytes:
    a = array("f")
    a.frombytes(data)
    a.byteswap()
    return a.tobytes()


def to_text(data: bytes) -> str:
    """File form of a vector cell: base64 of its little-endian float32 bytes."""
    if sys.byteorder != "little":
        data = _swapped(data)
    return base64.b64encode(data).decode("ascii")


def from_text(text: str) -> bytes:
    try:
        data = base64.b64decode(text, validate=True)
    except ValueError:
        raise ValueError("A vector given as text must be base64 of float32 bytes") from None
    return data if sys.byteorder == "little" else _swapped(data)


def describe(value) -> str:
    """Short text for a vector cell in printed tables."""
    if type(value) is bytes:
        return f"<vector {len(value) // 4}>"
    return str(value)


class VectorColumn():
    """One column's vectors as contiguous float32 matrices, one for each metric that was queried.

    `refresh` compares the column's current cell values with the values the matrices were built from.
    The same values keep the matrices. Rows added after them extend the matrices. Any other change
    rebuilds them on the next query. So edits, deletes and undos are seen however they were made.
    """
    __slots__ = ("values", "dim", "rows", "matrices", "_slots")

    def __init__(self):
        self.values = []  # the cell values the matrices were built from
        self.dim = None  # floats per vector
        self.rows = array("q")  # table row of each matrix row; rows without a vector are left out
        self.matrices: dict[str, bytearray] = {}  # metric -> float32 rows (unit length for cosine)
        self._slots = None  # table row -> matrix row (-1: no vector), made when a filter needs it

    def _collect(self, vals: list, start: int, dim):
        """(dim, table rows, float32 bytes) of the vectors in vals[start:]. Raises for a vector of another length."""
        rows, data = array("q"), []
        for i in range(start, len(vals)):
            v = vals[i]
            if v is None:
                continue
            if type(v) is not bytes:
                v = pack(v)
            if dim is None:
                dim = len(v) // 4
            elif len(v) != dim * 4:
                raise ValueError(f"Row {i} has a vector of {len(v) // 4} values, expected {dim}")
            rows.append(i)
            data.append(v)
        return dim, rows, data

    @staticmethod
    def _matrix(data: list, dim: int, metric: str) -> bytearray:
        matrix = bytearray(b"".join(data))
        if metric == "cosine" and matrix:
            vector_normalize(matrix, dim)
        return matrix

    def refresh(self, cells: list):
        vals = cell_values(cells)
        old = self.values
        if vals == old:
            return
        if old and len(vals) > len(old) and vals[:len(old)] == old:
            dim, rows, data = self._collect(vals, len(old), self.dim)
            for metric, matrix in self.matrices.items():
                matrix += self._matrix(data, dim, metric)
            self.rows.extend(rows)
        else:
            dim, rows, _ = self._collect(vals, 0, None)
            self.rows = rows
            self.matrices = {}
        self.values, self.dim, self._slots = vals, dim, None

    def matrix(self, metric: str) -> bytearray:
        matrix = self.matrices.get(metric)
        if matrix is None:
            _, _, data = self._collect(self.values, 0, self.dim)
            matrix = self.matrices[metric] = self._matrix(data, self.dim, metric)
        return matrix

    def candidates(self, positions) -> array:
        """Matrix rows of the given table rows that hold a vector."""
        if len(self.rows) == len(self.values):  # every row has a vector: matrix row == table row
            return array("q", positions)
        if self._slots is None:
            self._slots = array("q", [-1]) * len(self.values)
            for slot, row in enumerate(self.rows):
                self._slots[row] = slot
        slots = self._slots
        n = len(slots)
        out = array("q")
        for p in positions:
            if not 0 <= p < n:
                raise IndexError(f"Row {p} is out of range")
            if slots[p] >= 0:
                out.append(slots[p])
        return out

    def search(self, query, k, positions, metric) -> list[tuple[int, float]]:
        """(table row, score) pairs, best first. `positions` (None: every row) limits the rows checked."""
        if metric not in METRICS:
            raise ValueError(f"Unknown metric {metric!r}; expected one of {', '.join(METRICS)}")
        q = pack(query)
        if q is None:
            raise ValueError("The query vector is None")
        if self.dim is None:
            return []  # no row holds a vector yet
        if len(q) != self.dim * 4:
            raise ValueError(f"The query has {len(q) // 4} values, the column's vectors have {self.dim}")
        if metric == "cosine":
            if not any(Vector.unpack(q)):
                raise ValueError("Cosine similarity needs a query vector that isn't all zeros")
            q = bytearray(q)
            vector_normalize(q, self.dim)
        matrix = self.matrix(metric)
        found = vector_top(matrix, self.dim, q, -1 if k is None else k,
                           None if positions is None else self.candidates(positions))
        if len(self.rows) == len(self.values):
            return found
        rows = self.rows
        return [(rows[slot], score) for slot, score in found]
