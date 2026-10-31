import unittest
from datetime import timedelta
from payment_routing.config import StrategyRegistry, StrategyWeightCalculator
from payment_routing.hard_filter import Engine
from payment_routing.models import AmountRange, Operation, Provider, ProviderActuals
from payment_routing.rating import LoadFactorCalculator, ProviderScoreCalculator, deviation_norm
from payment_routing.router import MetricsUpdater, ProviderClient, Router, RunState
from payment_routing.utils import parse_time


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.p = Provider("p", traffic_percentage=50, volume_share_pct=50, conversion_24h=.9,
                          limit_amount_min=100, limit_amount_max=1000, daily_amount_limit=5000,
                          daily_approved_amount=1000, in_progress_count_limit=5, in_progress_count=1,
                          in_progress_amount_limit=2000, in_progress_amount=100, available_requisites=3,
                          banks=["sberbank"], requests_per_minute_limit=10, daily_turnover_max=4000,
                          provider_margin_pct=1, merchant_margin_pct=2)
        self.a = ProviderActuals(turnover_actual=1000, rpm_used=2)
        self.op = Operation("op", 500, "sberbank", "2026-07-30T10:00:00+03:00")

    def test_all_hard_rules_and_boundaries(self):
        cases = [("status_not_active", {"status": "disabled"}), ("amount_below_minimum", {"limit_amount_min": 501}),
                 ("amount_exceeds_limit", {"limit_amount_max": 499}), ("daily_amount_limit_exceeded", {"daily_amount_limit": 1499}),
                 ("in_progress_limit_exceeded", {"in_progress_count_limit": 1}), ("in_progress_limit_exceeded", {"in_progress_amount_limit": 599}),
                 ("bank_not_in_list", {"banks": ["other"]}), ("bank_not_in_list", {"exclude_banks": True}),
                 ("margin_not_acceptable", {"provider_margin_pct": 3}), ("no_available_requisites", {"available_requisites": 0}),
                 ("rate_limit_exceeded", {"requests_per_minute_limit": 2}), ("daily_turnover_max_exceeded", {"daily_turnover_max": 1499})]
        for reason, changes in cases:
            with self.subTest(reason=reason, changes=changes):
                self.assertEqual([reason], Engine().call(self.p.with_updates(**changes), self.op, self.a).reasons)
        boundaries = self.p.with_updates(limit_amount_min=500, limit_amount_max=500, daily_amount_limit=1500, in_progress_amount_limit=600, daily_turnover_max=1500)
        self.assertTrue(Engine().call(boundaries, self.op, self.a).eligible)
        self.assertTrue(Engine().call(self.p.with_updates(banks=[], allow_negative_agreement=True, provider_margin_pct=10), self.op, self.a).eligible)

    def test_all_reasons_preserve_order(self):
        p = self.p.with_updates(status="disabled", available_requisites=0, limit_amount_max=10)
        self.assertEqual(["status_not_active", "amount_exceeds_limit", "no_available_requisites"], Engine().call(p, self.op, self.a).reasons)

    def test_strategy_weights(self):
        calculator = StrategyWeightCalculator(StrategyRegistry())
        solo = calculator.call(["priority"])
        self.assertAlmostEqual(1, sum(solo["weights"].values()))
        self.assertEqual(.7, solo["weights"]["priority"])
        self.assertEqual(4, calculator.call(["intensity"])["gamma"])
        combo = calculator.call(["count_share", "priority"])
        self.assertAlmostEqual(1 / 2.6, combo["weights"]["count_share"])
        self.assertEqual(0, combo["weights"]["conversion"])
        for active in ([], ["unknown"]):
            with self.assertRaises(ValueError):
                calculator.call(active)

    def test_norms_and_load_factor(self):
        for target, actual, expected in [(100, 0, 1), (100, 100, .5), (100, 200, 0), (None, 0, 1), (0, 10, 1)]:
            self.assertEqual(expected, deviation_norm(target, actual))
        p = self.p.with_updates(preferred_range=AmountRange(100, 900), daily_turnover_min=2000)
        score = ProviderScoreCalculator({"range_fit": 1}, 2).rank([p], {"p": self.a}, self.op)[0]
        self.assertEqual(1, score.breakdown["range_fit"])
        self.assertEqual(1, score.breakdown["priority"])
        self.assertEqual(1, score.breakdown["conversion"])
        self.assertEqual(.75, score.breakdown["turnover"])
        self.assertAlmostEqual(64, score.score)
        self.assertEqual(0, LoadFactorCalculator().utilization(Provider("unlimited"), ProviderActuals()))

    def make_router(self, providers, actuals=None, client=None, simulator=None):
        fallback = Provider("spacepayments")
        state = RunState([*providers, fallback], actuals or {p.payment_system: ProviderActuals() for p in [*providers, fallback]})
        router = Router(state, [p.payment_system for p in providers], "spacepayments", StrategyRegistry(), ["priority"], provider_client=client, outcome_simulator=simulator)
        return router, state

    def test_ties_keep_rated_order(self):
        router, _ = self.make_router([Provider("b"), Provider("a")])
        self.assertEqual("b", router.route(self.op).selected_provider)

    def test_unavailable_cascade_cleans_in_progress_and_records_requests(self):
        class FailFirst(ProviderClient):
            def attempt(self, provider, operation):
                if provider.payment_system == "first":
                    raise self.UnavailableError()
        router, state = self.make_router([Provider("first", priority=1), Provider("second", priority=2)], client=FailFirst())
        decision = router.route(self.op)
        self.assertEqual("second", decision.selected_provider)
        self.assertEqual(["provider_unavailable", "highest_score"], [a.reason for a in decision.attempts])
        for name in ("first", "second"):
            self.assertEqual(0, state.provider(name).in_progress_count)
            self.assertEqual(0, state.provider(name).in_progress_amount)
            self.assertEqual(1, state.actuals(name).rpm_used)
        self.assertEqual(500, state.provider("second").daily_approved_amount)

    def test_fallback_preserves_reserve_policy(self):
        router, state = self.make_router([Provider("disabled", status="disabled")])
        decision = router.route(self.op)
        self.assertEqual("spacepayments", decision.selected_provider)
        self.assertEqual("no_eligible_provider", decision.attempts[-1].reason)
        self.assertEqual(1, state.actuals("spacepayments").rpm_used)

    def test_all_unavailable_fallback_and_rejected_accounting(self):
        class AlwaysFail(ProviderClient):
            def attempt(self, provider, operation):
                raise self.UnavailableError()
        router, _ = self.make_router([Provider("first")], client=AlwaysFail())
        self.assertEqual("all_providers_unavailable", router.route(self.op).attempts[-1].reason)
        class Reject:
            def simulate(self, provider, operation):
                return "rejected"
        router, state = self.make_router([Provider("first")], simulator=Reject())
        self.assertEqual("rejected", router.route(self.op).simulated_result)
        self.assertEqual(0, state.actuals("first").count_actual)
        self.assertEqual(1, state.actuals("first").rpm_used)

    def test_sliding_window_midnight_and_backwards_protection(self):
        at = parse_time("2026-07-30T00:00:00+03:00")
        p = self.p.with_updates(daily_approved_date="2026-07-29", daily_utc_offset=10800)
        times = [at - timedelta(seconds=60), at - timedelta(seconds=59), at, at + timedelta(seconds=1)]
        state = RunState([p], {"p": self.a.with_updates(request_times=times)})
        state.advance_to(at)
        self.assertEqual(2, state.actuals("p").rpm_used)
        self.assertEqual(0, state.provider("p").daily_approved_amount)
        state.advance_to(at + timedelta(seconds=1))
        self.assertEqual(2, state.actuals("p").rpm_used)
        with self.assertRaises(ValueError):
            state.advance_to(at)
        future = RunState([p.with_updates(daily_approved_date="2026-07-31")], {"p": self.a})
        with self.assertRaises(ValueError):
            future.advance_to(at)

    def test_shares_update_for_all_providers(self):
        router, state = self.make_router([Provider("first", priority=1), Provider("second", priority=2)])
        router.route(self.op)
        self.assertEqual(100, state.actuals("first").count_share_actual)
        self.assertEqual(0, state.actuals("second").count_share_actual)
        self.assertEqual(0, state.actuals("spacepayments").volume_share_actual)
