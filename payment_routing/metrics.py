from collections import defaultdict
from math import ceil
from statistics import median
from .utils import clean, percentage

TERMINAL_STATUSES = ("approved", "rejected", "expired")
AMOUNT_BANDS = [("0_to_999", 0, 1000), ("1000_to_9999", 1000, 10000), ("10000_to_49999", 10000, 50000), ("50000_to_99999", 50000, 100000), ("100000_and_above", 100000, None)]


def pct(numerator, denominator):
    return percentage(numerator, denominator, 4) if numerator is not None and denominator is not None and denominator > 0 else None


def latency(values):
    values = sorted(value for value in values if value is not None)
    return {"count": len(values), "avg_sec": clean(sum(values) / len(values), 4) if values else None,
            "median_sec": median(values) if values else None, "p95_sec": values[ceil(len(values) * .95) - 1] if values else None}


def summary(rows):
    count, amount = len(rows), sum(row["amount"] for row in rows)
    statuses = {}
    for status in sorted(set(TERMINAL_STATUSES) | {r.get("status") or "unknown" for r in rows}):
        selected = [row for row in rows if (row.get("status") or "unknown") == status]
        subtotal = sum(row["amount"] for row in selected)
        statuses[status] = {"count": len(selected), "amount": subtotal, "count_pct": pct(len(selected), count), "amount_pct": pct(subtotal, amount)}
    terminal_count = sum(statuses[s]["count"] for s in TERMINAL_STATUSES)
    return {"count": count, "amount": amount, "average_amount": clean(amount / count, 4) if count else None,
            "min_amount": min((r["amount"] for r in rows), default=None), "max_amount": max((r["amount"] for r in rows), default=None),
            "statuses": statuses, "approval_pct": statuses["approved"]["count_pct"], "rejection_pct": statuses["rejected"]["count_pct"],
            "expiration_pct": statuses["expired"]["count_pct"], "approved_amount_pct": statuses["approved"]["amount_pct"],
            "terminal_count": terminal_count, "terminal_approval_pct": pct(statuses["approved"]["count"], terminal_count),
            "latency": latency([r["latency_sec"] for r in rows]), "approved_latency": latency([r["latency_sec"] for r in rows if r["status"] == "approved"])}


def shares(metrics, total):
    return {**metrics, "count_share_pct": pct(metrics["count"], total["count"]), "amount_share_pct": pct(metrics["amount"], total["amount"]),
            "approved_amount_share_pct": pct(metrics["statuses"]["approved"]["amount"], total["statuses"]["approved"]["amount"])}


def breakdown(rows, total):
    groups = defaultdict(list)
    for row in rows:
        groups[row["bank"]].append(row)
    return {"banks": [{**shares(summary(group), total), "bank": bank} for bank, group in sorted(groups.items(), key=lambda item: str(item[0] or ""))],
            "amount_bands": [{**shares(summary([r for r in rows if r["amount"] >= lower and (upper is None or r["amount"] < upper)]), total),
                              "band": label, "min_inclusive": lower, "max_exclusive": upper} for label, lower, upper in AMOUNT_BANDS]}


def gap(actual, target):
    return clean(actual - target, 4) if actual is not None and target is not None else None


def remaining(limit, used):
    return limit - used if limit is not None and limit > 0 and used is not None else None


def targets(p, minute):
    return {"target_count_share_pct": p.get("traffic_percentage"), "count_share_gap_pp": gap(minute["count_share_pct"], p.get("traffic_percentage")),
            "target_amount_share_pct": p.get("volume_share_pct"), "amount_share_gap_pp": gap(minute["amount_share_pct"], p.get("volume_share_pct")),
            "requests_limit": p.get("requests_per_minute_limit"), "requests_limit_utilization_pct": pct(minute["count"], p.get("requests_per_minute_limit")),
            "requests_limit_remaining": remaining(p.get("requests_per_minute_limit"), minute["count"]),
            "snapshot_daily_approved_amount": p.get("daily_approved_amount"),
            "snapshot_daily_limit_utilization_pct": pct(p.get("daily_approved_amount"), p.get("daily_amount_limit")),
            "snapshot_daily_limit_remaining": remaining(p.get("daily_amount_limit"), p.get("daily_approved_amount")),
            "snapshot_daily_minimum_fulfilled_pct": pct(p.get("daily_approved_amount"), p.get("daily_turnover_min")),
            "snapshot_daily_maximum_utilization_pct": pct(p.get("daily_approved_amount"), p.get("daily_turnover_max"))}
