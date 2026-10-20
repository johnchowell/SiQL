"""Generate language-neutral conformance fixtures from the Python implementation, then test .NET."""
import argparse
import ast
import json
import random
import shutil
import subprocess
import tempfile
from pathlib import Path

from siql import Table, TableDiff, Interpreter, __version__
from siql.models import AddRow, DropRow, SetCell
from siql.controllers import QueryError

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'build/dotnet-parity'


def api_contract():
    """Require explicit .NET review when Python's public feature surface changes."""
    contract = {}
    for path in sorted((ROOT / 'src/siql').rglob('*.py')):
        # The standalone C++ adapter is a separate, scalar-only API. This
        # contract tracks the established Python API mirrored by .NET.
        if 'tests' in path.parts or path.name == 'native.py':
            continue
        entries = {}
        for node in ast.parse(path.read_text(encoding='utf-8')).body:
            if isinstance(node, ast.ClassDef):
                members = {}
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                            not item.name.startswith('_') or item.name.startswith('__')):
                        decorators = ','.join(ast.dump(d) for d in item.decorator_list)
                        members[item.name + ':' + decorators] = ast.dump(item.args)
                    elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        members[item.target.id] = ast.dump(item)
                entries[node.name] = members
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith('_'):
                entries[node.name] = ast.dump(node.args)
        if entries:
            contract[path.relative_to(ROOT / 'src/siql').as_posix()] = entries
    return contract


def snapshot(table):
    return {'names': list(table.struct.column_names), 'types': [t.__name__ for t in table.struct.columns],
            'rows': repr([[row.col(name).value for name in table.struct.column_names] for row in table.rows])}


