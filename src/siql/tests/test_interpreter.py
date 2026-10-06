"""Tests for the Database manager, the query parser/interpreter and the siql shell."""
import contextlib
import io
import os
import tempfile
import unittest

from ..managers import Database
from ..models import Table
from ..controllers import Interpreter, QueryError, Result, parse
from ..controllers.shell import main as shell_main


class InTempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()


class DatabaseTests(InTempDir):
    def test_create_open_list_and_drop(self):
        db = Database(self.dir)
        t = db.create("people", name=str, age=int)
        t.add(name="Ann", age=3)
        self.assertIs(db.table("people"), t)
        self.assertEqual(db.tables(), ["people"])
        self.assertIn("people", db)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "people.siql")))

        reopened = Database(self.dir).table("people")
        self.assertEqual([[c.value for c in r.content] for r in reopened.rows], [["Ann", 3]])

        db.drop("people")
        self.assertEqual(db.tables(), [])
        self.assertNotIn("people", db)
        with self.assertRaises(KeyError):
            db.table("people")
        with self.assertRaises(KeyError):
            db.drop("people")

    def test_existing_table_and_invalid_names(self):
        db = Database(self.dir)
        db.create("t")
        with self.assertRaises(ValueError):
            db.create("t")
        for bad in ["../evil", "a/b", "a.b", "", "1abc", "with space", None]:
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    db.table(bad)
                self.assertNotIn(bad, db)
        self.assertEqual(os.listdir(os.path.dirname(self.dir)).count("evil.siql"), 0)

    def test_ignores_other_files(self):
        open(os.path.join(self.dir, "notes.txt"), "w").close()
        open(os.path.join(self.dir, "bad name.siql"), "w").close()
        db = Database(self.dir)
        db.create("ok")
        self.assertEqual(db.tables(), ["ok"])

    def test_create_makes_missing_folder(self):
        db = Database(os.path.join(self.dir, "nested", "data"))
        db.create("t", x=int)
        self.assertEqual(db.tables(), ["t"])


class ParserTests(unittest.TestCase):
    def test_literals_and_escapes(self):
        (s,) = parse("INSERT INTO t VALUES ('it''s', \"say \"\"hi\"\"\", -3, 2.5, 1e3, TRUE, false, NULL, none)")
        self.assertEqual(s.rows, [["it's", 'say "hi"', -3, 2.5, 1000.0, True, False, None, None]])

    def test_keywords_are_case_insensitive_and_comments_are_ignored(self):
        (s,) = parse("select name from people -- trailing comment\n where AGE >= 3 order by name desc limit 2 offset 1")
        self.assertEqual((s.table, s.columns, s.order, s.limit, s.offset), ("people", ["name"], [("name", True)], 2, 1))

    def test_backtick_names(self):
        (s,) = parse("SELECT `order`, `two words` FROM t")
        self.assertEqual(s.columns, ["order", "two words"])

    def test_scripts(self):
        self.assertEqual(len(parse("SHOW TABLES; ; DESCRIBE t;")), 2)
        self.assertEqual(parse(""), [])

    def test_syntax_errors(self):
        for query in ["SELEC * FROM t", "SELECT FROM t", "SELECT * FROM", "SELECT * FROM t WHERE", "INSERT INTO t VALUES",
                      "CREATE TABLE t (a blob)", "CREATE TABLE t (a int, a str)", "SELECT * FROM t LIMIT 1.5",
                      "SELECT * FROM t WHERE a NOT = 1", "SELECT * FROM t WHERE name = 'open", "SELECT * FROM t ? x",
                      "SHOW TABLES DESCRIBE t", "SELECT"]:
            with self.subTest(query=query):
                with self.assertRaises(QueryError):
                    parse(query)


