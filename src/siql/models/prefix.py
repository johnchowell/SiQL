"""Interned per-column prefixes, hidden packed row signatures, and exact search inside groups."""
import struct
import bisect

from .tree import ColumnTree, tree_key
from ..helpers.search import scan, EQ


def prefix(value):
    kind = type(value)
    if kind is str:
        return ('text', value[:1])
    if kind in (int, float, bool):
        digit = tree_key(value)
        return ('number', digit) if digit is not None else None
    if kind in (bytes, bytearray):
        return ('byte', value[0] if value else -1)
    if kind is memoryview:
        try:
            if value.ndim == 1 and value.itemsize == 1 and value.format in ('B', 'b'):
                return ('byte', (value[0] & 255) if len(value) else -1)
        except ValueError:
            pass
        return None
    if value is None:
        return ('null', 0)
    return None  # Unknown equality semantics: route conservatively through wildcard groups.


class PrefixGroup:
    def __init__(self, signature, symbols):
        self.signature = signature
        self.symbols = symbols
        self.rows = set()
        self.trees = {}

    def invalidate(self):
        for tree in self.trees.values():
            tree.clear_cache()
        self.trees.clear()

    def find(self, table, criteria):
        candidates = None
        for column, value in criteria.items():
            tree = self.trees.get(column)
            if tree is None:
                tree = self.trees[column] = ColumnTree(cache=table._query_cache, observe=False)
                for row in self.rows:
                    tree.add(row, row.col(column))
            rows = tree.find(value)
            candidates = set(rows) if candidates is None else candidates.intersection(rows)
            if not candidates:
                return []
        return list(self.rows) if candidates is None else list(candidates)


class CompressedIndex:
    def __init__(self, table):
        self.table = table
        self.groups = {}
        self.row_groups = {}
        self.symbols = {}
        self.postings = []
        self.dirty = set()
        self.mutable_prefix_rows = set()
        self.schema_dirty = False
        self.last_candidates = self.last_groups = 0
        self.rebuild()

    def close(self):
        for group in self.groups.values():
            group.invalidate()
        for row in self.row_groups:
            if hasattr(row, '_compressed_signature'):
                del row._compressed_signature
        self.groups.clear()
        self.row_groups.clear()
        self.mutable_prefix_rows.clear()
        self.dirty.clear()

    def rebuild(self):
        self.close()
        self.symbols.clear()
        self.postings = [{} for _ in self.table.struct.column_names]
        self.schema_dirty = False
        for row in self.table.rows:
            self.add(row)

    def _signature(self, row):
        ids = []
        for cell in row.content[:len(self.postings)]:
            token = prefix(cell.value)
            if token is None:
                ids.append(0)
            else:
                symbol = self.symbols.get(token)
                if symbol is None:
                    symbol = self.symbols[token] = len(self.symbols) + 1
                ids.append(symbol)
        symbols = tuple(ids)
        return struct.pack('<' + 'I' * len(ids), *ids), symbols

    def add(self, row):
        if self.schema_dirty:
            return
        row._fill()
        signature, symbols = self._signature(row)
        group = self.groups.get(signature)
        if group is None:
            group = self.groups[signature] = PrefixGroup(signature, symbols)
            for i, symbol in enumerate(symbols):
                self.postings[i].setdefault(symbol, set()).add(signature)
        group.invalidate()
        group.rows.add(row)
        self.row_groups[row] = group
        row._compressed_signature = group.signature
        if any(type(cell.value) in (bytearray, memoryview) for cell in row.content):
            self.mutable_prefix_rows.add(row)
        else:
            self.mutable_prefix_rows.discard(row)

    def remove(self, row):
        group = self.row_groups.pop(row, None)
        if group is not None:
            group.invalidate()
            group.rows.discard(row)
            if not group.rows:
                del self.groups[group.signature]
                for i, symbol in enumerate(group.symbols):
                    signatures = self.postings[i][symbol]
                    signatures.remove(group.signature)
                    if not signatures:
                        del self.postings[i][symbol]
        self.mutable_prefix_rows.discard(row)
        self.dirty.discard(row)
        if hasattr(row, '_compressed_signature'):
            del row._compressed_signature

    def changed(self, row):
        if row in self.row_groups:
            self.dirty.add(row)

    def ensure(self):
        if self.schema_dirty:
            self.rebuild()
        for row in tuple(self.mutable_prefix_rows):
            signature, _ = self._signature(row)
            if signature != self.row_groups[row].signature:
                self.dirty.add(row)
        for row in tuple(self.dirty):
            self.remove(row)
            self.add(row)

    def find(self, criteria):
        self.ensure()
        routed = []
        for column, value in criteria.items():
            token = prefix(value)
            if token is None:
                continue
            position = self.table.struct.col(column)
            symbol = self.symbols.get(token, -1)
            matched = self.postings[position].get(symbol, set())
            wildcard = self.postings[position].get(0, set())
            routed.append(matched | wildcard)
        if routed:
            routed.sort(key=len)
            selected = set(routed[0])
            for matches in routed[1:]:
                selected.intersection_update(matches)
        else:
            selected = set(self.groups)
        self.last_groups = len(selected)
        self.last_candidates = sum(len(self.groups[s].rows) for s in selected)
        found = []
        for signature in selected:
            found.extend(self.groups[signature].find(self.table, criteria))
        return found

    @property
    def info(self):
        self.ensure()
        return {'rows': len(self.row_groups), 'groups': len(self.groups), 'symbols': len(self.symbols),
                'row_signature_width': 4 * len(self.postings),
                'interned_signature_bytes': sum(len(s) for s in self.groups),
                'group_column_trees': sum(len(g.trees) for g in self.groups.values()),
                'last_candidate_groups': self.last_groups, 'last_candidate_rows': self.last_candidates}


