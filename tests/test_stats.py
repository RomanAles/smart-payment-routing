import hashlib
import sqlite3
from payment_routing.database import columns, transaction
from payment_routing.stats import ProviderMinuteStats
from payment_routing.utils import parse_time
from tests.support import DatabaseCase


class StatsTests(DatabaseCase):
    AT = "2026-09-05T12:00:00Z"

    def setUp(self):
        super().setUp()
        self.seed(["providers", "business_parameters"])

    def operation(self, key, at="2026-09-05T11:59:30Z", amount=500, provider="vipay", status="approved", latency=None):
        pid = self.db.execute("SELECT payment_system_id FROM providers WHERE payment_system=?", (provider,)).fetchone()[0]
        with transaction(self.db):
            self.db.execute("INSERT INTO operations_history(operation_id,created_at,amount,bank,payment_system_id,status,latency_sec) VALUES(?,?,?,'sberbank',?,?,?)", (key, at, amount, pid, status, latency))

    def run_stats(self, **kwargs):
        return ProviderMinuteStats(self.path, at=kwargs.pop("at", self.AT), **kwargs).run()

    def test_idempotence_and_expiration(self):
        self.operation("recent")
        result = self.run_stats()
        before = self.snapshot()
        again = self.run_stats()
        self.assertEqual({k: v for k, v in result.items() if k != "persistence"}, {k: v for k, v in again.items() if k != "persistence"})
        self.assertEqual(before, self.snapshot())
        expired = self.run_stats(at="2026-09-05T12:01:01Z")
        self.assertTrue(all(p["requests_last_minute"] == 0 for p in expired["providers"]))
        self.assertNotIn("requests_last_minute", columns(self.db, "providers"))
        self.assertEqual(1, expired["totals"]["analysis"]["count"])

    def test_invalid_timestamp_or_amount_does_not_write(self):
        for key, kwargs in [("bad-time", {"at": "not-a-date"}), ("bad-amount", {"amount": "invalid"}), ("bad-latency", {"latency": -1})]:
            with self.subTest(key=key):
                self.operation(key, **kwargs)
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, key):
                    self.run_stats()
                self.assertEqual(before, self.snapshot())
                with transaction(self.db):
                    self.db.execute("DELETE FROM operations_history")

    def test_stats_failure_rolls_back_schema_and_values(self):
        self.operation("recent")
        self.db.execute("CREATE TRIGGER reject_stats BEFORE UPDATE ON providers WHEN NEW.payment_system='payflow' BEGIN SELECT RAISE(ABORT,'test failure'); END")
        schema = [tuple(r) for r in self.db.execute("SELECT sql FROM sqlite_master ORDER BY name")]
        before = self.snapshot()
        with self.assertRaises(sqlite3.DatabaseError):
            self.run_stats()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(schema, [tuple(r) for r in self.db.execute("SELECT sql FROM sqlite_master ORDER BY name")])

    def test_dry_run_is_readonly(self):
        self.operation("recent")
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        result = self.run_stats(dry_run=True)
        self.assertTrue(result["persistence"]["dry_run"])
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_empty_denominators_and_zero_limits_are_null(self):
        with transaction(self.db):
            self.db.execute("UPDATE providers SET requests_per_minute_limit=0,daily_amount_limit=0")
        result = self.run_stats()
        for row in result["providers"]:
            self.assertIsNone(row["conversion_24h"])
            self.assertIsNone(row["avg_latency_sec"])
            self.assertIsNone(row["periods"]["minute"]["approval_pct"])
            self.assertIsNone(row["periods"]["minute"]["count_share_pct"])
            self.assertIsNone(row["targets"]["requests_limit_utilization_pct"])
            self.assertIsNone(row["minute_change"]["count_change_pct"])

    def test_midnight_and_exclusive_bands(self):
        for i, amount in enumerate([0, 999, 1000, 9999, 10000, 49999, 50000, 99999, 100000]):
            self.operation(f"band{i}", at="2026-09-05T00:00:00Z", amount=amount)
        result = self.run_stats(at="2026-09-05T00:00:30Z")
        vipay = next(p for p in result["providers"] if p["payment_system"] == "vipay")
        self.assertEqual(9, vipay["periods"]["today_created_operations"]["count"])
        self.assertEqual([2, 2, 2, 2, 1], [r["count"] for r in vipay["minute_breakdown"]["amount_bands"]])
        self.assertEqual(100, vipay["periods"]["minute"]["approval_pct"])

    def test_minute_boundaries_offset_and_existing_requests_column(self):
        self.db.execute("ALTER TABLE providers ADD COLUMN requests_last_minute INTEGER")
        self.operation("excluded", at="2026-09-05T11:59:00Z")
        self.operation("included", at="2026-09-05T15:00:00+03:00")
        self.operation("future", at="2026-09-05T12:00:01Z")
        result = self.run_stats()
        self.assertEqual(1, result["providers"][0]["requests_last_minute"])
        self.assertEqual(2, result["totals"]["analysis"]["count"])
        self.assertEqual(1, self.db.execute("SELECT requests_last_minute FROM providers WHERE payment_system='vipay'").fetchone()[0])

    def test_analysis_window_and_snapshot_state_preservation(self):
        self.operation("old", at="2026-09-05T10:00:00Z", latency=10, status="rejected")
        self.operation("recent", latency=20)
        before = [tuple(r) for r in self.db.execute("SELECT in_progress_count,in_progress_amount,daily_approved_amount FROM providers")]
        result = self.run_stats(window_seconds=60)
        p = result["providers"][0]
        self.assertEqual(1, p["periods"]["analysis"]["count"])
        self.assertEqual(.5, p["conversion_24h"])
        self.assertEqual(20, p["avg_latency_sec"])
        self.assertEqual(60, p["stats_window_sec"])
        self.assertEqual(before, [tuple(r) for r in self.db.execute("SELECT in_progress_count,in_progress_amount,daily_approved_amount FROM providers")])