class InterpreterTests(InTempDir):
    def setUp(self):
        super().setUp()
        self.db = Interpreter(self.dir)
        self.db.executescript("""
            CREATE TABLE people (name str, age int, score float);
            INSERT INTO people VALUES ('Ann', 31, 1.5), ('Bob', 25, NULL), ('Cy', 40, 3), ('dee', 25, 2.0);
        """)

    def rows(self, query):
        return self.db.execute(query).rows

    def test_select(self):
        self.assertEqual(self.rows("SELECT name FROM people WHERE age = 25"), [["Bob"], ["dee"]])
        self.assertEqual(self.rows("SELECT * FROM people WHERE name == 'Cy'"), [["Cy", 40, 3.0]])
        result = self.db.execute("SELECT age, name FROM people LIMIT 1")
        self.assertEqual((result.columns, result.rows, result.rowcount), (["age", "name"], [[31, "Ann"]], 1))

    def test_conditions(self):
        cases = {
            "age > 25 AND age < 40": ["Ann"],
            "age = 40 OR name = 'Bob'": ["Bob", "Cy"],
            "NOT (age = 25)": ["Ann", "Cy"],
            "age = 25 AND (score IS NULL OR score > 1)": ["Bob", "dee"],
            "score IS NULL": ["Bob"],
            "score IS NOT NULL": ["Ann", "Cy", "dee"],
            "name IN ('Ann', 'Cy', 'nobody')": ["Ann", "Cy"],
            "name NOT IN ('Ann')": ["Bob", "Cy", "dee"],
            "name LIKE 'd%'": ["dee"],
            "name LIKE '_Y'": ["Cy"],
            "name NOT LIKE '%e%'": ["Ann", "Bob", "Cy"],
            "score > 1": ["Ann", "Cy", "dee"],  # NULL never compares as greater
            "name <> 'Ann' AND age != 25": ["Cy"],
            "1 = 1": ["Ann", "Bob", "Cy", "dee"],
            "age = score": [],
        }
        for where, expected in cases.items():
            with self.subTest(where=where):
                self.assertEqual(sorted(r[0] for r in self.rows(f"SELECT name FROM people WHERE {where}")), expected)

    def test_order_limit_offset_count(self):
        self.assertEqual(self.rows("SELECT name FROM people ORDER BY age DESC, name"),
                         [["Cy"], ["Ann"], ["Bob"], ["dee"]])
        self.assertEqual(self.rows("SELECT name FROM people ORDER BY score"),
                         [["Bob"], ["Ann"], ["dee"], ["Cy"]])
        self.assertEqual(self.rows("SELECT name FROM people ORDER BY age, name DESC LIMIT 2 OFFSET 1"),
                         [["Bob"], ["Ann"]])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM people WHERE age = 25"), [[2]])
        self.assertEqual(self.rows("SELECT name FROM people LIMIT 0"), [])

    def test_update_and_delete(self):
        result = self.db.execute("UPDATE people SET score = 10, name = 'Bobby' WHERE name = 'Bob'")
        self.assertEqual((result.rowcount, result.message), (1, "Updated 1 row"))
        self.assertEqual(self.rows("SELECT name, score FROM people WHERE age = 25"), [["Bobby", 10.0], ["dee", 2.0]])
        self.assertEqual(self.db.execute("DELETE FROM people WHERE age = 25").rowcount, 2)
        self.assertEqual(self.rows("SELECT name FROM people"), [["Ann"], ["Cy"]])
        self.assertEqual(self.db.execute("DELETE FROM people").message, "Deleted 2 rows")
        self.assertEqual(self.rows("SELECT * FROM people"), [])

    def test_changes_are_saved(self):
        self.db.executescript("UPDATE people SET age = 26 WHERE name = 'Bob'; DELETE FROM people WHERE name = 'Cy';"
                              "ALTER TABLE people ADD email str; UPDATE people SET email = 'a@x' WHERE name = 'Ann'")
        expected = self.rows("SELECT * FROM people")
        self.assertEqual(Interpreter(self.dir).execute("SELECT * FROM people").rows, expected)
        self.assertEqual([[c.value for c in r.content] for r in Table(file=os.path.join(self.dir, "people.siql")).rows],
                         expected)

    def test_schema_statements(self):
        self.db.execute("ALTER TABLE people ADD COLUMN email str")
        self.db.execute("ALTER TABLE people DROP score")
        self.db.execute("ALTER TABLE people ALTER COLUMN age TYPE float")
        self.assertEqual(self.rows("DESCRIBE people"), [["name", "str"], ["age", "float"], ["email", "str"]])
        self.db.execute("CREATE TABLE IF NOT EXISTS people (x int)")
        self.db.execute("CREATE TABLE other (x integer)")
        self.assertEqual(self.rows("SHOW TABLES"), [["other"], ["people"]])
        self.db.execute("DROP TABLE other")
        self.db.execute("DROP TABLE IF EXISTS other")
        self.assertEqual(self.rows("SHOW TABLES"), [["people"]])
        with self.assertRaises(QueryError):
            self.db.execute("ALTER TABLE people ALTER name int")  # existing values are strings

    def test_types_are_checked(self):
        self.db.execute("INSERT INTO people (name, score) VALUES ('Eve', 7)")
        self.assertEqual(self.rows("SELECT age, score FROM people WHERE name = 'Eve'"), [[None, 7.0]])
        for query in ["INSERT INTO people VALUES ('x', 'old', 1)", "INSERT INTO people (age) VALUES (1.5)",
                      "INSERT INTO people (age) VALUES (TRUE)", "UPDATE people SET name = 5"]:
            with self.subTest(query=query):
                with self.assertRaises(QueryError):
                    self.db.execute(query)

    def test_failed_statements_change_nothing(self):
        before = self.rows("SELECT * FROM people")
        with self.assertRaises(QueryError):
            self.db.execute("INSERT INTO people VALUES ('ok', 1, 1), ('bad', 'x', 1)")
        self.assertEqual(self.rows("SELECT * FROM people"), before)
        self.assertEqual(Interpreter(self.dir).execute("SELECT * FROM people").rows, before)

    def test_errors(self):
        for query in ["SELECT * FROM missing", "SELECT nope FROM people", "SELECT * FROM people WHERE nope = 1",
                      "SELECT * FROM people ORDER BY nope", "INSERT INTO people (nope) VALUES (1)",
                      "INSERT INTO people VALUES ('a', 1)", "INSERT INTO people (age, age) VALUES (1, 2)",
                      "CREATE TABLE people (x int)", "DROP TABLE missing", "ALTER TABLE people ADD name str",
                      "ALTER TABLE people DROP nope", "SELECT * FROM `../people`", "CREATE TABLE `a.b` (x int)",
                      "SHOW TABLES; SHOW TABLES"]:
            with self.subTest(query=query):
                with self.assertRaises(QueryError):
                    self.db.execute(query)

    def test_order_by_mixed_types_is_an_error(self):
        self.db.executescript("CREATE TABLE any_t (v any); INSERT INTO any_t VALUES (1), ('a')")
        with self.assertRaises(QueryError):
            self.db.execute("SELECT * FROM any_t ORDER BY v")

    def test_works_with_tables_made_by_the_library(self):
        db = Database(self.dir)
        db.create("lib", x=int).add(x=5)
        self.assertEqual(Interpreter(db).execute("SELECT x FROM lib").rows, [[5]])

    def test_result(self):
        result = self.db.execute("SELECT name FROM people WHERE age = 40")
        self.assertEqual(list(result), [["Cy"]])
        self.assertEqual(result.to_dict(), {"columns": ["name"], "rows": [["Cy"]], "rowcount": 1, "message": "1 row"})
        self.assertIn("│ Cy   │", str(result))
        self.assertEqual(str(Result(message="done")), "done")


