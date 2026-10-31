from dataclasses import dataclass
from .utils import clip


def deviation_norm(target, actual):
    return (clip((target - actual) / target, -1, 1) + 1) / 2.0 if target else 1.0


def min_max_norm(value, lower, upper):
    return (value - lower) / (upper - lower) if lower != upper else 1.0


class LoadFactorCalculator:
    def utilization(self, provider, actuals):
        ratios = [used / limit for used, limit in [
            (actuals.rpm_used, provider.requests_per_minute_limit),
            (provider.in_progress_count or 0, provider.in_progress_count_limit),
            (provider.in_progress_amount or 0, provider.in_progress_amount_limit),
        ] if limit]
        return clip(max(ratios, default=0))

    def rpm_utilization(self, provider, actuals):
        return clip(actuals.rpm_used / provider.requests_per_minute_limit) if provider.requests_per_minute_limit else 0.0

    def load_factor(self, utilization, gamma):
        return (1 - utilization) ** gamma


@dataclass
class ScoreResult:
    provider: object
    score: float
    breakdown: dict
    load_factor: float


class ProviderScoreCalculator:
    def __init__(self, weights, gamma, load_factor_calculator=None):
        self.weights, self.gamma = weights, gamma
        self.load_calculator = load_factor_calculator or LoadFactorCalculator()

    def rank(self, providers, actuals_by_provider, operation):
        if not providers:
            return []
        priorities = [p.priority for p in providers]
        conversions = [p.conversion_24h for p in providers if p.conversion_24h is not None]
        results = []
        for p in providers:
            a = actuals_by_provider[p.payment_system]
            preferred = p.preferred_range
            norms = {
                "count_share": deviation_norm(p.traffic_percentage, a.count_share_actual),
                "volume_share": deviation_norm(p.volume_share_pct, a.volume_share_actual),
                "priority": min_max_norm(max(priorities) - p.priority, 0, max(priorities) - min(priorities)),
                "range_fit": clip(1 - abs(operation.amount - preferred.mid) / preferred.halfwidth) if preferred and preferred.halfwidth else 1.0,
                "conversion": min_max_norm(p.conversion_24h, min(conversions), max(conversions)) if p.conversion_24h is not None else 0.5,
                "intensity": 1.0 - self.load_calculator.rpm_utilization(p, a),
                "turnover": deviation_norm(p.daily_turnover_min, a.turnover_actual) if p.daily_turnover_min else 0.5,
            }
            factor = self.load_calculator.load_factor(self.load_calculator.utilization(p, a), self.gamma)
            score = 100 * sum(self.weights.get(key, 0) * value for key, value in norms.items()) * factor
            results.append(ScoreResult(p, score, norms, factor))
        return sorted(results, key=lambda row: -row.score)
