from pathlib import Path
import math
import yaml
from .utils import ROOT


def load_yaml(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        return yaml.safe_load(stream) or {}


class RoutingConfig:
    def __init__(self, config_file=ROOT / "config/routing.yml"):
        self.config_file = Path(config_file).resolve()
        self.raw = load_yaml(self.config_file)

    def resolve(self, value):
        return ROOT / value

    def __getattr__(self, key):
        if key.endswith("_file"):
            return self.resolve(self.raw.get("data", {}).get(key) or self.raw[key])
        if key in ("rated_providers", "active_strategies"):
            return self.raw.get(key) or []
        if key == "fallback_provider":
            return self.raw[key]
        raise AttributeError(key)


class StrategyRegistry:
    def __init__(self, strategies_file=ROOT / "config/strategies.yml"):
        self.all = load_yaml(strategies_file)["strategies"]


class StrategyWeightCalculator:
    def __init__(self, registry):
        self.definitions = {row["key"]: row["combo_coefficient"] for row in registry.all}

    def call(self, active_keys):
        active_keys = list(dict.fromkeys(active_keys))
        if not active_keys:
            raise ValueError("at least one active strategy is required")
        unknown = set(active_keys) - self.definitions.keys()
        if unknown:
            raise ValueError(f"Unknown strategies: {', '.join(sorted(unknown))}")
        if len(active_keys) == 1:
            others = len(self.definitions) - 1
            weight = max(0.30 / others, 0.05) if others else 0
            weights = {key: 0.70 if key == active_keys[0] else weight for key in self.definitions}
        else:
            coefficients = [self.definitions[key] for key in active_keys]
            if any(not isinstance(c, (int, float)) or not math.isfinite(c) or c < 0 for c in coefficients) or sum(coefficients) <= 0:
                raise ValueError("Active combo coefficients must be non-negative with a positive sum")
            total = sum(coefficients)
            weights = {key: value / total if key in active_keys else 0.0 for key, value in self.definitions.items()}
        return {"weights": weights, "gamma": 4 if "intensity" in active_keys else 2}
