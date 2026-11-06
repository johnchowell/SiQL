"""Runs a `SelectQuery`: SELECTs over more than one source (JOIN, derived tables), with expressions, aggregates,
GROUP BY, HAVING, DISTINCT or subqueries.

A row of a query is a dict from (source alias, column) to value. Before it runs, a query is planned once: each
column name is resolved to such a key (or to an output name, or to a key of the query around it, for a correlated
subquery), and WHERE is split into the conditions that one table can answer alone, which run on that table's
search tree, and the rest. Joins on `a.x = b.y` use a hash table, or the right table's search tree when the left
side has few keys. A subquery's result is kept for each set of outer values it uses, so an uncorrelated subquery
runs once per statement.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass, replace
from typing import Any, Callable

from .parser import (QueryError, Expr, Column, Literal, Compare, And, In, Func, Aggregate, Subquery, InSelect, Exists,
                     Star, Source, SelectQuery, hashable, plain)

INDEX_LOOKUP = 4  # look up the right table's rows by key when it has this many times more rows than there are keys


@dataclass
class Ref(Expr):
    """A resolved column: the key of its value in a query row."""
    key: Any

    def eval(self, row):
        return row[self.key]


def walk(e):
    """`e` and the expressions in it, not going into subqueries."""
    yield e
    if isinstance(e, (Subquery, Exists)) or not is_dataclass(e):
        return
    for f in fields(e):
        v = getattr(e, f.name)
        if isinstance(v, Expr):
            yield from walk(v)
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, Expr):
                    yield from walk(x)


def rewrite(e, fn: Callable[[Expr], Expr | None]):
    """A copy of `e` with each node for which `fn` gives an expression replaced by it."""
    new = fn(e)
    if new is not None:
        return new
    if not is_dataclass(e):
        return e
    changes = {}
    for f in fields(e):
        if not f.init:
            continue
        v = getattr(e, f.name)
        if isinstance(v, Expr):
            changes[f.name] = rewrite(v, fn)
        elif isinstance(v, list) and any(isinstance(x, Expr) for x in v):
            changes[f.name] = [rewrite(x, fn) if isinstance(x, Expr) else x for x in v]
    return replace(e, **changes) if changes else e


def conjuncts(e: Expr | None) -> list[Expr]:
    if e is None:
        return []
    return [c for i in e.items for c in conjuncts(i)] if isinstance(e, And) else [e]


def has_subquery(e) -> bool:
    return any(isinstance(n, (Subquery, InSelect, Exists)) for n in walk(e))


def aggregates(e) -> list[Aggregate]:
    return [n for n in walk(e) if isinstance(n, Aggregate)]


class Scope:
    """The names a query can use: the columns of its sources, the outer query's names and, in ORDER BY and HAVING,
    its items' AS names. `outer_keys` collects the outer keys the query uses."""

    def __init__(self, sources: list[tuple[str, list[str]]], outer: Scope | None = None):
        self.sources = dict(sources)
        self.outer = outer
        self.outputs: dict[str, str] = {}
        self.outer_keys: set = set()
        self.owners: dict[str, list[str]] = {}
        for alias, columns in sources:
            for c in columns:
                self.owners.setdefault(c, []).append(alias)

    def key(self, c: Column, outputs: bool = False):
        if c.table is None:
            if outputs and c.name in self.outputs:
                return self.outputs[c.name]
            owners = self.owners.get(c.name, [])
            if len(owners) > 1:
                raise QueryError(f"Ambiguous column {c.name}: use {' or '.join(f'{a}.{c.name}' for a in owners)}")
            if owners:
                return owners[0], c.name
        elif c.table in self.sources:
            if c.name not in self.sources[c.table]:
                raise QueryError(f"No such column: {c.table}.{c.name}")
            return c.table, c.name
        if self.outer is not None:
            try:
                key = self.outer.key(c)
            except QueryError:
                pass
            else:
                self.outer_keys.add(key)
                return key
        raise QueryError(f"No such column: {c.name if c.table is None else f'{c.table}.{c.name}'}")


