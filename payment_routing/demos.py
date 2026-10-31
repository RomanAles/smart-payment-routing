from .hard_filter import Engine
from .models import Provider, ProviderActuals, Operation


def hard_filter_demo():
    p = Provider("demo_provider", conversion_24h=.9, traffic_percentage=100, volume_share_pct=100,
                 requests_per_minute_limit=60, in_progress_count=2, in_progress_count_limit=10,
                 in_progress_amount=50000, in_progress_amount_limit=1000000, limit_amount_min=1000,
                 limit_amount_max=100000, daily_amount_limit=5000000, daily_approved_amount=1000000,
                 available_requisites=5, banks=["sberbank", "tinkoff"], provider_margin_pct=1,
                 merchant_margin_pct=1.5, daily_turnover_max=4000000)
    op, actuals, engine = Operation("demo", 10000, "sberbank"), ProviderActuals(turnover_actual=1000000), Engine()
    cases = [("healthy", {}), ("status", {"status": "disabled"}), ("amount min", {"limit_amount_min": 20000}),
             ("amount max", {"limit_amount_max": 5000}), ("daily amount", {"daily_amount_limit": 1000000}),
             ("in progress", {"in_progress_count_limit": 2}), ("bank", {"banks": ["other"]}),
             ("margin", {"provider_margin_pct": 2}), ("requisites", {"available_requisites": 0}),
             ("intensity", {"requests_per_minute_limit": 0}), ("turnover max", {"daily_turnover_max": 1000000})]
    for label, changes in cases:
        result = engine.call(p.with_updates(**changes), op, actuals)
        print(f"{label}: {', '.join(result.reasons) or 'eligible'}")