class ShellTests(InTempDir):
    def run_shell(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = shell_main(["-d", self.dir, *args])
        return code, out.getvalue(), err.getvalue()

    def test_query_argument(self):
        code, out, _ = self.run_shell("CREATE TABLE t (x int); INSERT INTO t VALUES (1), (2); SELECT * FROM t")
        self.assertEqual(code, 0)
        self.assertIn("Inserted 2 rows", out)
        self.assertIn("│ 2 │", out)

    def test_unquoted_words_are_joined(self):
        self.assertEqual(self.run_shell("CREATE", "TABLE", "t", "(x", "int)")[0], 0)
        self.assertEqual(Database(self.dir).tables(), ["t"])

    def test_script_file(self):
        script = os.path.join(self.dir, "setup.sql")
        with open(script, "w", encoding="utf-8") as f:
            f.write("CREATE TABLE t (x int);\nINSERT INTO t VALUES (5);\n")
        code, out, _ = self.run_shell("-f", script, "SELECT COUNT(*) FROM t")
        self.assertEqual(code, 0)
        self.assertIn("│ 1     │", out)
        self.assertEqual(self.run_shell("-f", os.path.join(self.dir, "missing.sql"))[0], 1)

    def test_error_exit_code(self):
        code, out, err = self.run_shell("SHOW TABLES; SELECT * FROM missing")
        self.assertEqual(code, 1)
        self.assertIn("table", out)  # earlier statements still ran
        self.assertIn("No such table: missing", err)


class ManagementTests(InTempDir):
    def setUp(self):
        super().setUp()
        self.data = os.path.join(self.dir, "data")
        self.db = Interpreter(self.data)
        self.db.executescript("CREATE TABLE people (name str, age int); INSERT INTO people VALUES ('Ann', 31), ('Bob', 25)")

    def rows(self, query):
        return self.db.execute(query).rows

    def test_rename_and_copy_table(self):
        self.db.execute("RENAME TABLE people TO staff")
        self.db.execute("COPY TABLE staff TO backup")
        self.db.execute("INSERT INTO staff VALUES ('Cy', 40)")
        self.assertEqual(self.rows("SHOW TABLES"), [["backup"], ["staff"]])
        self.assertEqual(len(self.rows("SELECT * FROM staff")), 3)
        self.assertEqual(len(self.rows("SELECT * FROM backup")), 2)
        self.assertEqual(len(Interpreter(self.data).execute("SELECT * FROM staff").rows), 3)
        for query in ["RENAME TABLE missing TO x", "RENAME TABLE staff TO backup", "COPY TABLE staff TO `../x`"]:
            with self.subTest(query=query):
                with self.assertRaises(QueryError):
                    self.db.execute(query)

    def test_rename_column(self):
        self.db.execute("ALTER TABLE people RENAME COLUMN age TO years")
        self.assertEqual(self.rows("SELECT years FROM people WHERE name = 'Ann'"), [[31]])
        self.assertEqual(Interpreter(self.data).execute("DESCRIBE people").rows, [["name", "str"], ["years", "int"]])
        with self.assertRaises(QueryError):
            self.db.execute("ALTER TABLE people RENAME name TO years")
        with self.assertRaises(QueryError):
            self.db.execute("ALTER TABLE people RENAME nope TO x")

    def test_truncate(self):
        self.assertEqual(self.db.execute("TRUNCATE TABLE people").rowcount, 2)
        self.assertEqual(self.rows("SELECT * FROM people"), [])
        self.assertEqual(self.rows("DESCRIBE people"), [["name", "str"], ["age", "int"]])

    def test_compact(self):
        self.db.executescript("UPDATE people SET age = 1; UPDATE people SET age = 2; DELETE FROM people WHERE name = 'Bob'")
        expected = self.rows("SELECT * FROM people")
        (name, before, after), = self.rows("COMPACT TABLE people")
        self.assertEqual(name, "people")
        self.assertLess(after, before)
        self.assertEqual(Interpreter(self.data).execute("SELECT * FROM people").rows, expected)
        self.db.execute("INSERT INTO people VALUES ('Dee', 3)")  # the open table keeps saving after compacting
        self.assertEqual(len(Interpreter(self.data).execute("SELECT * FROM people").rows), 2)
        self.db.execute("CREATE TABLE empty (x int)")
        self.assertEqual([r[0] for r in self.rows("COMPACT")], ["empty", "people"])
        self.assertEqual(Interpreter(self.data).execute("DESCRIBE empty").rows, [["x", "int"]])

    def test_use_and_show_files(self):
        files = self.rows("SHOW FILES")
        self.assertEqual([(r[0], r[1], r[3]) for r in files], [("people", os.path.join(self.data, "people.siql"), 2)])
        other = os.path.join(self.dir, "other")
        self.db.execute(f"USE '{other}'")
        self.assertTrue(os.path.isdir(other))
        self.assertEqual(self.rows("SHOW TABLES"), [])
        self.db.execute(f"USE '{self.data}'")
        self.assertEqual(self.rows("SHOW TABLES"), [["people"]])

    def test_export_and_import(self):
        self.db.executescript("ALTER TABLE people ADD ok bool; ALTER TABLE people ADD score float; "
                              "ALTER TABLE people ADD tags list; "
                              "UPDATE people SET ok = TRUE, score = 1.5 WHERE name = 'Ann'")
        path = os.path.join(self.dir, "people.csv")
        self.assertEqual(self.db.execute(f"EXPORT people TO '{path}'").rowcount, 2)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read().splitlines(), ["name,age,ok,score,tags", "Ann,31,true,1.5,", "Bob,25,,,"])

        # Into a new table: types are inferred
        self.db.execute(f"IMPORT '{path}' INTO copy")
        self.assertEqual(self.rows("DESCRIBE copy"),
                         [["name", "str"], ["age", "int"], ["ok", "bool"], ["score", "float"], ["tags", "str"]])
        self.assertEqual(self.rows("SELECT * FROM copy"), [["Ann", 31, True, 1.5, None], ["Bob", 25, None, None, None]])

        # Into an existing table: fields are read as its column types
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("tags,name,age\n\"[1, 'a']\",Cy,7\n")
        self.assertEqual(self.db.execute(f"IMPORT '{path}' INTO people").rowcount, 1)
        self.assertEqual(self.rows("SELECT name, age, tags FROM people WHERE name = 'Cy'"), [["Cy", 7, [1, "a"]]])

    def test_bad_imports_change_nothing(self):
        cases = {"unknown column": "name,nope\nx,1\n", "bad value": "name,age\nx,old\n",
                 "short line": "name,age\nx\n", "no header": "", "duplicate header": "name,name\nx,y\n"}
        path = os.path.join(self.dir, "bad.csv")
        for label, text in cases.items():
            with self.subTest(label):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                with self.assertRaises(QueryError):
                    self.db.execute(f"IMPORT '{path}' INTO people")
                self.assertEqual(len(self.rows("SELECT * FROM people")), 2)
        with self.assertRaises(QueryError):
            self.db.execute(f"IMPORT '{os.path.join(self.dir, 'missing.csv')}' INTO people")

    def test_file_access_can_be_disabled(self):
        locked = Interpreter(self.data, file_access=False)
        path = os.path.join(self.dir, "x.csv")
        for query in [f"USE '{self.dir}'", f"EXPORT people TO '{path}'", f"IMPORT '{path}' INTO people"]:
            with self.subTest(query=query):
                with self.assertRaisesRegex(QueryError, "disabled"):
                    locked.execute(query)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(locked.execute("SHOW TABLES").rows, [["people"]])

    def test_parse_errors(self):
        for query in ["USE data", "IMPORT people.csv INTO t", "EXPORT t TO x", "RENAME people TO x",
                      "COPY TABLE a b", "ALTER TABLE t RENAME a b", "SHOW STUFF"]:
            with self.subTest(query=query):
                with self.assertRaises(QueryError):
                    parse(query)


if __name__ == "__main__":
    unittest.main()
