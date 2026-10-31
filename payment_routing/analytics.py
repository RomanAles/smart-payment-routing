"""Read-only analytics over the canonical database, with decision-over-history precedence."""
from collections import Counter, defaultdict
from math import ceil
from statistics import median
import json
from pathlib import Path
from .database import ADDITIONS, STATS_COLUMNS, TABLE_COLUMNS, columns, connect, rows, transaction
from .utils import UTC, clean, iso, now, number, parse_time, percentage, write_json

BASE_STATUSES = ("approved", "rejected", "expired")


def group_by(records, key):
    result = defaultdict(list)
    for row in records:
        result[key(row)].append(row)
    return result


def latency_stats(values):
    values = sorted(n for v in values if (n := number(v)) is not None)
    return {"count": len(values), "avg_sec": clean(sum(values) / len(values)) if values else None,
            "p50_sec": clean(median(values)) if values else None, "p95_sec": clean(values[ceil(len(values) * .95) - 1]) if values else None,
            "min_sec": clean(values[0]) if values else None, "max_sec": clean(values[-1]) if values else None}


def window(records, field="created_at", utc=False, detailed=False):
    times = [t.astimezone(UTC) if utc else t for row in records if (t := parse_time(row.get(field))) is not None]
    result = {"from": iso(min(times), detailed) if times else None, "to": iso(max(times), detailed) if times else None}
    if detailed:
        result["invalid_or_missing_timestamps"] = len(records) - len(times)
    return result


def period_label(records):
    w = window(records)
    if not w["from"] or not w["to"]:
        return "unknown"
    start, end = w["from"][:10], w["to"][:10]
    return start if start == end else f"{start}..{end}"


def latest_day_records(records):
    dated = [(t.strftime("%Y-%m-%d"), row) for row in records if (t := parse_time(row.get("created_at"))) is not None]
    latest = max((day for day, _ in dated), default=None)
    return [row for day, row in dated if day == latest]


def parse_banks(value):
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
        if isinstance(parsed, list):
            return parsed
    except (ValueError, TypeError):
        pass
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


