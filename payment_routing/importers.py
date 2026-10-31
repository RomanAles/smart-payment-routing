import csv
import json
from .config import load_yaml
from .database import columns, transaction, upsert
from .utils import parse_time, read_json

BUSINESS_FIELDS = {"preferred_range_min", "preferred_range_max", "volume_share_pct", "requests_per_minute_limit", "daily_turnover_min", "daily_turnover_max"}


def provider_id(db, name):
    row = db.execute("SELECT payment_system_id FROM providers WHERE payment_system=?", (name,)).fetchone()
    if not row:
        raise ValueError(f"Провайдер '{name}' не найден — сначала импортируйте providers")
    return row[0]


def import_providers(db, path):
    source = read_json(path)
    snapshot = parse_time(source.get("snapshot_at"))
    with transaction(db):
        for raw in source["providers"]:
            values = {name: raw.get(name) for name in columns(db, "providers") if name in raw and name != "payment_system_id"}
            values.update(banks=json.dumps(raw.get("banks") or []),
                          daily_approved_date=snapshot.strftime("%Y-%m-%d") if snapshot else None,
                          daily_utc_offset=int(snapshot.utcoffset().total_seconds()) if snapshot else 0)
            upsert(db, "providers", values, "payment_system")
    return len(source["providers"])


def import_business_parameters(db, path):
    parameters = load_yaml(path).get("providers") or {}
    known = {row[0] for row in db.execute("SELECT payment_system FROM providers")}
    if set(parameters) - known:
        raise ValueError(f"{path}: providers not found: {', '.join(sorted(set(parameters) - known))}")
    with transaction(db):
        for name, attrs in parameters.items():
            attrs = attrs or {}
            if set(attrs) - BUSINESS_FIELDS:
                raise ValueError(f"Unknown business fields: {set(attrs) - BUSINESS_FIELDS}")
            if attrs:
                assignment = ", ".join(f'"{key}"=?' for key in attrs)
                db.execute(f"UPDATE providers SET {assignment} WHERE payment_system=?", [*attrs.values(), name])
    return len(parameters)


def import_queue(db, path):
    source = read_json(path)
    with transaction(db):
        for raw in source:
            sbp = (raw.get("payout_requisite") or {}).get("sbp") or {}
            values = {key: raw.get(key) for key in ("operation_id", "created_at", "amount", "bank", "card_brand")}
            values.update(payout_requisite_sbp_phone=sbp.get("phone"), payout_requisite_bank_name=sbp.get("bank_name"))
            upsert(db, "operations_queue", values, "operation_id")
    return len(source)


def import_history(db, path):
    with open(path, encoding="utf-8-sig", newline="") as stream:
        source = list(csv.DictReader(stream))
    with transaction(db):
        for raw in source:
            values = {key: raw.get(key) for key in ("operation_id", "created_at", "bank", "card_brand", "status")}
            values.update(amount=int(raw["amount"]), latency_sec=int(raw.get("latency_sec") or 0), payment_system_id=provider_id(db, raw["payment_system"]))
            upsert(db, "operations_history", values, "operation_id")
    return len(source)


IMPORTERS = {"providers": ("providers", import_providers, "providers_file"),
             "business_parameters": ("providers", import_business_parameters, "business_parameters_file"),
             "queue": ("operations_queue", import_queue, "operations_queue_file"),
             "history": ("operations_history", import_history, "operations_history_file")}


def import_data(db, config, sources=None, replace=False):
    sources = list(IMPORTERS) if not sources else sources
    unknown = set(sources) - IMPORTERS.keys()
    if unknown:
        raise ValueError(f"Unknown data sources: {unknown}")
    result = {}
    with transaction(db):
        for key, (table, importer, attr) in IMPORTERS.items():
            if key not in sources:
                continue
            if replace and key != "business_parameters":
                # Foreign keys deliberately reject replacement of referenced data.
                db.execute(f'DELETE FROM "{table}"')
            result[key] = importer(db, getattr(config, attr))
    return result
