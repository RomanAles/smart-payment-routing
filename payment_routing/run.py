from dataclasses import dataclass
from .config import StrategyRegistry
from .database import transaction
from .importers import import_business_parameters
from .loaders import HistoricalActualsProvider, OperationQueueLoader, ProviderRegistry
from .persistence import DatabaseWriter, DecisionsReader, StateWriter
from .router import Router, RunState


@dataclass
class RunResult:
    processed: bool
    decisions: list | None = None


class RoutingRun:
    def __init__(self, db, database_path, config):
        self.db, self.database_path, self.config = db, database_path, config

    def call(self):
        cfg = self.config
        with transaction(self.db, immediate=True):
            import_business_parameters(self.db, cfg.business_parameters_file)
            names = list(dict.fromkeys(cfg.rated_providers + [cfg.fallback_provider]))
            providers = ProviderRegistry(self.db, names).load()
            missing = set(names) - {p.payment_system for p in providers}
            if missing:
                raise ValueError(f"Missing providers: {', '.join(sorted(missing))}; run import_data")
            actuals = HistoricalActualsProvider(self.db).load()
            operations = OperationQueueLoader(self.db).load()
            if not operations:
                return RunResult(False)
            state = RunState(providers, actuals)
            router = Router(state, cfg.rated_providers, cfg.fallback_provider, StrategyRegistry(cfg.strategies_file), cfg.active_strategies)
            routed = router.route_all(operations)
            StateWriter(self.db).write(state)
            writer = DatabaseWriter(self.database_path, db=self.db)
            writer.log_operations([op.to_dict() for op in operations], [d.to_dict() for d in routed])
            decisions = DecisionsReader(self.db).load()
        from .stats import ProviderMinuteStats
        ProviderMinuteStats(self.database_path).run()
        return RunResult(True, decisions)
