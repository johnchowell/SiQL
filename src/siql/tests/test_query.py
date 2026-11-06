"""Tests for SELECTs that run on the query engine (siql.controllers.query): JOIN, GROUP BY, HAVING, aggregates,
expressions, DISTINCT, derived tables and subqueries, and which SELECTs still parse to the one-table `Select`."""
import tempfile
import unittest

from ..controllers import Interpreter, QueryError
from ..controllers.parser import Select, SelectQuery, parse


class QueryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Interpreter(self._tmp.name)
        self.db.executescript("""
            CREATE TABLE assets (id str, kind str, parent str, w int, vec vector);
            INSERT INTO assets VALUES ('a1', 'character', NULL, 100, [1, 0]), ('p1', 'character', 'a1', 110, [0.9, 0.1]),
                ('p2', 'character', 'a1', 90, [0, 1]), ('a2', 'character', NULL, 120, NULL),
                ('p3', 'character', 'a2', 80, [1, 1]), ('b1', 'background', NULL, 1000, [0, 1]),
                ('o1', 'object', NULL, NULL, NULL);
            CREATE TABLE uses (asset str, job str);
            INSERT INTO uses VALUES ('p1', 'j1'), ('p1', 'j2'), ('p3', 'j1'), ('b1', 'j1'), ('zz', 'j3')""")

    def tearDown(self):
        self._tmp.cleanup()

    def q(self, sql):
        r = self.db.execute(sql)
        return r.columns, r.rows

    def rows(self, sql):
        return self.db.execute(sql).rows

    def test_simple_selects_keep_the_one_table_path(self):
        for sql in ["SELECT * FROM t", "SELECT a, b FROM t WHERE a = 1 ORDER BY b DESC LIMIT 2",
                    "SELECT COUNT(*) FROM t WHERE a IN (1, -2)", "SELECT id FROM t ORDER BY COSINE(v, [1, 0]) LIMIT 1"]:
            self.assertIsInstance(parse(sql)[0], Select, sql)
        for sql in ["SELECT t.a FROM t", "SELECT a FROM t x", "SELECT DISTINCT a FROM t", "SELECT a + 1 FROM t",
                    "SELECT a AS b FROM t", "SELECT a, COUNT(*) FROM t GROUP BY a", "SELECT a FROM t JOIN u ON a = b",
                    "SELECT a FROM t WHERE a IN (SELECT b FROM u)", "SELECT a FROM t WHERE a + 1 > 2"]:
            self.assertIsInstance(parse(sql)[0], SelectQuery, sql)

    def test_count_next_to_columns_with_a_left_join(self):
        self.assertEqual(self.q("SELECT a.id, COUNT(p.id) AS poses FROM assets a LEFT JOIN assets p ON p.parent = a.id "
                                "WHERE a.parent IS NULL GROUP BY a.id ORDER BY poses DESC, a.id"),
                         (["id", "poses"], [["a1", 2], ["a2", 1], ["b1", 0], ["o1", 0]]))

    def test_correlated_subquery_count(self):
        self.assertEqual(self.rows("SELECT id, (SELECT COUNT(*) FROM assets p WHERE p.parent = a.id) AS poses "
                                   "FROM assets a WHERE kind = 'character' AND parent IS NULL"), [["a1", 2], ["a2", 1]])

    def test_group_by_having_and_aggregates(self):
        self.assertEqual(self.q("SELECT kind, COUNT(*) AS n, SUM(w), AVG(w), MIN(w), MAX(w) FROM assets "
                                "GROUP BY kind HAVING n > 0 ORDER BY kind"),
                         (["kind", "n", "SUM(w)", "AVG(w)", "MIN(w)", "MAX(w)"],
                          [["background", 1, 1000, 1000.0, 1000, 1000], ["character", 5, 500, 100.0, 80, 120],
                           ["object", 1, None, None, None, None]]))
        self.assertEqual(self.rows("SELECT kind FROM assets GROUP BY kind HAVING COUNT(*) > 1"), [["character"]])
        self.assertEqual(self.rows("SELECT COUNT(DISTINCT kind), COUNT(parent), COUNT(*) FROM assets"), [[3, 3, 7]])
        # Aggregates over no rows still give one row
        self.assertEqual(self.rows("SELECT COUNT(*), SUM(w), MAX(id) FROM assets WHERE w > 5000"), [[0, None, None]])
        self.assertEqual(self.rows("SELECT kind, COUNT(*) FROM assets WHERE w > 5000 GROUP BY kind"), [])

    def test_inner_join_cross_join_and_qualified_star(self):
        self.assertEqual(self.rows("SELECT a.id, u.job FROM assets a JOIN uses u ON u.asset = a.id ORDER BY a.id, u.job"),
                         [["b1", "j1"], ["p1", "j1"], ["p1", "j2"], ["p3", "j1"]])
        self.assertEqual(self.rows("SELECT id, job FROM assets INNER JOIN uses ON asset = id AND job = 'j2'"),
                         [["p1", "j2"]])
        self.assertEqual(self.q("SELECT a.*, u.job FROM assets a, uses u WHERE u.asset = a.id AND a.kind = 'background'")[0],
                         ["id", "kind", "parent", "w", "vec", "job"])
        self.assertEqual(len(self.rows("SELECT * FROM assets CROSS JOIN uses")), 35)
        self.assertEqual(self.q("SELECT * FROM assets a JOIN assets b ON b.parent = a.id WHERE b.id = 'p3'"),
                         (["a.id", "a.kind", "a.parent", "a.w", "a.vec", "b.id", "b.kind", "b.parent", "b.w", "b.vec"],
                          [["a2", "character", None, 120, self.db.execute("SELECT vec FROM assets WHERE id = 'a2'").rows[0][0],
                            "p3", "character", "a2", 80, self.rows("SELECT vec FROM assets WHERE id = 'p3'")[0][0]]]))

    def test_left_join_keeps_unmatched_rows_and_on_vs_where(self):
        self.assertEqual(self.rows("SELECT u.asset, a.kind FROM uses u LEFT JOIN assets a ON a.id = u.asset "
                                   "ORDER BY u.asset, u.job"),
                         [["b1", "background"], ["p1", "character"], ["p1", "character"], ["p3", "character"],
                          ["zz", None]])
        # A condition in ON keeps the left row; the same condition in WHERE drops it
        self.assertEqual(len(self.rows("SELECT * FROM uses u LEFT JOIN assets a ON a.id = u.asset AND a.w > 100")), 5)
        self.assertEqual(len(self.rows("SELECT * FROM uses u LEFT JOIN assets a ON a.id = u.asset WHERE a.w > 100")), 3)
        self.assertEqual(self.rows("SELECT u.asset FROM uses u LEFT JOIN assets a ON a.id = u.asset WHERE a.id IS NULL"),
                         [["zz"]])

    def test_join_with_index_lookups_matches_hash_join(self):
        self.db.execute("CREATE TABLE big (k int, v str)")
        self.db.execute("INSERT INTO big VALUES " + ", ".join(f"({i % 50}, 'v{i}')" for i in range(500)))
        self.db.execute("CREATE TABLE few (k int)")
        self.db.execute("INSERT INTO few VALUES (3), (7), (NULL)")
        rows = self.rows("SELECT f.k, COUNT(b.v) FROM few f LEFT JOIN big b ON b.k = f.k GROUP BY f.k ORDER BY f.k")
        self.assertEqual(rows, [[None, 0], [3, 10], [7, 10]])

    def test_subqueries_in_where(self):
        self.assertEqual(self.rows("SELECT id FROM assets WHERE id IN (SELECT asset FROM uses) ORDER BY id"),
                         [["b1"], ["p1"], ["p3"]])
        self.assertEqual(self.rows("SELECT id FROM assets WHERE id NOT IN (SELECT asset FROM uses) AND parent IS NULL "
                                   "ORDER BY id"), [["a1"], ["a2"], ["o1"]])
        self.assertEqual(self.rows("SELECT id FROM assets a WHERE NOT EXISTS (SELECT * FROM uses WHERE asset = a.id) "
                                   "ORDER BY id"), [["a1"], ["a2"], ["o1"], ["p2"]])
        self.assertEqual(self.rows("SELECT id FROM assets WHERE w > (SELECT AVG(w) FROM assets)"), [["b1"]])
        # SiQL's = is true for NULL = NULL (as in the one-table SELECT), so o1 (no w, MAX NULL) matches too
        self.assertEqual(self.rows("SELECT id FROM assets a WHERE w = (SELECT MAX(w) FROM assets b "
                                   "WHERE b.kind = a.kind) ORDER BY id"), [["a2"], ["b1"], ["o1"]])

    def test_derived_table(self):
        self.assertEqual(self.rows("SELECT c.parent, c.n FROM (SELECT parent, COUNT(*) AS n FROM assets "
                                   "WHERE parent IS NOT NULL GROUP BY parent) c WHERE c.n > 1"), [["a1", 2]])
        self.assertEqual(self.rows("SELECT a.id, c.n FROM assets a JOIN (SELECT parent, COUNT(*) AS n FROM assets "
                                   "GROUP BY parent) AS c ON c.parent = a.id ORDER BY a.id"), [["a1", 2], ["a2", 1]])

    def test_expressions_functions_and_distinct(self):
        self.assertEqual(self.q("SELECT id, w * 2 + 1 AS x, (w - 100) / 10 FROM assets WHERE (w + 10) * 2 > 230 "
                                "ORDER BY x"),
                         (["id", "x", "(w - 100) / 10"], [["p1", 221, 1.0], ["a2", 241, 2.0], ["b1", 2001, 90.0]]))
        self.assertEqual(self.rows("SELECT w / 0, -w, COALESCE(parent, 'none'), UPPER(kind), LENGTH(id) FROM assets "
                                   "WHERE id = 'a1'"), [[None, -100, "none", "CHARACTER", 2]])
        self.assertEqual(self.rows("SELECT ROUND(AVG(w) / 3, 2) FROM assets"), [[round(1500 / 6 / 3, 2)]])
        self.assertEqual(self.rows("SELECT DISTINCT kind FROM assets ORDER BY kind"),
                         [["background"], ["character"], ["object"]])
        self.assertEqual(self.rows("SELECT DISTINCT parent FROM assets WHERE parent IS NOT NULL ORDER BY parent LIMIT 1"),
                         [["a1"]])

    def test_vector_similarity_in_joins(self):
        self.assertEqual(self.rows("SELECT a.id FROM assets a JOIN uses u ON u.asset = a.id "
                                   "ORDER BY COSINE(a.vec, [1, 0]) DESC LIMIT 2"), [["p1"], ["p1"]])
        self.assertEqual([r[0] for r in self.rows("SELECT id, ROUND(COSINE(vec, [1, 0]), 3) AS s FROM assets "
                                                  "WHERE vec IS NOT NULL ORDER BY s DESC, id")],
                         ["a1", "p1", "p3", "b1", "p2"])

    def test_update_and_delete_with_subqueries(self):
        self.assertEqual(self.db.execute("UPDATE assets SET w = 1 WHERE id IN (SELECT asset FROM uses WHERE job = 'j2')")
                         .rowcount, 1)
        self.assertEqual(self.db.execute("DELETE FROM uses WHERE asset NOT IN (SELECT id FROM assets)").rowcount, 1)
        with self.assertRaises(QueryError):  # the subquery can't use the outer table's columns
            self.db.execute("DELETE FROM assets WHERE EXISTS (SELECT * FROM uses WHERE asset = id2)")

    def test_errors(self):
        for sql, message in [
            ("SELECT id FROM assets a JOIN assets b ON a.id = b.parent", "Ambiguous column id"),
            ("SELECT kind FROM assets WHERE COUNT(*) > 1", "HAVING"),
            ("SELECT (SELECT id FROM assets) FROM uses", "gave 7 rows"),
            ("SELECT (SELECT id, kind FROM assets LIMIT 1) FROM uses", "one column"),
            ("SELECT a.nope FROM assets a", "No such column: a.nope"),
            ("SELECT x.id FROM assets a", "No such column: x.id"),
            ("SELECT * FROM assets a JOIN uses a ON 1 = 1", "used for two sources"),
            ("SELECT SUM(COUNT(*)) FROM assets", "inside another"),
            ("SELECT id FROM (SELECT id FROM assets)", "alias"),
            ("SELECT * FROM assets LEFT JOIN uses", "Expected ON"),
            ("SELECT LOWER(id, kind) FROM assets", "LOWER takes 1 argument"),
            ("UPDATE assets SET w = 1 WHERE COUNT(*) > 1", "only be used in a SELECT"),
        ]:
            with self.subTest(sql=sql):
                with self.assertRaises(QueryError) as caught:
                    self.db.execute(sql)
                self.assertIn(message, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