def generate():
    OUT.mkdir(parents=True, exist_ok=True)
    sql = []
    with tempfile.TemporaryDirectory() as folder:
        interpreter = Interpreter(folder)
        queries = [
            'CREATE TABLE t (name str, age int, score float, active bool)',
            "INSERT INTO t VALUES ('Ann', 31, 1, TRUE), ('Bob', 25, 2.5, FALSE), ('Cy', NULL, NULL, TRUE)",
            'SELECT * FROM t', 'SELECT COUNT(*) FROM t LIMIT 2 OFFSET 1',
            "SELECT name FROM t WHERE age >= 25 AND NOT (name = 'Bob')",
            "SELECT * FROM t WHERE name NOT IN ('Ann', 'Cy') OR age IS NULL",
            "SELECT name FROM t WHERE name LIKE '%a%'", "SELECT name FROM t WHERE name NOT LIKE 'A_'",
            'SELECT * FROM t ORDER BY age DESC, name ASC LIMIT 2',
            "UPDATE t SET age = 40, score = 4.25 WHERE name = 'Ann'",
            'SELECT name FROM t WHERE 40 = age', 'ALTER TABLE t ADD note str',
            "UPDATE t SET note = 'quote '' and comma, newline' WHERE name = 'Bob'",
            'ALTER TABLE t RENAME note TO remark', 'ALTER TABLE t ALTER age TYPE float',
            'DESCRIBE t', 'ALTER TABLE t DROP remark', 'COPY TABLE t TO copied',
            'RENAME TABLE copied TO renamed', 'SHOW TABLES', 'TRUNCATE renamed', 'DROP TABLE renamed',
            'CREATE TABLE IF NOT EXISTS t (ignored int)', 'DROP TABLE IF EXISTS missing',
            "INSERT INTO t (name, age) VALUES ('😀', 1), ('İ', 2), ('ſ', 3), ('K', 4)",
            "SELECT name FROM t WHERE name LIKE '_'", "SELECT name FROM t WHERE name LIKE '__'",
            "SELECT name FROM t WHERE name LIKE 'i'", "SELECT name FROM t WHERE name LIKE 's'",
            "SELECT name FROM t WHERE name LIKE 'k'", "DELETE FROM t WHERE name = 'Bob'",
            'SELECT * FROM t ORDER BY name',
        ]
        rng = random.Random(103)
        comparisons = ['=', '!=', '<', '<=', '>', '>=']
        for _ in range(240):
            value = rng.choice(['NULL', 'TRUE', 'FALSE', str(rng.randrange(45)), "'Ann'", "'😀'"])
            column = rng.choice(['name', 'age', 'score', 'active'])
            predicate = f'{column} {rng.choice(comparisons)} {value}'
            if rng.randrange(3) == 0:
                predicate += ' AND active = TRUE'
            queries.append('SELECT name, age FROM t WHERE ' + predicate + ' ORDER BY name')
        queries += ['SELECT * FROM missing', 'SELECT missing FROM t', "INSERT INTO t VALUES ('bad', 'age', 0, TRUE)",
                    "UPDATE t SET active = 1", 'ALTER TABLE t ADD name str', 'CREATE TABLE t (a int)',
                    'SELECT * FROM t WHERE (age = 1', 'SELECT * FROM t; SELECT * FROM t',
                    "INSERT INTO t (name, name) VALUES ('a', 'b')"]
        for query in queries:
            try:
                result = interpreter.execute(query)
                sql.append({'query': query, 'columns': result.columns, 'rows': repr(result.rows),
                            'rowcount': result.rowcount, 'message': result.message})
            except QueryError:
                sql.append({'query': query, 'error': True})

    core = []
    modes = [{}, {'lazy_delete': True}, {'lazy_add': True}, {'lazy_add': True, 'lazy_delete': True},
             {'compressed_search': True, 'lazy_add': True, 'lazy_delete': True},
             {'integer_search': True, 'lazy_add': True, 'lazy_delete': True}, {'tree': False}]
    for options in modes:
        with tempfile.TemporaryDirectory() as folder:
            table = Table(file=folder + '/rows.siql', **options)
            table.addCols(name=str, age=int, data=object)
            records = []
            rng = random.Random(104)
            for step in range(400):
                if not table.rows or step % 4 == 0:
                    value = rng.choice([None, b'ab\x00\xff', [1, 'x', None], {'nested': {'a': [2, 3]}},
                                        (1, 'tuple'), {1, 2}, 2**80, 1.25, 2+3j])
                    op = AddRow(rng.randrange(len(table.rows) + 1), {'name': 'Name' + str(step % 13), 'age': step % 31, 'data': value})
                elif step % 4 == 1:
                    op = SetCell(rng.randrange(len(table.rows)), 'age', step % 31)
                elif step % 4 == 2 and len(table.rows) > 15:
                    op = DropRow(rng.randrange(len(table.rows)))
                else:
                    op = AddRow(len(table.rows), {'name': 'Other', 'age': -step, 'data': None})
                TableDiff([op]).apply(table)
                checks = []
                for criteria in [{'name': 'Name' + str(step % 13)}, {'age': step % 31},
                                 {'name': 'Other', 'age': -step}, {'data': None, 'age': step % 31}]:
                    checks.append({'criteria': repr(criteria), 'expected': table.find_where(**criteria)})
                records.append({'op': op.dumps(), 'checks': checks})
            case = {'options': options, 'steps': records, 'snapshot': snapshot(table)}
            if options.get('integer_search'):
                table._compressed_index.ensure()
                case['integer_keys'] = [str(row._integer_signature) for row in table.rows]
                case['integer_bits'] = table.integer_info['integer_bits']
            core.append(case)

    literal_values = [None, True, False, 1, 1.0, -0.0, 2**100, 1e-10, "it's a string", '😀\n\x00\ud800',
                      b"a'\"\x00\xff", [], (), [1, {'a': {'b': [1, 2]}}], {None: 'null', 1: 'one'}, {1, 2}, 1+2j]
    literals = [repr(value) for value in literal_values]
    diffs = []
    rng = random.Random(105)
    with tempfile.TemporaryDirectory() as folder:
        for case in range(100):
            old, new = Table(file=f'{folder}/{case}-a.siql'), Table(file=f'{folder}/{case}-b.siql')
            old.addCols(name=str, gone=int)
            new.addCols(name=str, email=str)
            for table in [old, new]:
                for _ in range(rng.randrange(8)):
                    table.add(name=rng.choice(['a', 'b', 'c']))
            diffs.append({'old': snapshot(old), 'new': snapshot(new), 'ops': TableDiff.between(old, new).dumps()})
    with tempfile.TemporaryDirectory() as folder:
        table = Table(file=folder + '/python.siql')
        table.addCols(value=object)
        for value in literal_values:
            table.add(value=value)
        table.compact()
        shutil.copy2(table._file, OUT / 'python.siql')
        persisted = snapshot(table)
    result = {'version': __version__, 'sql': sql, 'core': core, 'literals': literals, 'diffs': diffs, 'persisted': persisted,
              'python_tests': sorted(f'{p.name}:{node.name}' for p in (ROOT / 'src/siql/tests').glob('test_*.py')
                                     for node in ast.walk(ast.parse(p.read_text(encoding='utf-8')))
                                     if isinstance(node, ast.FunctionDef) and node.name.startswith('test_'))}
    (OUT / 'fixtures.json').write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding='utf-8')
    print(f'Generated {len(sql)} SQL cases, {sum(len(c["steps"]) for c in core)} mutation steps, '
          f'{len(diffs)} exact diff cases from Python.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generate-only', action='store_true')
    parser.add_argument('--update-api', action='store_true', help='acknowledge a reviewed Python/C# API change')
    args = parser.parse_args()
    contract = api_contract()
    contract_path = ROOT / 'dotnet/python-api.json'
    if args.update_api:
        contract_path.write_text(json.dumps(contract, indent=2) + '\n', encoding='utf-8')
        print('Updated the Python API contract; review both implementations and run parity tests.')
        return
    if contract != json.loads(contract_path.read_text(encoding='utf-8')):
        raise SystemExit('Python API changed: update the C# counterpart, feature map, and shared tests; '
                         'then acknowledge with tools/dotnet_parity.py --update-api.')
    generate()
    if not args.generate_only:
        subprocess.run(['dotnet', 'run', '--project', str(ROOT / 'dotnet/SiQL.Tests'), '-c', 'Release', '--', str(OUT)], check=True)
        loaded = Table(file=str(OUT / 'csharp.siql'))
        expected = json.loads((OUT / 'fixtures.json').read_text())['persisted']
        assert list(loaded.struct.column_names) == expected['names']
        actual = [[row.col(name).value for name in loaded.struct.column_names] for row in loaded.rows]
        assert actual == ast.literal_eval(expected['rows']), 'C# → Python file mismatch'
        print('Bidirectional Python/C# .siql compatibility passed.', flush=True)


if __name__ == '__main__':
    main()
