"""All hard constraints are evaluated; reason order is part of the JSON contract."""
from dataclasses import dataclass

DETAIL_FIELDS = "status limit_amount_min limit_amount_max daily_amount_limit daily_approved_amount in_progress_count in_progress_count_limit in_progress_amount in_progress_amount_limit banks exclude_banks available_requisites provider_margin_pct merchant_margin_pct allow_negative_agreement requests_per_minute_limit daily_turnover_max".split()


@dataclass
class Result:
    reasons: list
    details: dict

    @property
    def eligible(self):
        return not self.reasons


class Engine:
    def call(self, provider, operation, actuals):
        p, o, a = provider, operation, actuals
        checks = [
            (p.status != "active", "status_not_active"),
            (p.limit_amount_min is not None and o.amount < p.limit_amount_min, "amount_below_minimum"),
            (p.limit_amount_max is not None and o.amount > p.limit_amount_max, "amount_exceeds_limit"),
            (p.daily_amount_limit is not None and (p.daily_approved_amount or 0) + o.amount > p.daily_amount_limit, "daily_amount_limit_exceeded"),
            ((p.in_progress_count_limit is not None and p.in_progress_count >= p.in_progress_count_limit) or
             (p.in_progress_amount_limit is not None and (p.in_progress_amount or 0) + o.amount > p.in_progress_amount_limit), "in_progress_limit_exceeded"),
            (bool(p.banks) and ((o.bank in p.banks) if p.exclude_banks else (o.bank not in p.banks)), "bank_not_in_list"),
            (not p.allow_negative_agreement and p.provider_margin_pct is not None and p.merchant_margin_pct is not None and p.provider_margin_pct > p.merchant_margin_pct, "margin_not_acceptable"),
            (p.available_requisites == 0, "no_available_requisites"),
            (p.requests_per_minute_limit is not None and a.rpm_used >= p.requests_per_minute_limit, "rate_limit_exceeded"),
            (p.daily_turnover_max is not None and a.turnover_actual + o.amount > p.daily_turnover_max, "daily_turnover_max_exceeded"),
        ]
        reasons = [reason for failed, reason in checks if failed]
        # AmountRangeRule returns at most one reason, even for inverted bounds.
        if "amount_below_minimum" in reasons and "amount_exceeds_limit" in reasons:
            reasons.remove("amount_exceeds_limit")
        return Result(reasons, {
            "operation": {"amount": o.amount, "bank": o.bank},
            "provider": {key: getattr(p, key) for key in DETAIL_FIELDS},
            "actuals": {"rpm_used": a.rpm_used, "turnover_actual": a.turnover_actual},
        })

    def call_all(self, providers, operation, actuals_by_provider):
        return [(p, self.call(p, operation, actuals_by_provider[p.payment_system])) for p in providers]
