"""Interned per-column prefixes, hidden packed row signatures, and exact search inside groups."""
import struct

from .tree import ColumnTree, tree_key


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
