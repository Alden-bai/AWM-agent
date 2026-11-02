import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("clean_data", Path(__file__).resolve().parents[1] / "scripts/clean_data.py")
clean = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clean)


class ScreeningTests(unittest.TestCase):
    def test_uncertainty_does_not_reject(self):
        env = {"initialization_complete": True, "hard_defect": False}
        self.assertEqual(clean.disposition(env, [], {"usable": True}, ["legacy_confirmed_defect"]),
                         ("retain", "review_required"))
        self.assertEqual(clean.disposition(env, [], {"usable": False}, []), ("retain", "blocked_verifier"))
        self.assertEqual(clean.disposition({}, [], {"usable": True}, []),
                         ("retain", "blocked_assets_or_environment_unknown"))

    def test_conflicting_and_identical_duplicates(self):
        a = {"task": "t", "verification": {"code": "def verify_task(a,b): return {}"}}
        b = {"task": "t", "verification": {"code": "def verify_task(a,b): return {'ok': True}"}}
        self.assertFalse(clean.verifier_binding([a, b], "t")["usable"])
        self.assertTrue(clean.verifier_binding([a, a], "t")["usable"])
        self.assertFalse(clean.verifier_binding([a], "wrong task")["usable"])

    def test_foreign_keys_enabled_after_schema_examples_commit(self):
        schema = {"tables": [
            {"name": "p", "ddl": "CREATE TABLE p(id INTEGER PRIMARY KEY)", "examples": ["INSERT INTO p VALUES(1)"]},
            {"name": "c", "ddl": "CREATE TABLE c(pid INTEGER REFERENCES p(id))"}]}
        sample = {"tables": [{"table_name": "c", "insert_statements": ["INSERT INTO c VALUES(2)"]}]}
        result = clean.initialize(schema, sample)
        self.assertFalse(result["complete"])
        self.assertEqual(result["errors"][0]["sqlite_errorname"], "SQLITE_CONSTRAINT_FOREIGNKEY")

    def test_reproduced_bad_sql_is_environment_defect(self):
        with tempfile.TemporaryDirectory() as d:
            result = clean.check_environment(
                {"db_schema": {"tables": [{"name": "t", "ddl": "CREATE TABLE t(id INTEGER)"}]}},
                {"sample_data": {"tables": [{"table_name": "t", "insert_statements": ["INSERT INTO t(id, 2);"]}]}},
                {"full_code": "x=1"}, Path(d))
        self.assertTrue(result["hard_defect"])
        self.assertTrue(result["failure_reproduced"])

    def test_missing_extension_is_unknown_not_data_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            result = clean.check_environment(
                {"db_schema": {"tables": [{"name": "t", "ddl": "CREATE TABLE t(id INTEGER)"}]}},
                {"sample_data": {"tables": [{"table_name": "t", "insert_statements": ["INSERT INTO t VALUES(nonexistent_function())"]}]}},
                {"full_code": "x=1"}, Path(d))
        self.assertFalse(result["hard_defect"])

    def test_invalid_judge_is_excluded_not_counted_as_semantic_failure(self):
        cases = [
            {"status": "valid", "oracle": {"correct": True}, "modes": {"sql": {"status": "valid", "accepted": True}}},
            {"status": "valid", "oracle": {"correct": True}, "modes": {"sql": {"status": "llm_invalid_output"}}},
            {"status": "negative_not_wrong", "oracle": {"correct": False}, "modes": {"sql": {"status": "valid", "accepted": True}}}]
        result = clean.legacy_metrics(cases)
        self.assertEqual(result["accuracy"]["denominator"], 1)
        self.assertEqual(result["excluded_sql"], {"llm_invalid_output": 1})


if __name__ == "__main__":
    unittest.main()
