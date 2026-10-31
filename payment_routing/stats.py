from datetime import timedelta
from pathlib import Path
from . import metrics
from .database import STATS_COLUMNS, columns, connect, rows, transaction
from .utils import UTC, clean, iso, now, parse_time


class ProviderMinuteStats:
    Error = ValueError

    def __init__(self, database, at=None, dry_run=False, window_seconds=None):
        if window_seconds is not None and (type(window_seconds) is not int or window_seconds <= 0):
            raise ValueError("window_seconds must be a positive integer or None (all history)")
        self.path = Path(database)
        self.at = parse_time(at) if at is not None else None
        if at is not None and self.at is None:
            raise ValueError("Invalid --at timestamp")
        self.dry_run, self.window_seconds = dry_run, window_seconds

    def run(self):
        if not self.path.is_file():
            raise ValueError(f"Database does not exist: {self.path}")
        db = connect(self.path, readonly=self.dry_run)
        try:
            with transaction(db, immediate=not self.dry_run):
                result = self.build_report(db, (self.at or now()).astimezone(UTC))
                existing = columns(db, "providers")
                fields = ["conversion_24h", "avg_latency_sec"]
                if not set(fields) <= set(existing):
                    raise ValueError("providers: missing conversion_24h/avg_latency_sec")
                if "requests_last_minute" in existing:
                    fields.append("requests_last_minute")
                fields += list(STATS_COLUMNS)
                missing = [key for key in STATS_COLUMNS if key not in existing]
                if not self.dry_run:
                    for key in missing:
                        db.execute(f'ALTER TABLE providers ADD COLUMN "{key}" {STATS_COLUMNS[key]}')
                    for row in result["providers"]:
                        assignments = ", ".join(f'"{key}"=?' for key in fields)
                        db.execute(f"UPDATE providers SET {assignments} WHERE payment_system_id=?", [row[key] for key in fields] + [row["payment_system_id"]])
                result["persistence"] = {"dry_run": self.dry_run, "columns": fields, "columns_to_add": missing, "schema_changed": not self.dry_run and bool(missing)}
            return result
        finally:
            db.close()

    def period_windows(self, at):
        bounds = {"analysis": (at - timedelta(seconds=self.window_seconds) if self.window_seconds else None, at, False),
                  "minute": (at - timedelta(seconds=60), at, False), "previous_minute": (at - timedelta(seconds=120), at - timedelta(seconds=60), False),
                  "last_hour": (at - timedelta(hours=1), at, False), "last_24h": (at - timedelta(days=1), at, False),
                  "today_created_operations": (at.replace(hour=0, minute=0, second=0, microsecond=0), at, True)}
        return {name: {"start": iso(start, True), "end": iso(end, True), "start_inclusive": inclusive, "end_inclusive": True} for name, (start, end, inclusive) in bounds.items()}

    def build_report(self, db, at):
        for table, required in {"providers": {"payment_system_id", "payment_system"}, "operations_history": {"operation_id", "created_at", "amount", "payment_system_id", "status", "latency_sec", "bank"}}.items():
            if not required <= set(columns(db, table)):
                raise ValueError(f"{table}: missing columns")
        invalid = db.execute("SELECT operation_id FROM operations_history WHERE created_at IS NULL OR julianday(created_at) IS NULL LIMIT 1").fetchone()
        if invalid:
            raise ValueError(f"Invalid or missing operations_history.created_at: {invalid[0]}")
        start = iso(at - timedelta(seconds=max(self.window_seconds, 86400)), True) if self.window_seconds else None
        end = iso(at, True)
        invalid = db.execute("""SELECT h.operation_id FROM operations_history h LEFT JOIN providers p USING(payment_system_id)
            WHERE (? IS NULL OR julianday(h.created_at)>=julianday(?)) AND julianday(h.created_at)<=julianday(?)
            AND (p.payment_system_id IS NULL OR typeof(h.amount) NOT IN ('integer','real') OR h.amount<0
            OR (h.latency_sec IS NOT NULL AND (typeof(h.latency_sec) NOT IN ('integer','real') OR h.latency_sec<0))) LIMIT 1""", (start, start, end)).fetchone()
        if invalid:
            raise ValueError(f"Missing provider or invalid amount/latency for operation: {invalid[0]}")
        history = rows(db, "SELECT *, julianday(created_at) AS created_jd FROM operations_history WHERE (? IS NULL OR julianday(created_at)>=julianday(?)) AND julianday(created_at)<=julianday(?) ORDER BY julianday(created_at),operation_id", (start, start, end))
        windows = self.period_windows(at)
        cohorts = {}
        for name, window in windows.items():
            lower = db.execute("SELECT julianday(?)", (window["start"],)).fetchone()[0]
            upper = db.execute("SELECT julianday(?)", (window["end"],)).fetchone()[0]
            cohorts[name] = [r for r in history if (lower is None or (r["created_jd"] >= lower if window["start_inclusive"] else r["created_jd"] > lower)) and r["created_jd"] <= upper]
        totals = {name: metrics.summary(group) for name, group in cohorts.items()}
        result = []
        for p in rows(db, "SELECT * FROM providers ORDER BY payment_system_id"):
            pid = p["payment_system_id"]
            groups = {name: [r for r in group if r["payment_system_id"] == pid] for name, group in cohorts.items()}
            periods = {name: metrics.shares(metrics.summary(group), totals[name]) for name, group in groups.items()}
            minute, previous, analysis, day = [periods[k] for k in ("minute", "previous_minute", "analysis", "last_24h")]
            targets = metrics.targets(p, minute)
            targets.update(count_share_gap_pp=metrics.gap(analysis["count_share_pct"], p.get("traffic_percentage")), amount_share_gap_pp=metrics.gap(analysis["amount_share_pct"], p.get("volume_share_pct")),
                           count_target_fulfillment_pct=metrics.pct(analysis["count_share_pct"], p.get("traffic_percentage")), volume_target_fulfillment_pct=metrics.pct(analysis["amount_share_pct"], p.get("volume_share_pct")))
            item = {"payment_system_id": pid, "payment_system": p["payment_system"], "requests_last_minute": minute["count"], "in_progress_count": minute["count"], "in_progress_amount": minute["amount"],
                    "conversion_24h": day["statuses"]["approved"]["count"] / day["count"] if day["count"] else None,
                    "avg_latency_sec": clean(analysis["latency"]["avg_sec"], 0), "periods": periods,
                    "minute_change": {"count_delta": minute["count"] - previous["count"], "amount_delta": minute["amount"] - previous["amount"],
                                      "count_change_pct": metrics.pct(minute["count"] - previous["count"], previous["count"]), "amount_change_pct": metrics.pct(minute["amount"] - previous["amount"], previous["amount"])},
                    "targets": targets, "minute_breakdown": metrics.breakdown(groups["minute"], minute), "analysis_breakdown": metrics.breakdown(groups["analysis"], analysis)}
            item.update(actual_count_share_pct=analysis["count_share_pct"], actual_volume_share_pct=analysis["amount_share_pct"], approved_volume_share_pct=analysis["approved_amount_share_pct"],
                        count_target_fulfillment_pct=targets["count_target_fulfillment_pct"], volume_target_fulfillment_pct=targets["volume_target_fulfillment_pct"],
                        count_share_gap_pp=targets["count_share_gap_pp"], volume_share_gap_pp=targets["amount_share_gap_pp"],
                        approval_rate_pct=analysis["approval_pct"], rejection_rate_pct=analysis["rejection_pct"], expiration_rate_pct=analysis["expiration_pct"],
                        approved_amount_pct=analysis["approved_amount_pct"], terminal_approval_rate_pct=analysis["terminal_approval_pct"], stats_calculated_at=end, stats_window_sec=self.window_seconds)
            result.append(item)
        return {"window_start_exclusive": windows["analysis"]["start"], "window_end_inclusive": end, "window_seconds": self.window_seconds,
                "window_mode": "rolling" if self.window_seconds else "all_time", "windows": windows, "totals": totals, "providers": result,
                "definitions": {"source": "operations_history.created_at; one recorded application per operation, all statuses", "daily": "UTC calendar day; approved operations created today, not payments completed today",
                                "conversion_24h": "approved / all operations created in the last 24 hours; ratio 0..1", "analysis": "all retained history through window end by default; --window-seconds N selects (end - N seconds, end]",
                                "avg_latency_sec": "all non-null latencies in the analysis window, rounded to integer seconds", "terminal_statuses": list(metrics.TERMINAL_STATUSES), "p95": "nearest rank: sorted[ceil(0.95 * count) - 1]",
                                "breakdown_shares": "within this provider; analysis_breakdown uses the analysis window, minute_breakdown uses the last minute", "empty_denominator": "null; counts and amounts for empty cohorts are zero",
                                "snapshot_limits": "full provider snapshot freshness is unknown; stats_calculated_at dates only recalculated metrics", "in_progress": "JSON-only minute counts and amounts; stored in_progress fields are not updated",
                                "historical_at": "uses currently stored statuses, not a reconstruction of past status changes", "persisted_shares": "analysis window; percentage points for gaps, percentages for shares and target fulfillment",
                                "requests_limits": "requests_last_minute and request limit utilization always use the last 60 seconds", "stats_window_sec": "analysis window length in seconds; null means all history through stats_calculated_at",
                                "target_fulfillment": "actual share / target share * 100; null if no observations or target is absent/zero", "stats_calculated_at": "calculation reference time in UTC (window end; --at if supplied)"}}
