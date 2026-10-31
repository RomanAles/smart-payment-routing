import json
from collections import Counter, defaultdict
from .models import AmountRange, Operation, Provider, ProviderActuals
from .utils import now, parse_time, share
from .database import rows


class ProviderRegistry:
    def __init__(self, db, rated_providers):
        self.db, self.names = db, rated_providers

    def load(self):
        result = []
        for row in rows(self.db, "SELECT * FROM providers"):
            if row["payment_system"] not in self.names:
                continue
            low, high, limit = row["daily_turnover_min"], row["daily_turnover_max"], row["daily_amount_limit"]
            for key in ("daily_turnover_min", "daily_turnover_max"):
                value = row[key]
                if value is not None and (value < 0 or (limit is not None and value > limit)):
                    raise ValueError(f"Провайдер '{row['payment_system']}': invalid {key}: {value}")
            if low is not None and high is not None and low > high:
                raise ValueError(f"Провайдер '{row['payment_system']}': daily_turnover_min > daily_turnover_max")
            fields = {key: row[key] for key in Provider.__dataclass_fields__ if key in row}
            fields["banks"] = json.loads(row["banks"] or "[]")
            fields["exclude_banks"] = bool(row["exclude_banks"])
            fields["allow_negative_agreement"] = bool(row["allow_negative_agreement"])
            fields["daily_utc_offset"] = row.get("daily_utc_offset") or 0
            lower, upper = row["preferred_range_min"], row["preferred_range_max"]
            fields["preferred_range"] = AmountRange(lower, upper) if lower is not None and upper is not None else None
            result.append(Provider(**fields))
        return result


class OperationQueueLoader:
    def __init__(self, db):
        self.db = db

    def load(self):
        source = rows(self.db, "SELECT q.* FROM operations_queue q LEFT JOIN routing_decisions d USING(operation_id) LEFT JOIN operations_history h USING(operation_id) WHERE d.operation_id IS NULL AND h.operation_id IS NULL")
        operations = [Operation(**{key: row[key] for key in Operation.__dataclass_fields__}) for row in source]
        return sorted(operations, key=lambda op: (op.created_at, op.operation_id))


class HistoricalActualsProvider:
    def __init__(self, db):
        self.db = db

    def load(self, at=None):
        history = rows(self.db, "SELECT h.*, p.payment_system FROM operations_history h JOIN providers p USING(payment_system_id)")
        counts, volumes, requests = Counter(), Counter(), defaultdict(list)
        attempts = rows(self.db, "SELECT a.*, p.payment_system FROM routing_attempts a JOIN providers p USING(payment_system_id) WHERE dispatched_at IS NOT NULL")
        recorded = {row["operation_id"] for row in attempts}
        for row in history:
            name = row["payment_system"]
            if row["status"] == "approved":
                counts[name] += 1
                volumes[name] += row["amount"]
            parsed = parse_time(row["created_at"])
            if row["operation_id"] not in recorded and parsed:
                requests[name].append(parsed)
        for row in attempts:
            parsed = parse_time(row["dispatched_at"])
            if parsed:
                requests[row["payment_system"]].append(parsed)
        end = at or max((t for times in requests.values() for t in times), default=now())
        total_count, total_volume = sum(counts.values()), sum(volumes.values())
        return {p["payment_system"]: ProviderActuals(
            count_share_actual=share(counts[p["payment_system"]], total_count),
            volume_share_actual=share(volumes[p["payment_system"]], total_volume),
            count_actual=counts[p["payment_system"]], volume_actual=volumes[p["payment_system"]],
            turnover_actual=float(p["daily_approved_amount"] or 0),
            rpm_used=sum(0 <= (end - t).total_seconds() < 60 for t in requests[p["payment_system"]]),
            request_times=requests[p["payment_system"]],
        ) for p in rows(self.db, "SELECT * FROM providers")}
