from collections import Counter
from datetime import timedelta
import json
from .analytics import Analyzer, group_by, latency_stats, latest_day_records, period_label, window
from .utils import UTC, clean, iso, number, parse_time, percentage

AMOUNT_BANDS = [("0_1000", 0, 1000), ("1000_10000", 1000, 10000), ("10000_50000", 10000, 50000), ("50000_100000", 50000, 100000), ("100000_plus", 100000, None)]
FAILURE_REASONS = ["provider_timeout", "provider_rejected", "provider_expired", "payout_failed", "provider_unavailable"]
FALLBACK_REASONS = ["fallback", "fallback_candidate", "fallback_selected", "self_provider_fallback", "fallback_to_self_provider", "fallback_to_spacepayments"]


def valid_amount(value):
    n = number(value)
    return n if n is not None and n >= 0 else None


def metric_summary(records):
    amounts = [n for r in records if (n := valid_amount(r["amount"])) is not None]
    statuses = Counter(r["status"] or "unknown" for r in records)
    return {"count": len(records), "amount": None if records and not amounts else clean(sum(amounts)), "invalid_amount_count": len(records) - len(amounts),
            "statuses": dict(sorted(statuses.items())), "approval_rate_pct": percentage(statuses["approved"], len(records)), "latency": latency_stats([valid_amount(r["latency_sec"]) for r in records]), "sources": dict(sorted(Counter(r["source"] for r in records).items()))}


def cohort_summary(records):
    result = metric_summary(records)
    providers = {}
    for name, own in sorted(group_by(records, lambda r: r["payment_system"] or "unknown").items()):
        metrics = metric_summary(own)
        providers[name] = {**metrics, "share_pct": percentage(len(own), len(records)), "volume_share_pct": percentage(metrics["amount"], result["amount"])}
    return {**result, "providers": providers}


def metric_changes(current, previous):
    result = {}
    pairs = {"count": (current["count"], previous["count"]), "amount": (current["amount"], previous["amount"]),
             "approval_rate": (current["approval_rate_pct"], previous["approval_rate_pct"]), "avg_latency_sec": (current["latency"]["avg_sec"], previous["latency"]["avg_sec"]), "p95_latency_sec": (current["latency"]["p95_sec"], previous["latency"]["p95_sec"])}
    for name, (a, b) in pairs.items():
        delta = clean(a - b) if a is not None and b is not None else None
        result[name] = {"delta_pp": delta} if name == "approval_rate" else {"delta": delta, "change_pct": percentage(delta, b)}
    return result


