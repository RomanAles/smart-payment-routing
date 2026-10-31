import hashlib
import json
import sqlite3
import unittest
from pathlib import Path
from payment_routing.analytics import CanonicalDatabaseAnalytics, CanonicalDatabaseSource
from payment_routing.database import connect, create_schema, transaction
from payment_routing.importers import import_data, import_history, import_queue
from payment_routing.loaders import HistoricalActualsProvider, OperationQueueLoader, ProviderRegistry
from payment_routing.persistence import DatabaseWriter, DecisionsReader
from payment_routing.run import RoutingRun
from payment_routing.utils import ROOT, parse_time, read_json, write_json
from tests.support import DatabaseCase


class PipelineTests(DatabaseCase):
    def test_imports_are_idempotent(self):
        self.assertEqual({"providers": 4, "business_parameters": 3, "queue": 90, "history": 100}, self.seed())
        before = self.snapshot()
        self.seed()
        self.assertEqual(before, self.snapshot())

    def test_all_90_decisions_match_ruby_reference(self):
        self.seed()
        reference = read_json(ROOT / "tests/fixtures/ruby_decisions.json")
        self.config.raw["active_strategies"] = reference[0]["explanation"]["active_strategies"]
        result = RoutingRun(self.db, self.path, self.config).call()
        self.assertTrue(result.processed)
        self.assertEqual(90, len(result.decisions))
        # Compare the whole JSON tree, including floating-point scores and traces.
        def compare(actual, expected):
            if type(actual) is dict:
                self.assertEqual(actual.keys(), expected.keys())
                for key in actual:
                    compare(actual[key], expected[key])
            elif type(actual) is list:
                self.assertEqual(len(actual), len(expected))
                for a, b in zip(actual, expected):
                    compare(a, b)
            elif type(actual) in (int, float) and type(expected) in (int, float):
                self.assertAlmostEqual(actual, expected, places=10)
            else:
                self.assertEqual(actual, expected)
        compare(result.decisions, reference)
        before = self.snapshot()
        self.assertFalse(RoutingRun(self.db, self.path, self.config).call().processed)
        self.assertEqual(before, self.snapshot())
        self.assertEqual([], OperationQueueLoader(self.db).load())
        self.assertEqual(result.decisions, DecisionsReader(self.db).load())
        self.assertEqual(190, self.db.execute("SELECT COUNT(*) FROM operations_history").fetchone()[0])
        self.assertEqual([], list(self.db.execute("PRAGMA foreign_key_check")))

    def test_route_and_reopen_preserve_runtime_and_dispatch_history(self):
        self.seed()
        RoutingRun(self.db, self.path, self.config).call()
        before = ProviderRegistry(self.db, self.config.rated_providers + [self.config.fallback_provider]).load()
        requests = HistoricalActualsProvider(self.db).load()
        self.db.close()
        self.db = connect(self.path)
        self.assertEqual(before, ProviderRegistry(self.db, self.config.rated_providers + [self.config.fallback_provider]).load())
        self.assertEqual(requests, HistoricalActualsProvider(self.db).load())
        self.assertEqual(90, self.db.execute("SELECT COUNT(*) FROM routing_attempts WHERE dispatched_at IS NOT NULL").fetchone()[0])

    def test_routing_write_failure_rolls_back_everything(self):
        self.seed()
        self.db.execute("CREATE TRIGGER fail_routing BEFORE INSERT ON routing_attempts BEGIN SELECT RAISE(ABORT,'test failure'); END")
        before = self.snapshot()
        with self.assertRaises(sqlite3.DatabaseError):
            RoutingRun(self.db, self.path, self.config).call()
        self.assertEqual(before, self.snapshot())

    def test_queue_skips_history_ids_and_sorts_by_instant(self):
        self.seed(["providers", "history"])
        history = dict(self.db.execute("SELECT * FROM operations_history LIMIT 1").fetchone())
        with transaction(self.db):
            for key, at in [(history["operation_id"], "2026-07-30T01:00:00Z"), ("later", "2026-07-30T11:00:00+03:00"), ("earlier", "2026-07-30T10:30:00+03:00")]:
                self.db.execute("INSERT INTO operations_queue(operation_id,created_at,amount,bank) VALUES(?,?,?,?)", (key, at, 1000, "sberbank"))
        self.assertEqual(["earlier", "later"], [o.operation_id for o in OperationQueueLoader(self.db).load()])

    def test_writer_replaces_attempts_and_uses_queue_timestamp(self):
        self.seed(["providers"])
        op = {"operation_id": "one", "amount": 10000, "bank": "sberbank"}
        with transaction(self.db):
            self.db.execute("INSERT INTO operations_queue(operation_id,created_at,amount,bank) VALUES('one','2026-07-30T10:00:00+03:00',10000,'sberbank')")
        writer = DatabaseWriter(self.path, db=self.db)
        decision = {"operation_id": "one", "selected_provider": "vipay", "attempts": [{"provider": "vipay", "decision": "selected", "reason": "highest_score"}], "simulated_result": "approved", "latency_sec": 30}
        writer.log_operations([op], [decision])
        decision.update(selected_provider="payflow", attempts=[{"provider": "vipay", "decision": "skipped", "reason": "provider_timeout"}, {"provider": "payflow", "decision": "selected", "reason": "fallback_candidate"}])
        writer.log_operations([op], [decision])
        self.assertEqual(2, self.db.execute("SELECT COUNT(*) FROM routing_attempts").fetchone()[0])
        self.assertEqual(1, self.db.execute("SELECT COUNT(*) FROM operations_history").fetchone()[0])
        self.assertEqual("2026-07-30T10:00:00+03:00", self.db.execute("SELECT created_at FROM operations_history").fetchone()[0])
        report = self.report()
        self.assertEqual(1, report["total_operations"])
        self.assertEqual(1, report["distribution"]["payflow"]["count"])
        self.assertEqual({"provider_timeout": 1}, report["skip_reasons"])
        self.assertEqual(1, report["attempt_cascades"]["fallback_operations"])

    def report(self):
        analytics = CanonicalDatabaseAnalytics(self.path)
        try:
            return analytics.report(parse_time("2026-09-05T12:00:00+07:00"))
        finally:
            analytics.close()

    def test_history_analytics_are_readonly_and_preserve_known_totals(self):
        self.seed(["providers", "business_parameters", "history"])
        import_queue(self.db, ROOT / "tests/fixtures/operations_queue_10.json")
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        report = self.report()
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(100, report["total_operations"])
        self.assertEqual(10, report["pending_operations"])
        self.assertEqual(110, report["all_operations_seen"])
        self.assertEqual(3391500, report["total_amount"])
        self.assertEqual(385800, report["pending_queue"]["total_amount"])
        self.assertEqual([41, 19, 40], [report["distribution"][p]["count"] for p in ("vipay", "payflow", "quickpay")])
        self.assertEqual(68, report["status_summary"]["approved"]["count"])
        self.assertEqual(29, report["data_quality"]["history_current_bank_rule_mismatch_count"])
        self.assertEqual(9, report["data_quality"]["foreign_key_definitions"])
        self.assertEqual("ok", report["data_quality"]["database_integrity"])
        self.assertEqual(0, sum(report["data_quality"]["database_orphans"].values()))
        self.assertEqual({}, report["skip_reasons"])
        write_json(Path(self.directory.name) / "report.json", report)

    def test_report_after_route_has_segments_comparison_and_unique_skips(self):
        self.seed()
        RoutingRun(self.db, self.path, self.config).call()
        report = self.report()
        self.assertEqual(190, report["total_operations"])
        self.assertEqual(0, report["pending_operations"])
        self.assertEqual(90, report["routing_coverage"]["with_decision"])
        self.assertEqual(100, report["attempt_cascades"]["attempt_log_coverage_pct"])
        comparison = report["period_comparison"]
        self.assertEqual("available_with_caveats", comparison["status"])
        self.assertEqual(90, comparison["current"]["count"])
        self.assertEqual(100, comparison["previous"]["count"])
        self.assertEqual(-10, comparison["changes"]["count"]["delta"])
        self.assertEqual(190, sum(group["count"] for group in report["segments"]["by_bank"].values()))
        self.assertEqual(190, sum(group["count"] for group in report["segments"]["by_amount"].values()))
        self.assertTrue(all("minute_stats" in p for p in report["provider_state"].values()))

    def test_all_numeric_report_values_match_ruby_reference(self):
        self.seed()
        reference = read_json(ROOT / "tests/fixtures/ruby_report.json")
        decisions = read_json(ROOT / "tests/fixtures/ruby_decisions.json")
        self.config.raw["active_strategies"] = decisions[0]["explanation"]["active_strategies"]
        RoutingRun(self.db, self.path, self.config).call()
        # Freeze the persistence timestamps to reproduce the saved report's age metrics.
        with transaction(self.db):
            for table in ("routing_decisions", "routing_attempts", "provider_skip_reasons"):
                at = reference["freshness"]["sources"][table]["to"]
                self.db.execute(f"UPDATE {table} SET created_at=?", (at,))
            for name, p in reference["provider_state"].items():
                self.db.execute("UPDATE providers SET stats_calculated_at=? WHERE payment_system=?", (p["minute_stats"]["stats_calculated_at"], name))
        analytics = CanonicalDatabaseAnalytics(self.path)
        try:
            actual = analytics.report(parse_time(reference["freshness"]["evaluated_at"]))
        finally:
            analytics.close()
        def compare_numbers(a, b, path):
            if isinstance(b, dict):
                self.assertIsInstance(a, dict, path)
                for key, value in b.items():
                    compare_numbers(a.get(key), value, path + "." + key)
            elif isinstance(b, list):
                self.assertIsInstance(a, list, path)
                # Recommendations are textual and may differ in numeric formatting.
                if path != "$.recommendations":
                    self.assertEqual(len(a), len(b), path)
                    for i, (x, y) in enumerate(zip(a, b)):
                        compare_numbers(x, y, path + f"[{i}]")
            elif type(b) in (int, float):
                self.assertIsNotNone(a, path)
                self.assertAlmostEqual(a, b, places=7, msg=path)
        compare_numbers(actual, reference, "$")

    def test_reject_noncanonical_schema_without_writes(self):
        self.db.execute("CREATE TABLE unexpected(x)")
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            CanonicalDatabaseSource(self.path)
        self.assertEqual(before, self.path.read_bytes())

    def test_import_failure_is_atomic(self):
        self.seed(["providers"])
        source = [{"operation_id": "valid", "created_at": "2026-07-30T00:00:00Z", "amount": 100, "bank": "sberbank"}, {"operation_id": "invalid", "created_at": None, "amount": 100, "bank": "sberbank"}]
        path = Path(self.directory.name) / "broken.json"
        write_json(path, source)
        before = self.snapshot()
        with self.assertRaises(sqlite3.DatabaseError):
            import_queue(self.db, path)
        self.assertEqual(before, self.snapshot())

    def test_schema_upgrade_is_idempotent(self):
        self.seed()
        before = self.snapshot()
        create_schema(self.db)
        self.assertEqual(before, self.snapshot())
