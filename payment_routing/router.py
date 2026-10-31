from dataclasses import dataclass
from datetime import timedelta, timezone
from .config import StrategyWeightCalculator
from .hard_filter import Engine
from .rating import ProviderScoreCalculator
from .utils import iso, now, share


@dataclass
class Attempt:
    provider: str
    decision: str
    reason: str | None
    details: dict | None = None
    dispatched_at: object = None

    def to_dict(self):
        result = {"provider": self.provider, "decision": self.decision, "reason": self.reason}
        if self.details is not None:
            result["details"] = self.details
        if self.dispatched_at is not None:
            result["dispatched_at"] = iso(self.dispatched_at, True)
        return result


@dataclass
class Decision:
    operation_id: str
    selected_provider: str
    attempts: list
    simulated_result: str
    latency_sec: int
    explanation: dict | None = None

    def to_dict(self):
        result = {"operation_id": self.operation_id, "selected_provider": self.selected_provider,
                  "attempts": [a.to_dict() for a in self.attempts], "simulated_result": self.simulated_result,
                  "latency_sec": self.latency_sec}
        if self.explanation is not None:
            result["explanation"] = self.explanation
        return result


class ProviderClient:
    class UnavailableError(Exception):
        pass

    def attempt(self, provider, operation):
        pass


class OutcomeSimulator:
    APPROVED = "approved"

    def simulate(self, provider, operation):
        return self.APPROVED


class RunState:
    def __init__(self, providers, actuals_by_provider):
        self.providers_by_name = {p.payment_system: p for p in providers}
        self.actuals_by_provider = dict(actuals_by_provider)
        self.time = None

    @property
    def providers(self):
        return list(self.providers_by_name.values())

    def provider(self, name):
        return self.providers_by_name[name]

    def actuals(self, name):
        return self.actuals_by_provider[name]

    def replace_provider(self, provider):
        self.providers_by_name[provider.payment_system] = provider

    def replace_actuals(self, name, actuals):
        self.actuals_by_provider[name] = actuals

    def advance_to(self, time):
        if self.time and time < self.time:
            raise ValueError("operation time moved backwards")
        self.time = time
        for p in self.providers:
            day = time.astimezone(timezone(timedelta(seconds=p.daily_utc_offset))).strftime("%Y-%m-%d")
            previous = p.daily_approved_date
            if previous and day < previous:
                raise ValueError(f"operation precedes daily snapshot for {p.payment_system}")
            amount = 0.0 if previous and day > previous else float(p.daily_approved_amount or 0)
            self.replace_provider(p.with_updates(daily_approved_date=day, daily_approved_amount=amount))
            self.replace_actuals(p.payment_system, self.actuals(p.payment_system).with_updates(turnover_actual=amount))
        for name, a in list(self.actuals_by_provider.items()):
            times = a.request_times if a.request_times is not None else [time] * int(a.rpm_used)
            times = [t for t in times if t > time - timedelta(seconds=60)]
            self.replace_actuals(name, a.with_updates(request_times=times, rpm_used=sum(t <= time for t in times)))

    def record_request(self, name):
        a = self.actuals(name)
        self.replace_actuals(name, a.with_updates(request_times=(a.request_times or []) + [self.time], rpm_used=a.rpm_used + 1))


class MetricsUpdater:
    def start_attempt(self, state, provider, operation):
        p = state.provider(provider.payment_system)
        state.record_request(p.payment_system)
        state.replace_provider(p.with_updates(in_progress_count=(p.in_progress_count or 0) + 1, in_progress_amount=(p.in_progress_amount or 0) + operation.amount))

    def finish_attempt(self, state, provider, operation):
        p = state.provider(provider.payment_system)
        state.replace_provider(p.with_updates(in_progress_count=p.in_progress_count - 1, in_progress_amount=p.in_progress_amount - operation.amount))

    def apply(self, state, provider, operation, simulated_result, rated_payment_systems=()):
        if simulated_result != "approved":
            return
        name = provider.payment_system
        p, a = state.provider(name), state.actuals(name)
        state.replace_provider(p.with_updates(daily_approved_amount=p.daily_approved_amount + operation.amount))
        state.replace_actuals(name, a.with_updates(turnover_actual=a.turnover_actual + operation.amount, count_actual=a.count_actual + 1, volume_actual=a.volume_actual + operation.amount))
        total_count = sum(a.count_actual for a in state.actuals_by_provider.values())
        total_volume = sum(a.volume_actual for a in state.actuals_by_provider.values())
        for name, a in list(state.actuals_by_provider.items()):
            state.replace_actuals(name, a.with_updates(count_share_actual=share(a.count_actual, total_count), volume_share_actual=share(a.volume_actual, total_volume)))


