"""Deterministic advice based on observed traffic, constraints and routing traces."""
import json
from datetime import timedelta
from collections import Counter
from .analytics import group_by, latency_stats
from .utils import clean, number, parse_time, percentage

AMOUNT_BANDS = [("от 0 до 1 000", 0, 1000), ("от 1 000 до 10 000", 1000, 10000), ("от 10 000 до 50 000", 10000, 50000), ("от 50 000 до 100 000", 50000, 100000), ("от 100 000", 100000, None)]
SKIP_LABELS = {"bank_not_in_list": "банк не входит в список доступных", "amount_exceeds_limit": "сумма превышает допустимый предел", "daily_amount_limit_exceeded": "дневной лимит суммы исчерпан", "provider_timeout": "превышено время ожидания провайдера", "provider_rejected": "провайдер отклонил операцию", "provider_expired": "истёк срок обработки операции", "provider_unavailable": "провайдер недоступен", "unknown": "причина не указана"}


class Recommendations:
    def __init__(self, providers, records, all_records, report, generated_at, details=None, snapshot_at=None):
        self.providers, self.report, self.at = providers, report, generated_at
        self.by_name = {p["payment_system"]: p for p in providers}
        self.by_id = {p["payment_system_id"]: p["payment_system"] for p in providers if "payment_system_id" in p}
        self.details, self.snapshot_at = details or {}, parse_time(snapshot_at)
        self.scope_ids = {r["operation_id"] for r in records}
        self.records = [r for r in records if self.valid_record(r)]
        self.all_records = [r for r in all_records if self.valid_record(r)]
        self.groups = group_by(self.records, lambda r: r["payment_system"])
        self.skip_groups = group_by([r for r in self.details.get("attempts", []) if r["operation_id"] in self.scope_ids and r["payment_system_id"] in self.by_id], lambda r: r["payment_system_id"])
        self.result = []
        self.invalid_data = False

    def valid_record(self, row):
        amount, at = number(row.get("amount")), parse_time(row.get("created_at"))
        return row.get("payment_system") in self.by_name and amount is not None and amount >= 0 and at is not None and at <= self.at

    def call(self):
        quality = self.report.get("data_quality", {})
        self.invalid_data = any(quality.get(key, 0) > 0 for key in ("unknown_provider_count", "invalid_amount_count", "invalid_timestamp_count")) or self.report.get("routing_coverage", {}).get("unknown_selected_provider", 0) > 0 or any(quality.get("database_orphans", {}).values())
        if self.invalid_data:
            self.result.append("Есть неизвестные провайдеры, некорректные суммы, даты или нарушенные связи; не использовать затронутую выборку для автоматической оптимизации до исправления данных.")
        if not self.records:
            self.result.append("Недостаточно операций для рекомендаций; накопить журнал новых решений.")
        for p in self.providers:
            self.provider_recommendations(p)
        self.skip_recommendations()
        self.eligibility_recommendations()
        if self.records:
            self.segment_recommendations()
        self.score_recommendations()
        self.routing_recommendations()
        return list(dict.fromkeys(self.result))

    @staticmethod
    def approval(rows):
        return percentage(sum(r["status"] == "approved" for r in rows), len(rows))

    @staticmethod
    def latencies(rows):
        return [n for r in rows if (n := number(r.get("latency_sec"))) is not None and n >= 0]

    @staticmethod
    def utilization(p, used_key, limit_key):
        used, limit = number(p.get(used_key)), number(p.get(limit_key))
        return percentage(used, limit) if used is not None and used >= 0 and limit is not None and limit > 0 else None

    def stale(self, p):
        at = parse_time(p.get("stats_calculated_at"))
        return bool((at and (self.at - at).total_seconds() > 300) or (self.snapshot_at and (self.at - self.snapshot_at).total_seconds() > 3600))

    def frequent_blocking_skips(self, p):
        rows = self.skip_groups.get(p.get("payment_system_id"), [])
        evaluated = len({r["operation_id"] for r in rows})
        if evaluated < 10:
            return False
        for reason in ("bank_not_in_list", "daily_amount_limit_exceeded"):
            count = len({r["operation_id"] for r in rows if r["decision"] == "skipped" and r["reason"] == reason})
            if count >= 5 and percentage(count, evaluated) >= 20:
                return True
        return False

    def constrained(self, p):
        daily = self.utilization(p, "daily_approved_amount", "daily_amount_limit")
        workload = [self.utilization(p, key, key + "_limit") for key in ("in_progress_count", "in_progress_amount")]
        requisites = number(p.get("available_requisites"))
        status = str(p.get("status") or "").strip().lower()
        return bool((status and status != "active") or self.stale(p) or self.frequent_blocking_skips(p) or
                    (daily is not None and daily >= 90) or any(n is not None and n >= 80 for n in workload) or (requisites is not None and requisites < 3))

    def degraded(self, name):
        own, peers = self.groups.get(name, []), [r for r in self.records if r["payment_system"] != name]
        latencies = self.latencies(own)
        return ((len(own) >= 10 and percentage(sum(r["status"] == "expired" for r in own), len(own)) >= 20) or
                (len(own) >= 10 and len(peers) >= 10 and self.approval(peers) - self.approval(own) >= 15) or
                (len(latencies) >= 10 and latency_stats(latencies)["p95_sec"] > 60))

    def can_increase(self, p):
        return not self.invalid_data and not self.constrained(p) and not self.degraded(p["payment_system"])

    def state_recommendations(self, p):
        name = p["payment_system"]
        count, amount = [self.utilization(p, key, key + "_limit") for key in ("in_progress_count", "in_progress_amount")]
        if count is not None and count >= 80:
            self.result.append(f"{name}: лимит количества операций в обработке использован на {count}%; снизить входящий поток по количеству до освобождения доступной ёмкости.")
        if amount is not None and amount >= 80:
            self.result.append(f"{name}: лимит суммы операций в обработке использован на {amount}%; ограничить крупные операции либо перераспределить объём на других провайдеров.")
        req = number(p.get("available_requisites"))
        if req is not None and req < 3:
            self.result.append(f"{name}: мало доступных реквизитов ({clean(req)}); не увеличивать трафик до восстановления доступных реквизитов.")
        status = str(p.get("status") or "").strip().lower()
        if status and status != "active":
            self.result.append(f"{name}: провайдер неактивен; исключить из основного пула и использовать только после восстановления состояния.")
        if self.stale(p):
            self.result.append(f"{name}: данные провайдера устарели; не изменять веса автоматически, сначала обновить снимок состояния и статистику провайдера.")

    def provider_recommendations(self, p):
        self.state_recommendations(p)
        if not self.records:
            return
        name = p["payment_system"]
        own = self.groups.get(name, [])
        share = percentage(len(own), len(self.records))
        volume = percentage(sum(r["amount"] for r in own), sum(r["amount"] for r in self.records))
        target, volume_target = number(p.get("traffic_percentage")), number(p.get("volume_share_pct"))
        gap = share - target if target is not None and target > 0 else None
        volume_gap = volume - volume_target if volume is not None and volume_target is not None else None
        daily, increase = self.utilization(p, "daily_approved_amount", "daily_amount_limit"), self.can_increase(p)
        if daily is not None and daily >= 90:
            if gap is not None and gap <= -5:
                self.result.append(f"{name}: дневной лимит использован на {daily}%, доля количества ниже цели на {clean(-gap)} п.п.; увеличить доступную ёмкость или снизить целевую долю.")
            else:
                self.result.append(f"{name}: дневной лимит использован на {daily}%; временно снизить приоритет или вес провайдера до обновления лимита.")
        elif gap is not None and gap <= -5 and increase:
            self.result.append(f"{name}: доля количества ниже цели на {clean(-gap)} п.п.; повысить вес распределения по количеству среди допустимых провайдеров.")
        elif gap is not None and gap >= 5:
            self.result.append(f"{name}: доля количества выше цели на {clean(gap)} п.п.; снизить вес распределения по количеству среди допустимых провайдеров.")
        if volume_gap is not None and volume_gap <= -5 and increase:
            self.result.append(f"{name}: доля суммы ниже цели на {clean(-volume_gap)} п.п.; повысить вес распределения по объёму для провайдера.")
        elif volume_gap is not None and volume_gap >= 5:
            self.result.append(f"{name}: доля суммы выше цели на {clean(volume_gap)} п.п.; снизить вес распределения по объёму.")
        if gap is not None and abs(gap) < 5 and volume_gap is not None and abs(volume_gap) >= 5:
            self.result.append(f"{name}: доля количества соответствует цели, но доля суммы отклонена на {clean(volume_gap)} п.п.; проверить распределение по суммам: распределение по количеству не обеспечивает целевой объём.")
        self.performance_recommendations(p, own, share, gap, increase)

    def performance_recommendations(self, p, own, share, gap, increase):
        name = p["payment_system"]
        if len(own) >= 10:
            expired = percentage(sum(r["status"] == "expired" for r in own), len(own))
            if expired >= 20:
                self.result.append(f"{name}: доля операций с истёкшим сроком составляет {expired}%; проверить время ожидания и обработки до увеличения трафика.")
            peers = [r for r in self.records if r["payment_system"] != name]
            if len(peers) >= 10 and self.approval(peers) - self.approval(own) >= 15:
                self.result.append(f"{name}: успешность {self.approval(own)}% существенно ниже остальных провайдеров ({self.approval(peers)}%); снизить вес показателя успешности или общий приоритет провайдера до восстановления успешности.")
            if self.approval(own) >= 90 and (share < 5 or (gap is not None and gap <= -5)) and increase and (number(p.get("traffic_percentage")) or 0) > 0:
                self.result.append(f"{name}: успешность высокая ({self.approval(own)}%), но трафика мало; рассмотреть увеличение веса провайдера, если ограничения и лимиты позволяют.")
        latencies = self.latencies(own)
        if len(latencies) >= 10:
            stats = latency_stats(latencies)
            p95, p50 = stats["p95_sec"], stats["p50_sec"]
            if p95 > 60:
                self.result.append(f"{name}: 95-й перцентиль времени обработки составляет {p95} с и превышает порог 60 с; ограничить новый трафик и проверить деградацию провайдера.")
            if p95 >= p50 * 3 and p95 - p50 >= 10:
                self.result.append(f"{name}: 95-й перцентиль времени обработки ({p95} с) значительно выше медианы ({p50} с); наблюдается длинный хвост времени обработки, проверить нестабильные или зависшие операции.")
        expired = self.latencies([r for r in own if r["status"] == "expired"])
        approved = self.latencies([r for r in own if r["status"] == "approved"])
        if len(expired) >= 5 and len(approved) >= 5:
            a, b = sum(expired) / len(expired), sum(approved) / len(approved)
            if a >= b * 2 and a - b >= 10:
                self.result.append(f"{name}: операции с истёкшим сроком обрабатываются значительно дольше одобренных; сократить время ожидания или раньше переключаться на резервный маршрут.")

    def skip_recommendations(self):
        for pid, rows in self.skip_groups.items():
            evaluated = len({r["operation_id"] for r in rows})
            skips = {(r["operation_id"], r["reason"] or "unknown") for r in rows if r["decision"] == "skipped"}
            for reason, count in sorted(Counter(reason for _, reason in skips).items()):
                if count < 5:
                    continue
                name = self.by_id[pid]
                label = SKIP_LABELS.get(reason, f"код причины «{reason}»")
                if percentage(count, len(skips)) >= 50:
                    self.result.append(f"{name}: среди пропусков доминирует причина «{label}» ({count}); скорректировать правила провайдера либо стратегию маршрутизации по этой причине.")
                if evaluated >= 10 and percentage(count, evaluated) >= 20:
                    advice = {"bank_not_in_list": "не увеличивать глобальную долю провайдера; учитывать доступность по банкам", "amount_exceeds_limit": "перенаправлять крупные операции к провайдерам с большим диапазоном сумм", "daily_amount_limit_exceeded": "уменьшить целевую долю либо увеличить дневной лимит; дальнейшее повышение веса бессмысленно"}.get(reason)
                    if advice:
                        self.result.append(f"{name}: частые пропуски по причине «{label}» ({count} из {evaluated} операций); {advice}.")

    def eligibility_recommendations(self):
        known = {r["operation_id"] for r in self.details.get("decisions", []) + self.details.get("queue", [])}
        groups = group_by([r for r in self.details.get("eligibility", []) if r["operation_id"] in known], lambda r: r["operation_id"])
        groups = {key: checks for key, checks in groups.items() if key is not None and all(r["payment_system_id"] in self.by_id and r["is_eligible"] in (True, False, 1, 0, "1", "0") for r in checks)}
        if len(groups) < 10:
            return
        limited = sum(len({r["payment_system_id"] for r in checks if r["is_eligible"] in (True, 1, "1")}) <= 1 for checks in groups.values())
        share = percentage(limited, len(groups))
        if share >= 20:
            self.result.append(f"Для {share}% операций с проверкой доступности имеется не более одного допустимого провайдера; высок риск отсутствия маршрута, расширить покрытие провайдеров и пересмотреть ограничения.")

    def compare_segment(self, rows):
        for name, own in group_by(rows, lambda r: r["payment_system"]).items():
            peers = [r for r in rows if r["payment_system"] != name]
            if len(own) >= 10 and len(peers) >= 10:
                yield name, self.approval(own) - self.approval(peers)

    def best_in_segment(self, name, rows):
        groups = group_by(rows, lambda r: r["payment_system"])
        own = groups.pop(name, [])
        return len(own) >= 10 and bool(groups) and all(len(peers) >= 10 and self.approval(own) - self.approval(peers) >= 15 for peers in groups.values())

    def segment_recommendations(self):
        for bank, rows in group_by(self.records, lambda r: r["bank"]).items():
            if not bank or not bank.strip():
                continue
            for name, gap in self.compare_segment(rows):
                if gap <= -15:
                    self.result.append(f"{name}, банк «{bank}»: успешность заметно ниже остальных провайдеров этого банка; снизить вес провайдера для данного банка.")
                elif gap >= 15 and self.can_increase(self.by_name[name]):
                    self.result.append(f"{name}, банк «{bank}»: успешность заметно выше остальных провайдеров этого банка при достаточной выборке; повысить предпочтение провайдера для данного банка.")
        previous_date = max(parse_time(r["created_at"]).date() for r in self.records) - timedelta(days=1)
        previous = [r for r in self.all_records if parse_time(r["created_at"]).date() == previous_date]
        for label, lower, upper in AMOUNT_BANDS:
            in_band = lambda r: r["amount"] >= lower and (upper is None or r["amount"] < upper)
            rows, prior = [r for r in self.records if in_band(r)], [r for r in previous if in_band(r)]
            for name, gap in self.compare_segment(rows):
                if gap <= -15:
                    self.result.append(f"{name}, сумма {label}: успешность заметно ниже остальных провайдеров; снизить приоритет для данного диапазона сумм.")
                elif gap >= 15 and self.best_in_segment(name, rows) and self.best_in_segment(name, prior) and self.can_increase(self.by_name[name]):
                    self.result.append(f"{name}, сумма {label}: провайдер лучший по успешности в двух соседних днях при достаточной выборке; повысить приоритет внутри данного диапазона.")

    def score_recommendations(self):
        close, strong = 0, 0
        for d in self.details.get("decisions", []):
            explanation = d.get("explanation")
            try:
                if isinstance(explanation, str):
                    explanation = json.loads(explanation)
            except ValueError:
                continue
            if not isinstance(explanation, dict):
                continue
            ranking = explanation.get("ranking", explanation.get("scores"))
            if isinstance(ranking, dict):
                ranking = [{"provider": name, "score": score} for name, score in ranking.items()]
            if not isinstance(ranking, list):
                continue
            scores = []
            for r in ranking:
                if not isinstance(r, dict) or r.get("is_eligible") in (False, 0, "0"):
                    continue
                name = r.get("provider") or r.get("payment_system") or self.by_id.get(r.get("payment_system_id"))
                score = number(r.get("score", r.get("total_score")))
                if name in self.by_name and score is not None:
                    scores.append((name, score))
            if len(scores) < 2 or len({name for name, _ in scores}) != len(scores):
                continue
            first, second = sorted(scores, key=lambda item: -item[1])[:2]
            scale = max(abs(first[1]), abs(second[1]))
            gap = 100 * (first[1] - second[1]) / scale if scale else 0
            if gap <= 5:
                close += 1
            elif gap >= 25 and first[0] == self.by_id.get(d.get("selected_payment_system_id")) and self.can_increase(self.by_name[first[0]]):
                strong += 1
        if close:
            self.result.append(f"Для {close} операций оценки двух лучших провайдеров почти одинаковы; решение имеет низкую уверенность, сильнее учитывать балансировку и целевую долю.")
        if strong:
            self.result.append(f"Для {strong} операций оценка выбранного провайдера значительно выше второй; сохранить выбор, имеется сильное основание для маршрута.")

    def routing_recommendations(self):
        coverage = self.report.get("routing_coverage", {})
        cascades = self.report.get("attempt_cascades", {})
        pct = number(coverage.get("decision_coverage_pct"))
        if pct is not None and pct < 100:
            self.result.append(f"Решения покрывают {pct:g}% операций; проверить необработанные операции и журнал решений.")
        logs = number(cascades.get("attempt_log_coverage_pct"))
        if logs is not None and logs < 100:
            self.result.append(f"Журнал шагов покрывает {logs:g}% решений; недостаточно данных для надёжного анализа резервных маршрутов и качества маршрутизации.")
        rate = number(coverage.get("fallback_share_of_decisions_pct"))
        if coverage.get("with_decision", 0) >= 10 and rate is not None and rate >= 20:
            self.result.append(f"Доля резервных маршрутов высокая ({rate:g}%); проверить качество первого выбора и причины отказов до переключения на резерв.")
        if cascades.get("fallback_operations", 0) >= 10:
            approved = number(cascades.get("fallback_approval_pct"))
            if approved is not None and approved >= 80:
                self.result.append(f"Успешность резервных маршрутов высокая ({approved:g}%); резерв эффективно восстанавливает операции, сохранить резервный маршрут.")
            elif approved is not None and approved < 50:
                self.result.append(f"Успешность резервных маршрутов низкая ({approved:g}%); пересмотреть порядок резервных провайдеров.")