class CanonicalDatabaseSource:
    PROVIDER_STATS_COLUMNS = ["requests_last_minute", *STATS_COLUMNS]

    def __init__(self, path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise ValueError(f"database does not exist: {self.path}")
        self.db = connect(path, readonly=True)
        try:
            self.validate_schema()
        except BaseException:
            self.close()
            raise

    def close(self):
        self.db.close()

    def validate_schema(self):
        tables = {r["name"] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables != TABLE_COLUMNS.keys():
            raise ValueError(f"database tables differ from canonical schema: {tables}")
        for table, expected in TABLE_COLUMNS.items():
            optional = list(ADDITIONS.get(table, {})) + (self.PROVIDER_STATS_COLUMNS if table == "providers" else [])
            actual = [c for c in columns(self.db, table) if c not in optional]
            if actual != list(expected):
                raise ValueError(f"columns differ for {table}; canonical column order required")
            expected_keys = []
            for field, kind in expected.items():
                if "REFERENCES " in kind:
                    reference = kind.split("REFERENCES ")[1].split()[0]
                    target, key = reference.rstrip(")").split("(")
                    expected_keys.append((field, target, key))
            actual_keys = [(r["from"], r["table"], r["to"]) for r in self.db.execute(f"PRAGMA foreign_key_list({table})")]
            if sorted(actual_keys) != sorted(expected_keys):
                raise ValueError(f"foreign keys differ for {table}")

    def provider_data(self):
        providers = rows(self.db, "SELECT * FROM providers ORDER BY priority,payment_system_id")
        for p in providers:
            p["banks"] = parse_banks(p["banks"])
            p["exclude_banks"] = bool(p["exclude_banks"])
            p["allow_negative_agreement"] = bool(p["allow_negative_agreement"])
        return {"snapshot_at": None, "gateway": None, "merchant": None, "providers": providers}

    def history_rows(self):
        return rows(self.db, "SELECT h.operation_id,h.created_at,h.amount,h.bank,h.card_brand,p.payment_system,h.status,h.latency_sec FROM operations_history h LEFT JOIN providers p USING(payment_system_id) ORDER BY h.created_at,h.operation_id")

    def pending_operations(self):
        return rows(self.db, "SELECT operation_id,created_at,amount,bank,card_brand FROM operations_queue ORDER BY created_at,operation_id")

    def attempts(self):
        result = []
        for row in rows(self.db, "SELECT a.*,p.payment_system AS provider FROM routing_attempts a LEFT JOIN providers p USING(payment_system_id) ORDER BY a.operation_id,a.attempt_number"):
            result.append({**{key: row[key] for key in ("operation_id", "provider", "decision", "reason", "dispatched_at")}, "details": json.loads(row["details"]) if row["details"] else None})
        known = {(r["operation_id"], r["provider"], r["reason"]) for r in result if r["decision"] == "skipped"}
        for row in rows(self.db, "SELECT s.operation_id,p.payment_system AS provider,s.reason FROM provider_skip_reasons s LEFT JOIN providers p USING(payment_system_id) ORDER BY s.operation_id,s.skip_reason_id"):
            if (row["operation_id"], row["provider"], row["reason"]) not in known:
                result.append({**row, "decision": "skipped"})
        return result

    def routing_events(self):
        attempts = group_by(self.attempts(), lambda row: row["operation_id"])
        source = rows(self.db, """SELECT d.*,p.payment_system AS selected_provider,
            COALESCE(q.created_at,h.created_at,d.created_at) AS operation_created_at,
            COALESCE(q.amount,h.amount) AS amount, COALESCE(q.bank,h.bank) AS bank, COALESCE(q.card_brand,h.card_brand) AS card_brand
            FROM routing_decisions d LEFT JOIN providers p ON p.payment_system_id=d.selected_payment_system_id
            LEFT JOIN operations_queue q ON q.operation_id=d.operation_id LEFT JOIN operations_history h ON h.operation_id=d.operation_id ORDER BY d.created_at,d.operation_id""")
        return [{"logged_at": r["created_at"], "event": "routing_operation", "operation_id": r["operation_id"],
                 "operation": {**{key: r[key] for key in ("operation_id", "amount", "bank", "card_brand")}, "created_at": r["operation_created_at"]},
                 "routing_decision": {**{key: r[key] for key in ("operation_id", "selected_provider", "simulated_result", "latency_sec")}, "attempts": attempts[r["operation_id"]], "explanation": json.loads(r["explanation"]) if r["explanation"] else None}}
                for r in source]

    def skip_reason_counts(self):
        return {r["reason"]: r["count"] for r in rows(self.db, """SELECT reason,COUNT(*) AS count FROM (
            SELECT operation_id,payment_system_id,COALESCE(reason,'unknown') AS reason FROM provider_skip_reasons UNION
            SELECT operation_id,payment_system_id,COALESCE(reason,'unknown') AS reason FROM routing_attempts WHERE decision='skipped') GROUP BY reason ORDER BY reason""")}

    def detail_inputs(self):
        return {"decisions": rows(self.db, "SELECT * FROM routing_decisions ORDER BY operation_id"), "queue": self.pending_operations(),
                "attempts": rows(self.db, "SELECT * FROM routing_attempts ORDER BY operation_id,attempt_number,attempt_id"),
                "eligibility": rows(self.db, "SELECT * FROM eligible_providers ORDER BY operation_id,payment_system_id"), "stored_skips": rows(self.db, "SELECT * FROM provider_skip_reasons ORDER BY skip_reason_id")}

    def orphan_counts(self):
        relations = {
            "history_unknown_provider": ("operations_history", "payment_system_id", "providers", "payment_system_id"),
            "decision_without_queue_operation": ("routing_decisions", "operation_id", "operations_queue", "operation_id"),
            "decision_unknown_provider": ("routing_decisions", "selected_payment_system_id", "providers", "payment_system_id"),
            "attempt_without_decision": ("routing_attempts", "operation_id", "routing_decisions", "operation_id"),
            "attempt_unknown_provider": ("routing_attempts", "payment_system_id", "providers", "payment_system_id"),
            "eligible_without_queue_operation": ("eligible_providers", "operation_id", "operations_queue", "operation_id"),
            "eligible_unknown_provider": ("eligible_providers", "payment_system_id", "providers", "payment_system_id"),
            "skip_without_queue_operation": ("provider_skip_reasons", "operation_id", "operations_queue", "operation_id"),
            "skip_unknown_provider": ("provider_skip_reasons", "payment_system_id", "providers", "payment_system_id"),
        }
        return {name: self.db.execute(f"SELECT COUNT(*) FROM {source} a LEFT JOIN {target} b ON a.{field}=b.{key} WHERE a.{field} IS NOT NULL AND b.{key} IS NULL").fetchone()[0] for name, (source, field, target, key) in relations.items()}

    def source_metadata(self):
        return {"type": "sqlite", "database": str(self.path), "integrity_check": self.db.execute("PRAGMA integrity_check").fetchone()[0],
                "foreign_key_definitions": sum(len(list(self.db.execute(f"PRAGMA foreign_key_list({table})"))) for table in TABLE_COLUMNS),
                "table_rows": {table: self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLE_COLUMNS}, "orphans": self.orphan_counts()}

    def analysis_inputs(self):
        return {"provider_data": self.provider_data(), "history_rows": self.history_rows(), "routing_events": self.routing_events(), "pending_operations": self.pending_operations(), "source_metadata": self.source_metadata()}


class Analyzer:
    def __init__(self, provider_data, history_rows, routing_events=(), pending_operations=(), source_metadata=None):
        self.provider_data, self.providers = provider_data, provider_data["providers"]
        self.history_rows, self.routing_events, self.pending_operations = list(history_rows), list(routing_events), list(pending_operations)
        self.source_metadata = source_metadata or {}
        self.by_name = {p["payment_system"]: p for p in self.providers}

    def normalized_history(self):
        return [{**{key: r.get(key) for key in ("operation_id", "created_at", "bank", "card_brand", "payment_system", "status")}, "amount": number(r.get("amount")), "latency_sec": number(r.get("latency_sec")), "source": "operations_history"} for r in self.history_rows]

    def latest_routing_events(self):
        result = {}
        for e in self.routing_events:
            key = e.get("operation_id") or e.get("operation", {}).get("operation_id")
            if key:
                result[key] = e
        return list(result.values())

    def combined_records(self, events):
        records = {r["operation_id"]: r for r in self.normalized_history()}
        for e in events:
            op, d = e.get("operation", {}), e.get("routing_decision", {})
            key = op.get("operation_id") or e.get("operation_id")
            records[key] = {"operation_id": key, "created_at": op.get("created_at") or e.get("logged_at"), "amount": number(op.get("amount")),
                            "bank": op.get("bank"), "card_brand": op.get("card_brand"), "payment_system": d.get("selected_provider"), "status": d.get("simulated_result") or "unknown", "latency_sec": number(d.get("latency_sec")), "source": "routing_decisions"}
        return sorted(records.values(), key=lambda r: ((parse_time(r["created_at"]).timestamp() if parse_time(r["created_at"]) else 0), str(r["operation_id"])))

    def unprocessed_pending_operations(self, events):
        processed = {r["operation_id"] for r in self.history_rows} | {e.get("operation_id") or e.get("operation", {}).get("operation_id") for e in events}
        return [r for r in self.pending_operations if r["operation_id"] not in processed]

    def distribution_for(self, records):
        names = list(dict.fromkeys([p["payment_system"] for p in self.providers] + [r["payment_system"] for r in records if r["payment_system"] is not None]))
        total = sum(r["amount"] or 0 for r in records)
        result = {}
        for name in names:
            p = self.by_name.get(name, {})
            own = [r for r in records if r["payment_system"] == name]
            count, amount = len(own), sum(r["amount"] or 0 for r in own)
            statuses = Counter(r["status"] or "unknown" for r in own)
            target, share = number(p.get("traffic_percentage")), percentage(count, len(records))
            conversion = number(p.get("conversion_24h"))
            result[name] = {"count": count, "share_pct": share, "target_pct": clean(target), "deviation_pp": clean(share - target) if share is not None and target is not None else None,
                            "amount": clean(amount), "volume_share_pct": percentage(amount, total), "target_volume_share_pct": number(p.get("volume_share_pct")),
                            **{key: statuses[key] for key in (*BASE_STATUSES, "unknown")}, "approval_rate_pct": percentage(statuses["approved"], count),
                            "snapshot_conversion_24h_pct": clean(conversion * 100) if conversion is not None else None, "latency": latency_stats([r["latency_sec"] for r in own])}
        return result

    def current_bank_rule_mismatch(self, row):
        p = self.by_name.get(row["payment_system"])
        if not p or not p.get("banks"):
            return False
        return row["bank"] in p["banks"] if p.get("exclude_banks") else row["bank"] not in p["banks"]

    def data_quality(self, records, events):
        history = self.normalized_history()
        times = [t for row in history if (t := parse_time(row["created_at"])) is not None]
        history_ids = [r["operation_id"] for r in self.history_rows]
        routing_ids = {e.get("operation_id") or e.get("operation", {}).get("operation_id") for e in events}
        snapshot = parse_time(self.provider_data.get("snapshot_at"))
        return {"history_rows": len(history), "routing_decision_rows": len(self.routing_events), "routing_unique_operations": len(routing_ids),
                "history_decision_overlap": len(set(history_ids) & routing_ids), "analyzed_unique_operations": len(records), "history_duplicate_operation_ids": len(history_ids) - len(set(history_ids) - {None}),
                "invalid_timestamp_count": sum(parse_time(r["created_at"]) is None for r in records), "invalid_amount_count": sum(r["amount"] is None or r["amount"] < 0 for r in records),
                "unknown_provider_count": sum(r["payment_system"] not in self.by_name for r in records), "unknown_status_count": sum(r["status"] not in BASE_STATUSES for r in records),
                "target_snapshot_after_history": bool(snapshot and times and snapshot.date() > max(times).date()), "blank_card_brand_count": sum(not r["card_brand"] for r in records),
                "history_timestamp_backward_transitions": sum(b < a for a, b in zip(times, times[1:])), "history_current_bank_rule_mismatch_count": sum(self.current_bank_rule_mismatch(r) for r in history),
                "database_integrity": self.source_metadata.get("integrity_check"), "database_table_rows": self.source_metadata.get("table_rows", {}), "database_orphans": self.source_metadata.get("orphans", {}), "foreign_key_definitions": self.source_metadata.get("foreign_key_definitions")}

    def report(self, generated_at=None):
        generated_at = generated_at or now()
        events = self.latest_routing_events()
        records, pending = self.combined_records(events), self.unprocessed_pending_operations(events)
        amounts = [n for r in pending if (n := number(r.get("amount"))) is not None]
        pending_queue = {"count": len(pending), "total_amount": clean(sum(amounts)), "min_amount": clean(min(amounts)) if amounts else None, "max_amount": clean(max(amounts)) if amounts else None,
                         **window(pending), "banks": dict(sorted(Counter(r.get("bank") or "unknown" for r in pending).items()))}
        daily = {}
        for day, group in sorted(group_by(records, lambda r: parse_time(r["created_at"]).strftime("%Y-%m-%d") if parse_time(r["created_at"]) else "unknown").items()):
            total = sum(r["amount"] or 0 for r in group)
            daily[day] = {"total_operations": len(group), "total_amount": clean(total), "providers": {name: {"count": len(own), "share_pct": percentage(len(own), len(group)), "amount": clean(sum(r["amount"] or 0 for r in own)), "volume_share_pct": percentage(sum(r["amount"] or 0 for r in own), total)} for name, own in sorted(group_by(group, lambda r: r["payment_system"] or "unknown").items())}}
        counts = Counter(r["status"] or "unknown" for r in records)
        status_summary = {s: {"count": counts[s], "share_pct": percentage(counts[s], len(records))} for s in dict.fromkeys([*BASE_STATUSES, *counts])}
        latency = latency_stats([r["latency_sec"] for r in records])
        latency["by_status"] = {s: latency_stats([r["latency_sec"] for r in group]) for s, group in sorted(group_by(records, lambda r: r["status"] or "unknown").items())}
        utilization, state = {}, {}
        for p in self.providers:
            name = p["payment_system"]
            used, limit = number(p.get("daily_approved_amount")) or 0, number(p.get("daily_amount_limit"))
            utilization[name] = {"used": clean(used), "limit": clean(limit), "remaining": clean(limit - used) if limit is not None else None, "utilization_pct": percentage(used, limit)}
            state[name] = {k: p.get(k) for k in ("status", "priority", "available_requisites")}
            for metric in ("in_progress_count", "in_progress_amount"):
                used, limit = number(p.get(metric)) or 0, number(p.get(metric + "_limit"))
                state[name].update({metric: clean(used), metric + "_limit": clean(limit), metric + "_utilization_pct": percentage(used, limit)})
        skips = Counter(a.get("reason") or "unknown" for e in events for a in e.get("routing_decision", {}).get("attempts", []) if a.get("decision") == "skipped")
        result = {"period": period_label(records), "window": window(records), "generated_at": iso(generated_at), "source": self.source_metadata,
                  "provider_snapshot_at": self.provider_data.get("snapshot_at"), "gateway": self.provider_data.get("gateway"), "merchant": self.provider_data.get("merchant"),
                  "total_operations": len(records), "pending_operations": len(pending), "all_operations_seen": len(records) + len(pending), "total_amount": clean(sum(r["amount"] or 0 for r in records)),
                  "pending_queue": pending_queue, "distribution": self.distribution_for(records), "daily_distribution": daily, "status_summary": status_summary, "latency": latency,
                  "skip_reasons": dict(sorted(skips.items())), "projected_daily_utilization": utilization, "provider_state": state, "data_quality": self.data_quality(records, events), "recommendation_period": period_label(latest_day_records(records))}
        result["recommendations"] = self.recommendations_for(result, generated_at)
        return result

    def recommendations_for(self, report, generated_at, details=None):
        from .recommendations import Recommendations
        records = self.combined_records(self.latest_routing_events())
        return Recommendations(self.providers, latest_day_records(records), records, report, generated_at, details or {}, self.provider_data.get("snapshot_at")).call()


class CanonicalDatabaseAnalytics:
    def __init__(self, path):
        self.source = CanonicalDatabaseSource(path)

    def close(self):
        self.source.close()

    def report(self, generated_at=None):
        from .report_details import CanonicalReportDetails
        at = generated_at or now()
        with transaction(self.source.db):
            inputs = self.source.analysis_inputs()
            analyzer = Analyzer(**inputs)
            result = analyzer.report(at)
            result["skip_reasons"] = self.source.skip_reason_counts()
            for p in inputs["provider_data"]["providers"]:
                stats = {k: p[k] for k in self.source.PROVIDER_STATS_COLUMNS if k in p}
                if stats:
                    result["provider_state"][p["payment_system"]]["minute_stats"] = stats
            result["source"]["definitions"] = {
                "provider_snapshot": "Время полного снимка состояния провайдеров неизвестно; stats_calculated_at указывает время только для пересчитанных метрик.",
                "minute_stats": "Сохранённые метрики: окно заканчивается в stats_calculated_at, stats_window_sec=null означает всю историю; RPM — 60 секунд, conversion_24h — 24 часа. При формировании отчёта метрики не пересчитываются.",
                "in_progress": "Обновление статистики не меняет сохранённые in_progress_count/amount.",
                "skip_reasons": "Уникальные сочетания операции, провайдера и причины из routing_attempts и provider_skip_reasons."}
            details = self.source.detail_inputs()
            result.update(CanonicalReportDetails(details=details, **inputs).sections(at))
            result["recommendations"] = analyzer.recommendations_for(result, at, details)
            return result


class ReportWriter:
    write = staticmethod(write_json)