@dataclass
class Plan:
    scope: Scope
    sources: list[Source]
    columns: dict[str, list[str]]  # alias -> column names
    derived: dict[str, list[dict]]  # alias -> rows of a derived table
    pushed: dict[str, list[Expr]]  # alias -> WHERE conditions on that table alone (keys resolved)
    joins: dict[str, tuple[list, list[Expr], list[Expr]]]  # alias -> (equal keys, conditions on it alone, other ON)
    where: list[Expr]
    items: list[Expr]
    names: list[str]
    group: list[Expr]
    aggregates: list[Aggregate]
    having: Expr | None
    order: list[tuple[Expr, bool]]
    cache: dict = field(default_factory=dict)


class Engine:
    """Runs SelectQuery statements for an Interpreter, using its tables and its one-table WHERE search."""

    def __init__(self, interpreter):
        self.interpreter = interpreter
        self.plans: dict[int, Plan] = {}

    # Planning ----------------------------------------------------------------------------------------------------
    def plan(self, q: SelectQuery, outer: Scope | None) -> Plan:
        p = self.plans.get(id(q))
        if p is not None:
            return p
        columns, derived = {}, {}
        for src in q.sources:
            alias = src.name
            if alias in columns:
                raise QueryError(f"The name {alias} is used for two sources; give one an alias")
            if src.query is not None:
                res = self.run(src.query, {}, None)
                if len(set(res.columns)) != len(res.columns):
                    raise QueryError(f"The subquery {alias} has two columns with the same name; use AS")
                columns[alias] = list(res.columns)
                derived[alias] = [{(alias, c): v for c, v in zip(res.columns, row)} for row in res.rows]
            else:
                columns[alias] = list(self.interpreter._table(src.table).struct.column_names)
        scope = Scope(list(columns.items()), outer)

        def resolve(e, outputs=False):
            def fn(n):
                if isinstance(n, Column):
                    return Ref(scope.key(n, outputs))
                if isinstance(n, (Subquery, InSelect, Exists)):
                    return self.bind(n, scope, resolve)
                return None
            return rewrite(e, fn)

        # Items: * and alias.* become one Ref per column
        items, names = [], []
        for e, alias, text in q.items:
            if isinstance(e, Star):
                aliases = [e.table] if e.table else list(columns)
                if e.table and e.table not in columns:
                    raise QueryError(f"No such table in this query: {e.table}")
                for a in aliases:
                    for c in columns[a]:
                        items.append(Ref((a, c)))
                        names.append(c if len(scope.owners[c]) == 1 or e.table else f"{a}.{c}")
            else:
                items.append(resolve(e))
                names.append(alias or (e.name if isinstance(e, Column) else text))
        for _, alias, _ in q.items:
            if alias:
                scope.outputs[alias] = alias

        # WHERE: conditions that one table answers alone run on its search tree
        inner = {q.sources[0].name} | {s.name for s in q.sources[1:] if s.join in ("INNER", "CROSS")}
        pushed = {a: [] for a in columns}
        where = []
        for c in conjuncts(q.where):
            if aggregates(c):
                raise QueryError("Aggregates can't be used in WHERE; use HAVING")
            r = resolve(c)
            owner = self.owner(r, columns)
            if owner in inner and not has_subquery(r) and owner not in derived:
                pushed[owner].append(r)
            else:
                where.append(r)

        joins = {}
        for src in q.sources[1:]:
            eq, alone, other = [], [], []
            for c in conjuncts(src.on):
                if aggregates(c):
                    raise QueryError("Aggregates can't be used in ON")
                r = resolve(c)
                if isinstance(r, Compare) and r.op in ("=", "==") and isinstance(r.left, Ref) \
                        and isinstance(r.right, Ref):
                    sides = [r.left.key, r.right.key]
                    mine = [k for k in sides if isinstance(k, tuple) and k[0] == src.name]
                    if len(mine) == 1:
                        theirs = sides[1 - sides.index(mine[0])]
                        eq.append((theirs, mine[0][1]))
                        continue
                if self.owner(r, columns) == src.name and not has_subquery(r) and src.name not in derived:
                    alone.append(r)
                else:
                    other.append(r)
            joins[src.name] = (eq, alone, other)

        group = [resolve(g) for g in q.group]
        if any(aggregates(g) for g in group):
            raise QueryError("Aggregates can't be used in GROUP BY")
        having = resolve(q.having, True) if q.having is not None else None
        order = [(resolve(e, True), d) for e, d in q.order]
        if q.similar:
            metric, column, vector, descending = q.similar
            order = [(Func(metric.upper(), [resolve(column), Literal(vector)]), descending)]
        aggs = [a for e in items + [having] + [e for e, _ in order] if e is not None for a in aggregates(e)]
        for a in aggs:
            if a.arg is not None and aggregates(a.arg):
                raise QueryError("An aggregate can't be inside another aggregate")
        p = Plan(scope, q.sources, columns, derived, pushed, joins, where, items, names, group, aggs, having, order)
        self.plans[id(q)] = p
        return p

    @staticmethod
    def owner(e: Expr, columns: dict) -> str | None:
        """The one source whose columns `e` uses (outer columns aside), or None."""
        aliases = {n.key[0] for n in walk(e) if isinstance(n, Ref) and isinstance(n.key, tuple)
                   and n.key[0] in columns}
        return aliases.pop() if len(aliases) == 1 else None

    def bind(self, n, scope: Scope, resolve) -> Expr:
        """A subquery node with `run` set; the subquery is planned now, so its outer keys are known."""
        p = self.plan(n.query, scope)
        q = n.query

        def result(env):
            key = (id(q),) + tuple(hashable(env.get(k)) for k in sorted(p.scope.outer_keys, key=repr))
            if key not in p.cache:
                p.cache[key] = self.run(q, env, scope)
            return p.cache[key]

        if isinstance(n, Exists):
            return replace(n, run=lambda env: bool(result(env).rows))

        def values(env):
            res = result(env)
            if len(res.columns) != 1:
                raise QueryError(f"A subquery used as a value must give one column, not {len(res.columns)}")
            return res

        if isinstance(n, Subquery):
            def scalar(env):
                res = values(env)
                if len(res.rows) > 1:
                    raise QueryError(f"A subquery used as a value gave {len(res.rows)} rows; use LIMIT 1 or IN")
                return res.rows[0][0] if res.rows else None
            return replace(n, run=scalar)
        sets = {}

        def members(env):
            res = values(env)
            s = sets.get(id(res))
            if s is None:
                s = sets[id(res)] = ({hashable(r[0]) for r in res.rows}, res)
            return s[0]
        return replace(n, operand=resolve(n.operand), run=members)

    # Running -------------------------------------------------------------------------------------------------------
    def run(self, q: SelectQuery, outer: dict, outer_scope: Scope | None):
        from .interpreter import Result, _plural
        p = self.plan(q, outer_scope)
        first = p.sources[0]
        rows = self.rows(p, first, p.pushed[first.name], outer)
        rows = [{**outer, **r} for r in rows] if outer else rows
        for src in p.sources[1:]:
            rows = self.join(p, rows, src, outer)
        for c in p.where:
            rows = [r for r in rows if c.eval(r)]

        if p.group or p.aggregates:
            groups: dict[tuple, list[dict]] = {}
            for r in rows:
                groups.setdefault(tuple(hashable(g.eval(r)) for g in p.group), []).append(r)
            if not groups and not p.group:
                groups[()] = []
            envs = []
            empty = {**outer, **{(a, c): None for a, cs in p.columns.items() for c in cs}}
            for members in groups.values():
                env = dict(members[0]) if members else dict(empty)
                for a in p.aggregates:
                    env[a.key] = a.compute(members)
                envs.append(env)
        else:
            envs = rows

        out = []
        outputs = p.scope.outputs
        for env in envs:
            values = [e.eval(env) for e in p.items]
            if outputs:
                env = {**env, **{n: v for n, v in zip(p.names, values) if n in outputs}}
            if p.having is None or p.having.eval(env):
                out.append((values, env))

        for e, descending in reversed(p.order):
            keyed = [(e.eval(env), values, env) for values, env in out]
            nulls = [(v, e) for k, v, e in keyed if k is None]
            rest = [(k, v, e) for k, v, e in keyed if k is not None]
            try:
                rest.sort(key=lambda x: x[0], reverse=descending)
            except TypeError:
                raise QueryError("Can't order by a value that holds values of different types") from None
            rest = [(v, e) for _, v, e in rest]
            out = rest + nulls if descending else nulls + rest

        result = [v for v, _ in out]
        if q.distinct:
            seen, unique = set(), []
            for v in result:
                k = hashable(v)
                if k not in seen:
                    seen.add(k)
                    unique.append(v)
            result = unique
        result = result[q.offset:None if q.limit is None else q.offset + q.limit]
        return Result(list(p.names), result, len(result), _plural(len(result), "row"))

    def rows(self, p: Plan, src: Source, conditions: list[Expr], outer: dict, positions=None) -> list[dict]:
        """The rows of a source that meet `conditions` (resolved conditions on it alone)."""
        alias = src.name
        if alias in p.derived:
            rows = p.derived[alias]
            env = outer or {}
            return [r for r in rows if all(c.eval({**env, **r}) for c in conditions)]
        it = self.interpreter
        t = it._table(src.table)
        if conditions:
            # The table's own WHERE search: its columns by name, outer columns as values
            local = [rewrite(c, lambda n: (Column(n.key[1]) if isinstance(n.key, tuple) and n.key[0] == alias
                                           else Literal(outer[n.key])) if isinstance(n, Ref) else None)
                     for c in conditions]
            fast = [c for c in local if plain(c)]
            slow = [c for c in local if not plain(c)]
            found = it._matching(t, fast[0] if len(fast) == 1 else And(fast)) if fast else None
            if positions is not None:
                found = positions if found is None else sorted(set(found) & set(positions))
            positions = found
        else:
            slow = []
        names = t.struct.column_names
        if positions is None:
            positions = range(len(t.rows))
        rows = []
        for i in positions:
            row = t.rows[i]
            rec = {n: row.col(j).value for j, n in enumerate(names)}
            if all(c.eval(rec) for c in slow):
                rows.append({(alias, n): v for n, v in rec.items()})
        return rows

    def join(self, p: Plan, left: list[dict], src: Source, outer: dict) -> list[dict]:
        eq, alone, other = p.joins[src.name]
        conditions = alone + (p.pushed[src.name] if src.join in ("INNER", "CROSS") else [])
        alias = src.name
        positions = None
        if eq and src.query is None:
            t = self.interpreter._table(src.table)
            theirs, mine = eq[0]
            keys = {r.get(theirs) for r in left} - {None} if all(
                isinstance(r.get(theirs), (str, int, float, bool, type(None))) for r in left) else None
            if keys is not None and len(keys) * INDEX_LOOKUP < len(t.rows):
                positions = sorted({i for k in keys for i in t.find(mine, k)})
        right = self.rows(p, src, conditions, outer, positions)
        buckets: dict[tuple, list[dict]] | None = None
        if eq:
            buckets = {}
            for r in right:
                k = tuple(hashable(r[(alias, c)]) for _, c in eq)
                if None not in k:
                    buckets.setdefault(k, []).append(r)
        nulls = {(alias, c): None for c in p.columns[alias]}
        out = []
        for lr in left:
            if buckets is None:
                candidates = right
            else:
                k = tuple(hashable(lr.get(theirs)) for theirs, _ in eq)
                candidates = [] if None in k else buckets.get(k, [])
            matched = False
            for rr in candidates:
                m = {**lr, **rr}
                if all(c.eval(m) for c in other):
                    out.append(m)
                    matched = True
            if not matched and src.join == "LEFT":
                out.append({**lr, **nulls})
        return out


def materialize(e: Expr, engine: Engine) -> Expr:
    """`e` for UPDATE or DELETE: each subquery is run once (it can't use the table's columns) and becomes values."""
    def fn(n):
        if isinstance(n, (Subquery, InSelect, Exists)):
            res = engine.run(n.query, {}, None)
            if isinstance(n, Exists):
                return Literal(bool(res.rows))
            if len(res.columns) != 1:
                raise QueryError(f"A subquery used as a value must give one column, not {len(res.columns)}")
            if isinstance(n, Subquery):
                if len(res.rows) > 1:
                    raise QueryError(f"A subquery used as a value gave {len(res.rows)} rows; use LIMIT 1 or IN")
                return Literal(res.rows[0][0] if res.rows else None)
            return In(rewrite(n.operand, fn), [Literal(r[0]) for r in res.rows], n.negate)
        return None
    return rewrite(e, fn)
