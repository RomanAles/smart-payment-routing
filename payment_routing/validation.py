"""Compatibility validator for the supplied case data (static provider snapshot)."""
from collections import Counter
from .hard_filter import Engine
from .models import Provider, ProviderActuals, Operation
from .utils import ROOT, read_json


def validate_decisions(decisions):
    decisions = decisions if isinstance(decisions, list) else [decisions]
    errors, warnings = [], []
    queue = read_json(ROOT / "data/operations_queue_90.json")
    reference = read_json(ROOT / "data/reference_decisions.json")
    providers = read_json(ROOT / "data/providers.json")["providers"]
    by_id = {}
    for decision in decisions:
        if not isinstance(decision, dict):
            errors.append("Decision must be an object")
            continue
        key = decision.get("operation_id")
        if key in by_id:
            errors.append(f"Duplicate operation_id: {key}")
        by_id[key] = decision
        for field in ("operation_id", "selected_provider", "attempts"):
            if field not in decision:
                errors.append(f"{key}: missing {field}")
        if not isinstance(decision.get("attempts"), list):
            errors.append(f"{key}: attempts must be an array")
            continue
        for attempt in decision["attempts"]:
            if not isinstance(attempt, dict) or not {"provider", "decision", "reason"} <= attempt.keys() or attempt.get("decision") not in ("selected", "skipped"):
                errors.append(f"{key}: malformed attempt")
    ids = {op["operation_id"] for op in queue}
    errors += [f"No decision for {key}" for key in sorted(ids - by_id.keys())]
    warnings += [f"Extra operation_id: {key}" for key in sorted(by_id.keys() - ids, key=str)]
    for case in reference["deterministic_cases"]:
        key = case["operation_id"]
        if key in ids and key in by_id and by_id[key].get("selected_provider") != case["required_provider"]:
            errors.append(f"{key}: expected {case['required_provider']}")
    engine = Engine()
    for op in queue:
        key = op["operation_id"]
        if key not in by_id:
            continue
        eligible = []
        for raw in providers:
            if not raw.get("traffic_percentage") and raw["payment_system"] != "spacepayments":
                continue
            fields = {k: v for k, v in raw.items() if k in Provider.__dataclass_fields__}
            fields["available_requisites"] = raw.get("available_requisites") or 0
            result = engine.call(Provider(**fields), Operation(key, op["amount"], op.get("bank")), ProviderActuals())
            if result.eligible:
                eligible.append(raw["payment_system"])
        if by_id[key].get("selected_provider") not in eligible:
            errors.append(f"{key}: selected provider not eligible; eligible={eligible}")
    for key, expected in reference.get("skip_reasons_expected", {}).items():
        if key not in ids or key not in by_id:
            continue
        attempts = by_id[key].get("attempts")
        if not isinstance(attempts, list):
            continue
        for provider, reason in expected.items():
            attempt = next((a for a in attempts if isinstance(a, dict) and a.get("provider") == provider), None)
            if attempt is None or attempt.get("decision") != "skipped":
                warnings.append(f"{key}: expected skip for {provider}: {reason}")
    return errors, warnings
