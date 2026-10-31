import json
from collections import defaultdict
from .database import connect, rows, transaction, upgrade_schema, upsert
from .importers import provider_id
from .utils import ensure_writable, iso, now, parse_time


class DecisionsReader:
    def __init__(self, db):
        self.db = db

    def load(self):
        decisions = {row["operation_id"]: row for row in rows(self.db, "SELECT d.*, p.payment_system AS selected_provider FROM routing_decisions d JOIN providers p ON p.payment_system_id=d.selected_payment_system_id")}
        attempts = defaultdict(list)
        for row in rows(self.db, "SELECT a.*, p.payment_system AS provider FROM routing_attempts a JOIN providers p USING(payment_system_id) ORDER BY a.operation_id, a.attempt_number"):
            attempt = {key: row[key] for key in ("provider", "decision", "reason")}
            if row.get("details") is not None:
                attempt["details"] = json.loads(row["details"])
            if row.get("dispatched_at") is not None:
                attempt["dispatched_at"] = iso(parse_time(row["dispatched_at"]), True)
            attempts[row["operation_id"]].append(attempt)
        queue = sorted(rows(self.db, "SELECT operation_id, created_at FROM operations_queue"), key=lambda r: (parse_time(r["created_at"]), r["operation_id"]))
        result = []
        for row in queue:
            key = row["operation_id"]
            if key not in decisions:
                raise ValueError(f"Нет routing_decisions для {key} — запустите route перед build_decisions")
            decision = decisions[key]
            record = {name: decision[name] for name in ("operation_id", "selected_provider", "simulated_result", "latency_sec")}
            record["attempts"] = attempts[key]
            if decision.get("explanation") is not None:
                record["explanation"] = json.loads(decision["explanation"])
            result.append(record)
        return result


class DatabaseWriter:
    def __init__(self, path, protected_roots=(), db=None):
        self.path = ensure_writable(path, protected_roots)
        if db is None and not self.path.is_file():
            raise ValueError(f"database does not exist: {self.path}")
        self.owns_db = db is None
        self.db = db if db is not None else connect(path)
        with transaction(self.db):
            upgrade_schema(self.db)

    def close(self):
        if self.owns_db:
            self.db.close()

    def log_operations(self, operations, decisions, logged_at=None):
        operation_ids = [o.get("operation_id") for o in operations]
        decision_ids = [d.get("operation_id") for d in decisions]
        if len(set(operation_ids)) != len(operation_ids) or len(set(decision_ids)) != len(decision_ids):
            raise ValueError("duplicate operation_id in input")
        if set(operation_ids) != set(decision_ids):
            raise ValueError("operation/decision mismatch")
        if any(not d.get("selected_provider") or not isinstance(d.get("attempts"), list) for d in decisions):
            raise ValueError("routing decision must contain selected_provider and attempts array")
        stamp = iso(logged_at or now(), True)
        by_id = {d["operation_id"]: d for d in decisions}
        with transaction(self.db):
            for operation in operations:
                key = operation["operation_id"]
                decision = by_id[key]
                for field in ("operation_id", "amount", "bank"):
                    if operation.get(field) is None or operation[field] == "":
                        raise ValueError(f"missing required field: {field}")
                queued = self.db.execute("SELECT * FROM operations_queue WHERE operation_id=?", (key,)).fetchone()
                queued = dict(queued) if queued else {}
                provider = provider_id(self.db, decision["selected_provider"])
                status = decision.get("simulated_result") or "unknown"
                upsert(self.db, "operations_history", {
                    "operation_id": key, "created_at": operation.get("created_at") or queued.get("created_at") or stamp,
                    "amount": operation["amount"], "bank": operation["bank"],
                    "card_brand": operation.get("card_brand", queued.get("card_brand")),
                    "payment_system_id": provider, "status": status, "latency_sec": decision.get("latency_sec"),
                }, "operation_id")
                explanation = decision.get("explanation")
                upsert(self.db, "routing_decisions", {"operation_id": key, "selected_payment_system_id": provider,
                       "simulated_result": status, "latency_sec": decision.get("latency_sec"), "created_at": stamp,
                       "explanation": json.dumps(explanation, ensure_ascii=False) if explanation is not None else None}, "operation_id")
                for table in ("routing_attempts", "eligible_providers", "provider_skip_reasons"):
                    self.db.execute(f"DELETE FROM {table} WHERE operation_id=?", (key,))
                for index, attempt in enumerate(decision["attempts"], 1):
                    pid = provider_id(self.db, attempt["provider"])
                    details = attempt.get("details")
                    self.db.execute("INSERT INTO routing_attempts(operation_id,payment_system_id,attempt_number,decision,reason,details,dispatched_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                    (key, pid, index, attempt["decision"], attempt.get("reason"), json.dumps(details, ensure_ascii=False) if details is not None else None, attempt.get("dispatched_at"), stamp))
                    if attempt["decision"] == "skipped":
                        self.store_skip(key, pid, attempt.get("reason") or "unknown", stamp)
                for check in (explanation or {}).get("eligibility", []):
                    pid = provider_id(self.db, check["provider"])
                    self.db.execute("INSERT INTO eligible_providers VALUES(?,?,?,?)", (key, pid, check["is_eligible"], stamp))
                    for reason in check.get("reasons", []):
                        self.store_skip(key, pid, reason, stamp)
        return len(operations)

    def store_skip(self, operation_id, pid, reason, stamp):
        self.db.execute("INSERT OR IGNORE INTO provider_skip_reasons(operation_id,payment_system_id,reason,created_at) VALUES(?,?,?,?)", (operation_id, pid, reason, stamp))


class StateWriter:
    def __init__(self, db):
        self.db = db

    def write(self, state):
        with transaction(self.db):
            for provider in state.providers:
                self.db.execute("UPDATE providers SET in_progress_count=?,in_progress_amount=?,daily_approved_amount=?,daily_approved_date=?,daily_utc_offset=? WHERE payment_system=?", (provider.in_progress_count, provider.in_progress_amount, provider.daily_approved_amount, provider.daily_approved_date, provider.daily_utc_offset, provider.payment_system))