class CanonicalReportDetails(Analyzer):
    def __init__(self, details, **inputs):
        super().__init__(**inputs)
        self.details, self.decisions = details, details["decisions"]
        self.decisions_by_id = {d["operation_id"]: d for d in self.decisions}
        self.providers_by_id = {p["payment_system_id"]: p["payment_system"] for p in self.providers}
        self.attempt_groups = group_by(details["attempts"], lambda r: r["operation_id"])

    def sections(self, generated_at):
        self.records = self.combined_records(self.latest_routing_events())
        self.generated_at = generated_at.astimezone(UTC)
        cascades = self.attempt_cascades()
        return {"routing_coverage": self.routing_coverage(cascades), "attempt_cascades": cascades, "segments": self.segments(), "period_comparison": self.period_comparison(),
                "skip_reason_sources": self.skip_reason_sources(), "routing_explanations": {d["operation_id"]: json.loads(d["explanation"]) if d.get("explanation") else None for d in self.decisions}, "freshness": self.freshness()}

    def routing_coverage(self, cascades):
        queue = {r["operation_id"] for r in self.pending_operations}
        decisions = set(self.decisions_by_id)
        population = len(queue | decisions)
        return {"definition": "Уникальные операции из operations_queue и routing_decisions; операции только из истории исключены.", "status": "no_operations" if not population else ("no_decisions" if not self.decisions else "available"),
                "operations_in_scope": population, "queue_operations": len(queue), "with_decision": len(decisions), "awaiting_decision": len(queue - decisions), "decision_coverage_pct": percentage(len(decisions), population),
                "without_selected_provider": sum(d["selected_payment_system_id"] is None for d in self.decisions),
                "unknown_selected_provider": sum(d["selected_payment_system_id"] is not None and d["selected_payment_system_id"] not in self.providers_by_id for d in self.decisions),
                "with_known_selected_provider": sum(d["selected_payment_system_id"] in self.providers_by_id for d in self.decisions),
                "fallback_operations": cascades["fallback_operations"], "fallback_share_of_decisions_pct": percentage(cascades["fallback_operations"], len(decisions)), "fallback_unclassified_operations": cascades["unclassified_operations"]}

    def cascade_classification(self, decision, attempts):
        selected = [r for r in attempts if r["decision"] == "selected"]
        final = decision["selected_payment_system_id"]
        explicit = any(final in self.providers_by_id and r["payment_system_id"] == final and r["reason"] in FALLBACK_REASONS for r in selected)
        if self.providers_by_id.get(final) == "spacepayments" or explicit:
            return "fallback"
        numbers = [r["attempt_number"] for r in attempts]
        ordered = bool(attempts) and all(type(n) is int and n > 0 for n in numbers) and sorted(numbers) == list(range(1, len(attempts) + 1))
        if not ordered or final not in self.providers_by_id or not selected or selected[-1]["payment_system_id"] != final:
            return "unclassified"
        if len(selected) > 1 or any(r["attempt_number"] < selected[-1]["attempt_number"] and r["reason"] in FAILURE_REASONS for r in attempts):
            return "fallback"
        return "first_choice" if all(r["decision"] in ("selected", "skipped") for r in attempts) else "unclassified"

    def attempt_cascades(self):
        classified = [(d, self.attempt_groups[d["operation_id"]], self.cascade_classification(d, self.attempt_groups[d["operation_id"]])) for d in self.decisions]
        first = [d for d, _, k in classified if k == "first_choice"]
        fallback = [d for d, _, k in classified if k == "fallback"]
        with_attempts = [a for _, a, _ in classified if a]
        recorded = [r for a in with_attempts for r in a]
        approved = lambda group: sum(d["simulated_result"] == "approved" for d in group)
        return {"status": "no_decisions" if not self.decisions else ("no_attempt_logs" if not with_attempts else "available_with_caveats"),
                "definitions": {"attempt_count": "Сохранённые шаги оценки, включая пропуски; это не количество HTTP-запросов.", "first_choice": "Единственный итоговый выбор без зарегистрированного сбоя или резервного перехода.",
                                "fallback": "spacepayments, явная причина резерва, несколько выборов или сбой до итогового выбора.", "failure_reasons": FAILURE_REASONS, "explicit_fallback_reasons": FALLBACK_REASONS,
                                "success": "simulated_result = approved; исход отдельной попытки и происхождение данных не сохраняются.", "first_attempt_success_pct": "Одобренные операции первого выбора / все классифицированные операции.", "cohort_approval_pct": "Одобренные операции / все операции соответствующей группы."},
                "operations_with_attempt_logs": len(with_attempts), "operations_without_attempt_logs": len(self.decisions) - len(with_attempts), "attempt_log_coverage_pct": percentage(len(with_attempts), len(self.decisions)),
                "recorded_attempts": len(recorded), "average_recorded_attempts": clean(len(recorded) / len(with_attempts)) if with_attempts else None,
                "attempt_count_distribution": {str(n): count for n, count in sorted(Counter(len(a) for a in with_attempts).items())}, "attempt_decisions": dict(sorted(Counter(r["decision"] or "unknown" for r in recorded).items())),
                "orphan_attempt_rows": sum(r["operation_id"] not in self.decisions_by_id for r in self.details["attempts"]),
                "first_choice_operations": len(first), "first_choice_approved": approved(first), "first_choice_approval_pct": percentage(approved(first), len(first)), "first_attempt_success_pct": percentage(approved(first), len(first) + len(fallback)),
                "fallback_operations": len(fallback), "fallback_approved": approved(fallback), "fallback_approval_pct": percentage(approved(fallback), len(fallback)), "fallback_statuses": dict(sorted(Counter(d["simulated_result"] or "unknown" for d in fallback).items())),
                "unclassified_operations": sum(k == "unclassified" for _, _, k in classified), "classification_coverage_pct": percentage(len(first) + len(fallback), len(self.decisions)), "network_attempt_count": None}

    def segments(self):
        amounts = {label: [] for label, _, _ in AMOUNT_BANDS}
        amounts["unknown"] = []
        for row in self.records:
            amount = valid_amount(row["amount"])
            label = next((label for label, lower, upper in AMOUNT_BANDS if amount is not None and amount >= lower and (upper is None or amount < upper)), "unknown")
            amounts[label].append(row)
        return {"definition": "Та же выборка без дубликатов, что и total_operations; решения имеют приоритет. Доли провайдеров внутри сегмента. Некорректные суммы исключены из суммирования.",
                "window": window(self.records, utc=True, detailed=True), "amount_band_boundaries": {label: {"min_inclusive": lower, "max_exclusive": upper} for label, lower, upper in AMOUNT_BANDS},
                "by_bank": {bank: cohort_summary(group) for bank, group in sorted(group_by(self.records, lambda r: r["bank"] if r["bank"] and r["bank"].strip() else "unknown").items())}, "by_amount": {label: cohort_summary(group) for label, group in amounts.items()}}

    def period_comparison(self):
        dated = [(t.astimezone(UTC), r) for r in self.records if (t := parse_time(r["created_at"])) is not None and t < self.generated_at]
        result = {"timezone": "UTC", "definition": "Последний день с наблюдениями сравнивается с непосредственно предыдущим. Для текущего дня используются одинаковые по длительности интервалы до generated_at.",
                  "caveats": ["Охват и полнота источников не фиксируются.", "Используются сохранённые текущие статусы с приоритетом решений над историей."],
                  "excluded_invalid_timestamp_count": sum(parse_time(r["created_at"]) is None for r in self.records), "excluded_at_or_after_generated_at_count": sum(t >= self.generated_at for r in self.records if (t := parse_time(r["created_at"])) is not None),
                  "status": "no_dated_operations", "partial_day": None, "current": None, "previous": None, "changes": None, "provider_changes": {}}
        if not dated:
            return result
        start = max(t for t, _ in dated).replace(hour=0, minute=0, second=0, microsecond=0)
        end = min(start + timedelta(days=1), self.generated_at)
        groups = []
        for lower, upper in [(start, end), (start - timedelta(days=1), end - timedelta(days=1))]:
            groups.append({**cohort_summary([r for t, r in dated if lower <= t < upper]), "from_inclusive": iso(lower, True), "to_exclusive": iso(upper, True), "duration_sec": clean((upper - lower).total_seconds())})
        current, previous = groups
        result.update(current=current, previous=previous, partial_day=end < start + timedelta(days=1))
        if not previous["count"]:
            result["status"] = "no_previous_period_observations"
            return result
        result.update(status="available_with_caveats", changes=metric_changes(current, previous))
        for name in sorted(current["providers"].keys() | previous["providers"].keys()):
            a, b = [p["providers"].get(name) or {**metric_summary([]), "share_pct": 0, "volume_share_pct": 0 if p["amount"] else None} for p in groups]
            changes = metric_changes(a, b)
            for key in ("share_pct", "volume_share_pct"):
                changes[key.removesuffix("_pct") + "_delta_pp"] = clean(a[key] - b[key]) if a[key] is not None and b[key] is not None else None
            result["provider_changes"][name] = changes
        return result

    def skip_reason_sources(self):
        keys = lambda source: {(r["operation_id"], r["payment_system_id"], r["reason"] or "unknown") for r in source}
        observed = keys([r for r in self.details["attempts"] if r["decision"] == "skipped"])
        stored = keys(self.details["stored_skips"])
        summary = lambda group: {"distinct_skips": len(group), "reasons": dict(sorted(Counter(r[2] for r in group).items()))}
        return {"definition": "Уникальные сочетания операции, провайдера и причины; источники пересекаются и не суммируются. Справочные пропуски не считаются фактическими попытками.",
                "routing_attempts": summary(observed), "stored_reference_or_unclassified": summary(stored), "overlap_count": len(observed & stored), "stored_only_count": len(stored - observed), "combined_distinct_count": len(observed | stored)}

    def source_freshness(self, records, field="created_at"):
        w = window(records, field, utc=True, detailed=True)
        latest = parse_time(w["to"])
        return {**w, "rows": len(records), "timestamp_field": field, "last_observation_age_sec": clean((self.generated_at - latest).total_seconds()) if latest else None,
                "future_timestamp_count": sum(t > self.generated_at for r in records if (t := parse_time(r.get(field))) is not None), "completeness_verified": False}

    def freshness(self):
        metric_windows = {}
        for p in self.providers:
            at = parse_time(p.get("stats_calculated_at"))
            seconds = valid_amount(p.get("stats_window_sec")) or None
            metric_windows[p["payment_system"]] = {
                "analysis_from_exclusive": iso(at - timedelta(seconds=seconds), True) if at and seconds else None, "analysis_to_inclusive": iso(at, True), "analysis_window_mode": ("rolling" if seconds else "all_time") if at else None,
                "minute_from_exclusive": iso(at - timedelta(seconds=60), True) if at else None, "minute_to_inclusive": iso(at, True), "window_sec": seconds,
                "conversion_24h_from_exclusive": iso(at - timedelta(days=1), True) if at else None, "last_calculation_age_sec": clean((self.generated_at - at).total_seconds()) if at else None}
        return {"evaluated_at": iso(self.generated_at, True), "timezone": "UTC", "definition": "Давность сохранённых событий. Даты без часового пояса интерпретируются как UTC. Полнота источников не проверена.",
                "sources": {"operations_history": self.source_freshness(self.history_rows), "operations_queue": self.source_freshness(self.pending_operations), "routing_decisions": self.source_freshness(self.decisions),
                            "routing_attempts": self.source_freshness(self.details["attempts"]), "provider_skip_reasons": self.source_freshness(self.details["stored_skips"]), "providers": {"as_of": None, "reason": "Время полного снимка не сохраняется."}},
                "provider_metric_windows": metric_windows,
                "blocks": {"distribution_status_latency_segments": {"source": "operations_history и routing_decisions; приоритет решений", "window": window(self.records, utc=True, detailed=True), "outcome_provenance": "Происхождение результатов не сохраняется."},
                           "pending_queue": {"source": "operations_queue без истории и решений", "window": window(self.unprocessed_pending_operations(self.latest_routing_events()), utc=True, detailed=True)},
                           "routing_coverage_cascades": {"source": "Очередь, исходные решения и попытки", "decision_window": window(self.decisions, utc=True, detailed=True)},
                           "skip_reasons": {"source": "Уникальное объединение routing_attempts и provider_skip_reasons", "window": window(self.details["stored_skips"] + [r for r in self.details["attempts"] if r["decision"] == "skipped"], utc=True, detailed=True)},
                           "provider_state_utilization_recommendations": {"source": "Сохранённые поля провайдеров; актуальность полного снимка неизвестна", "as_of": None, "recommendation_period": period_label(latest_day_records(self.records))},
                           "period_comparison": {"source": "Та же выборка без дубликатов; окна UTC указаны в period_comparison"}}}