def integer_field(value):
    """Canonical full-value bytes; equal builtins share bytes, unknown equality uses a wildcard."""
    kind = type(value)
    if value is None:
        payload = b'\x00'
    elif kind is str:
        payload = b'\x01' + value.encode('utf-8', errors='surrogatepass')
    elif kind in (int, bool, float):
        try:
            numerator, denominator = value.as_integer_ratio() if kind is float else (int(value), 1)
        except (ValueError, OverflowError):
            return None
        try:
            payload = b'\x02' + f'{numerator}/{denominator}'.encode('ascii')
        except ValueError:
            return None
    elif kind in (bytes, bytearray):
        payload = b'\x03' + bytes(value)
    elif kind is memoryview:
        try:
            if value.ndim != 1 or value.itemsize != 1 or value.format not in ('B', 'b'):
                return None
            payload = b'\x03' + value.tobytes()
        except ValueError:
            return None
    else:
        return None
    # A length prefix prevents trailing NUL bytes colliding when fixed-width slots are padded.
    return len(payload).to_bytes(4, 'big') + payload


class IntegerIndex(CompressedIndex):
    """Full row bits ordered as one integer; biased seek plus exhaustive bounded backtracking."""
    def __init__(self, table):
        self.order_dirty = True
        self.keys = []
        self.ordered_groups = []
        self.widths = self.minimums = self.maximums = self.averages = []
        self.last_range = self.last_backtracked = self.last_seek = 0
        super().__init__(table)

    def _signature(self, row):
        fields = tuple(integer_field(cell.value) for cell in row.content[:len(self.postings)])
        return b''.join(field if field is not None else b'\0' * 4 for field in fields), fields

    def add(self, row):
        super().add(row)
        self.order_dirty = True

    def remove(self, row):
        super().remove(row)
        if hasattr(row, '_integer_signature'):
            del row._integer_signature
        self.order_dirty = True

    def close(self):
        for row in self.row_groups:
            if hasattr(row, '_integer_signature'):
                del row._integer_signature
        super().close()
        self.keys.clear()
        self.ordered_groups.clear()
        self.order_dirty = True

    def _pack(self, values):
        key = 0
        for value, width in zip(values, self.widths):
            key = (key << (8 * width)) | value
        return key

    def _field_code(self, field, position):
        if field is None:
            return 0
        return int.from_bytes(field.ljust(self.widths[position], b'\0'), 'big')

    def ensure(self):
        super().ensure()
        if not self.order_dirty:
            return
        groups = list(self.groups.values())
        self.widths = [max([len(field) for field in postings if field is not None] + [4])
                       for postings in self.postings]
        codes = [tuple(self._field_code(field, i) for i, field in enumerate(group.symbols)) for group in groups]
        count = len(self.row_groups)
        self.minimums, self.maximums, self.averages = [], [], []
        for i in range(len(self.widths)):
            column = [code[i] for code in codes]
            self.minimums.append(min(column, default=0))
            self.maximums.append(max(column, default=0))
            self.averages.append(sum(code[i] * len(group.rows) for code, group in zip(codes, groups)) // count
                                 if count else 0)
        ordered = sorted((self._pack(code), group) for code, group in zip(codes, groups))
        self.keys = [key for key, _ in ordered]
        self.ordered_groups = [group for _, group in ordered]
        for key, group in ordered:
            for row in group.rows:
                row._integer_signature = key
        self.order_dirty = False

    def find(self, criteria, *, seek='under'):
        if seek not in ('under', 'lower'):
            raise ValueError("seek must be 'under' or 'lower'")
        self.ensure()
        lower, upper, guess, masks, desired = [], [], [], [], []
        wildcard = set()
        impossible = False
        for i, column in enumerate(self.table.struct.column_names):
            field = integer_field(criteria[column]) if column in criteria else None
            if field is not None:
                # Unknown query bits cannot match ordinary entries, but may match custom values.
                if field not in self.postings[i]:
                    impossible = True
                wildcard.update(self.postings[i].get(None, ()))
                code = self._field_code(field, i) if len(field) <= self.widths[i] else 0
                lower.append(code)
                upper.append(code)
                guess.append(code)
                masks.append((1 << (8 * self.widths[i])) - 1)
                desired.append(code)
            else:
                lower.append(self.minimums[i])
                upper.append(self.maximums[i])
                # Mean encoded field minus a quarter of its observed range: deliberately aim low.
                guess.append(max(self.minimums[i], self.averages[i] -
                                 (self.maximums[i] - self.minimums[i]) // 4))
                masks.append(0)
                desired.append(0)
        low, high = self._pack(lower), self._pack(upper)
        self.last_seek = low if seek == 'lower' else max(low, min(high, self._pack(guess)))
        selected = set(wildcard)
        self.last_range = self.last_backtracked = 0
        if not impossible:
            begin = bisect.bisect_left(self.keys, low)
            end = bisect.bisect_right(self.keys, high)
            jump = max(begin, min(end, bisect.bisect_left(self.keys, self.last_seek)))
            mask, wanted = self._pack(masks), self._pack(desired)
            self.last_range = end - begin
            self.last_backtracked = jump - begin
            # Visit right from the estimate, then cover earlier matches that the estimate skipped.
            for index in range(jump, end):
                if self.keys[index] & mask == wanted:
                    selected.add(self.ordered_groups[index].signature)
            for index in range(jump - 1, begin - 1, -1):
                if self.keys[index] & mask == wanted:
                    selected.add(self.ordered_groups[index].signature)
        rows = [row for signature in selected for row in self.groups[signature].rows]
        self.last_groups, self.last_candidates = len(selected), len(rows)
        for column, value in criteria.items():
            cells = [row.col(column) for row in rows]
            rows = [rows[i] for i in scan(cells, EQ, value, True)]
            if not rows:
                break
        return rows

    @property
    def info(self):
        details = super().info
        details.update(mode='integer', integer_bits=sum(self.widths) * 8,
                       row_signature_width=sum(self.widths),
                       last_range_groups=self.last_range, last_backtracked_groups=self.last_backtracked,
                       last_seek_integer=self.last_seek)
        return details