class Router:
    def __init__(self, state, rated_payment_systems, fallback_payment_system, strategy_registry, active_strategies,
                 hard_filter_engine=None, provider_client=None, outcome_simulator=None, metrics_updater=None, clock=now):
        self.state, self.rated = state, rated_payment_systems
        self.fallback = fallback_payment_system
        self.weight_calculator = StrategyWeightCalculator(strategy_registry)
        self.active = active_strategies
        self.engine = hard_filter_engine or Engine()
        self.client = provider_client or ProviderClient()
        self.simulator = outcome_simulator or OutcomeSimulator()
        self.updater = metrics_updater or MetricsUpdater()
        self.clock = clock

    def route_all(self, operations):
        return [self.route(operation) for operation in operations]

    def route(self, operation):
        state = self.state
        state.advance_to(operation.created_at or self.clock())
        attempts, eligible = [], []
        explanation = {"eligibility": [], "ranking": []}
        for p, result in self.engine.call_all([state.provider(name) for name in self.rated], operation, state.actuals_by_provider):
            explanation["eligibility"].append({"provider": p.payment_system, "is_eligible": result.eligible, "reasons": result.reasons, "details": result.details})
            if result.eligible:
                eligible.append(p)
            else:
                attempts.append(Attempt(p.payment_system, "skipped", result.reasons[0], {**result.details, "reasons": result.reasons}))
        selected = None
        if eligible:
            parameters = self.weight_calculator.call(self.active)
            ranked = ProviderScoreCalculator(**parameters).rank(eligible, state.actuals_by_provider, operation)
            explanation.update(active_strategies=self.active, **parameters,
                               selection_policy="descending weighted score times load factor; ties follow rated_providers order",
                               ranking=[{"provider": row.provider.payment_system, "score": row.score, "norms": row.breakdown, "load_factor": row.load_factor} for row in ranked])
            for row in ranked:
                p = row.provider
                self.updater.start_attempt(state, p, operation)
                try:
                    try:
                        self.client.attempt(provider=p, operation=operation)
                    finally:
                        self.updater.finish_attempt(state, p, operation)
                except ProviderClient.UnavailableError:
                    attempts.append(Attempt(p.payment_system, "skipped", "provider_unavailable", {"result": "provider unavailable during dispatch"}, state.time))
                    continue
                reason = "only_eligible_provider" if len(ranked) == 1 else "highest_score"
                details = next(r for r in explanation["ranking"] if r["provider"] == p.payment_system)
                attempts.append(Attempt(p.payment_system, "selected", reason, details, state.time))
                selected = p
                break
        if selected is None:
            selected = state.provider(self.fallback)
            reason = "all_providers_unavailable" if eligible else "no_eligible_provider"
            attempts.append(Attempt(selected.payment_system, "selected", reason))
        # Preserve the original reserve policy: fallback bypasses hard constraints.
        if selected.payment_system == self.fallback:
            state.record_request(selected.payment_system)
            attempts[-1].dispatched_at = state.time
            attempts[-1].details = {"selection": attempts[-1].reason}
        result = self.simulator.simulate(provider=selected, operation=operation)
        self.updater.apply(state, selected, operation, result, self.rated)
        return Decision(operation.operation_id, selected.payment_system, attempts, result, selected.avg_latency_sec or 0, explanation)
