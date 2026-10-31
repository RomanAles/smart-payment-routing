"""Six-sheet Excel export with safe strings, charts and cached formula results."""
import math
import os
import re
import tempfile
from pathlib import Path
from .utils import clean, read_json

LATENCY = dict.fromkeys(("avg_sec", "p50_sec", "p95_sec"), "number")
PROVIDER = {**dict.fromkeys("count share_pct target_pct deviation_pp amount volume_share_pct target_volume_share_pct approved rejected expired approval_rate_pct".split(), "number"), "latency": LATENCY}
PERIOD = {"count": "number", "amount": "number", "approval_rate_pct": "number", "latency": LATENCY, "providers": ("map", PROVIDER), "from_inclusive": "text", "to_exclusive": "text"}
SEGMENT = {"count": "number", "approval_rate_pct": "number", "providers": ("map", {"approval_rate_pct": "number"})}
SCHEMA = {"period": "text", "generated_at": "text", "total_operations": "number", "total_amount": "number", "recommendation_period": "text", "distribution": ("map", PROVIDER),
          "skip_reasons": ("map", "number"), "projected_daily_utilization": ("map", dict.fromkeys(("used", "limit", "utilization_pct"), "number")), "recommendations": ("array", "text"),
          "provider_state": ("map", {"status": "text", "priority": "number"}), "latency": LATENCY, "status_summary": ("map", {"share_pct": "number"}),
          "attempt_cascades": {**dict.fromkeys("operations_with_attempt_logs recorded_attempts average_recorded_attempts first_choice_operations first_choice_approval_pct fallback_operations classification_coverage_pct".split(), "number"), "attempt_count_distribution": ("map", "number"), "attempt_decisions": ("map", "number")},
          "segments": {"by_bank": ("map", SEGMENT), "by_amount": ("map", SEGMENT)}, "period_comparison": {"current": PERIOD, "previous": PERIOD}}


def validate_report(data):
    def expect(condition, path, description):
        if not condition:
            raise ValueError(f"{path} must be {description}")

    def validate(value, schema, path):
        if value is None:
            return
        if isinstance(schema, dict):
            expect(isinstance(value, dict), path, "an object")
            for key, child in schema.items():
                validate(value.get(key), child, f"{path}.{key}")
        elif isinstance(schema, tuple):
            kind, child = schema
            expect(isinstance(value, dict if kind == "map" else list), path, "an object" if kind == "map" else "an array")
            for key, item in (value.items() if kind == "map" else enumerate(value)):
                expect(item is not None, f"{path}.{key}", "non-null")
                validate(item, child, f"{path}.{key}")
        elif schema == "number":
            expect(type(value) in (int, float) and math.isfinite(value), path, "a finite JSON number")
        elif schema == "text":
            expect(isinstance(value, str), path, "a string")

    expect(isinstance(data, dict), "$", "a JSON object")
    for key in ("period", "total_operations", "distribution", "skip_reasons", "projected_daily_utilization", "recommendations"):
        expect(key in data, key, "present")
        if key != "period":
            expect(data[key] is not None, key, "non-null")
    validate(data, SCHEMA, "$")
    for key in (data.get("attempt_cascades") or {}).get("attempt_count_distribution") or {}:
        expect(re.fullmatch(r"0|[1-9]\d*", key), f"attempt_count_distribution.{key}", "a non-negative integer key")
    return data


def deviation(metrics):
    if metrics.get("deviation_pp") is not None:
        return metrics["deviation_pp"]
    a, b = metrics.get("share_pct"), metrics.get("target_pct")
    return clean(a - b) if a is not None and b is not None else None


def export_excel(input_path, output_path=None):
    data = validate_report(read_json(input_path))
    output = Path(output_path) if output_path is not None else Path(input_path).resolve().with_name("routing_analytics.xlsx")
    if output.resolve() == Path(input_path).resolve():
        raise ValueError("Excel output must not overwrite the source JSON")
    import xlsxwriter
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=output.name + ".tmp-", suffix=".xlsx", dir=output.parent)
    os.close(fd)
    try:
        with xlsxwriter.Workbook(temporary, {"strings_to_formulas": False, "strings_to_urls": False}) as book:
            _build(book, data)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return output


def _build(book, data):
    title = book.add_format({"bold": True, "font_size": 18, "font_color": "white", "bg_color": "#1F4E78", "valign": "vcenter"})
    header = book.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1, "text_wrap": True})
    cell = book.add_format({"border": 1, "valign": "vcenter"})
    numeric = book.add_format({"border": 1, "num_format": "#,##0.00"})
    kpi = book.add_format({"bold": True, "font_size": 16, "font_color": "#1F4E78", "border": 1, "num_format": "#,##0.00"})
    wrap = book.add_format({"text_wrap": True, "valign": "top", "border": 1})

    def row(sheet, index, values, style=cell):
        sheet.write_row(index, 0, values, style)

    def section(sheet, index, text, end=5):
        sheet.merge_range(index, 0, index, end, text, title)
        sheet.set_row(index, 28)

    def chart(sheet, kind, categories, series, position, caption):
        result = book.add_chart({"type": kind})
        for name, values in series:
            result.add_series({"name": name, "categories": categories, "values": values})
        result.set_title({"name": caption})
        result.set_style(10)
        sheet.insert_chart(position, result, {"x_scale": .95, "y_scale": .85})

    distribution = data["distribution"]
    dashboard = book.add_worksheet("Dashboard")
    dashboard.set_column("A:A", 25)
    dashboard.set_column("B:H", 18)
    dashboard.merge_range("A1:H2", "SMART ROUTING ANALYTICS", title)
    row(dashboard, 2, ["Period", data.get("period"), "Generated at", data.get("generated_at")])
    labels = ["Operations", "Approval rate", "Avg latency", "Total amount"]
    values = [data["total_operations"], ((data.get("status_summary") or {}).get("approved") or {}).get("share_pct"), (data.get("latency") or {}).get("avg_sec"), data.get("total_amount")]
    for i, (label, value) in enumerate(zip(labels, values)):
        dashboard.merge_range(4, i * 2, 4, i * 2 + 1, label, header)
        dashboard.merge_range(5, i * 2, 5, i * 2 + 1, value, kpi)
    section(dashboard, 7, "Traffic distribution: fact vs target", 3)
    row(dashboard, 8, ["Provider", "Fact share %", "Target share %", "Deviation pp"], header)
    for index, (name, metrics) in enumerate(distribution.items(), 9):
        row(dashboard, index, [name, metrics.get("share_pct"), metrics.get("target_pct"), deviation(metrics)])
    if distribution:
        last = 8 + len(distribution)
        chart(dashboard, "column", ["Dashboard", 9, 0, last, 0], [("Fact", ["Dashboard", 9, 1, last, 1]), ("Target", ["Dashboard", 9, 2, last, 2])], "F8", "Traffic distribution")
    start = 12 + len(distribution)
    section(dashboard, start, "Daily limit utilization", 3)
    row(dashboard, start + 1, ["Provider", "Used", "Limit", "Utilization %"], header)
    utilization = data["projected_daily_utilization"]
    for index, (name, metrics) in enumerate(utilization.items(), start + 2):
        row(dashboard, index, [name, metrics.get("used"), metrics.get("limit"), metrics.get("utilization_pct")])
    if utilization:
        chart(dashboard, "bar", ["Dashboard", start + 2, 0, start + 1 + len(utilization), 0], [("Utilization %", ["Dashboard", start + 2, 3, start + 1 + len(utilization), 3])], f"F{start + 18}", "Daily utilization")
    skip_start = start + len(utilization) + 5
    section(dashboard, skip_start, "Skip reasons", 3)
    row(dashboard, skip_start + 1, ["Reason", "Count"], header)
    for index, item in enumerate(data["skip_reasons"].items(), skip_start + 2):
        row(dashboard, index, list(item))
    if data["skip_reasons"]:
        end = skip_start + 1 + len(data["skip_reasons"])
        chart(dashboard, "bar", ["Dashboard", skip_start + 2, 0, end, 0], [("Skips", ["Dashboard", skip_start + 2, 1, end, 1])], f"F{max(skip_start + 1, start + 35)}", "Skip reasons")
    dashboard.freeze_panes(9, 1)

    providers = book.add_worksheet("Providers")
    headers = ["Provider", "Count", "Share %", "Target %", "Deviation pp", "Amount", "Volume share %", "Target volume %", "Approved", "Rejected", "Expired", "Approval rate %", "Avg latency", "P50 latency", "P95 latency", "Daily used", "Daily limit", "Daily utilization %", "Status", "Priority"]
    row(providers, 0, headers, header)
    for index, (name, m) in enumerate(distribution.items(), 1):
        u = utilization.get(name, {})
        p = (data.get("provider_state") or {}).get(name, {})
        l = m.get("latency") or {}
        row(providers, index, [name, m.get("count"), m.get("share_pct"), m.get("target_pct"), deviation(m), m.get("amount"), m.get("volume_share_pct"), m.get("target_volume_share_pct"),
                               m.get("approved"), m.get("rejected"), m.get("expired"), m.get("approval_rate_pct"), l.get("avg_sec"), l.get("p50_sec"), l.get("p95_sec"), u.get("used"), u.get("limit"), u.get("utilization_pct"), p.get("status"), p.get("priority")])
    providers.set_column("A:A", 25)
    providers.set_column("B:T", 19, numeric)
    providers.set_row(0, 34)
    providers.freeze_panes(1, 1)
    providers.autofilter(0, 0, len(distribution), len(headers) - 1)
    if distribution:
        providers.conditional_format(1, 11, len(distribution), 11, {"type": "3_color_scale", "min_color": "#F4CCCC", "mid_color": "#FFE699", "max_color": "#D9EAD3"})

    routing = book.add_worksheet("Routing")
    section(routing, 0, "ROUTING AND FALLBACK")
    row(routing, 2, ["Metric", "Value"], header)
    cascades = data.get("attempt_cascades") or {}
    metrics = ["operations_with_attempt_logs", "recorded_attempts", "average_recorded_attempts", "first_choice_operations", "first_choice_approval_pct", "fallback_operations", "classification_coverage_pct"]
    for index, key in enumerate(metrics, 3):
        row(routing, index, [key, cascades.get(key)])
    row(routing, 12, ["Recorded attempts", "Operations"], header)
    counts = cascades.get("attempt_count_distribution") or {}
    for index, (key, count) in enumerate(sorted(counts.items(), key=lambda item: int(item[0])), 13):
        row(routing, index, [int(key), count])
    if counts:
        chart(routing, "column", ["Routing", 13, 0, 12 + len(counts), 0], [("Operations", ["Routing", 13, 1, 12 + len(counts), 1])], "D3", "Recorded steps per operation")
    start = 16 + len(counts)
    row(routing, start, ["Attempt decision", "Count"], header)
    for index, item in enumerate((cascades.get("attempt_decisions") or {}).items(), start + 1):
        row(routing, index, list(item))
    routing.set_column("A:A", 42)
    routing.set_column("B:F", 20)

    segments = book.add_worksheet("Segments")
    names = list(distribution)
    end_column = max(2, len(names) + 2)
    start = 0
    for key, label in [("by_bank", "BANK SEGMENTS"), ("by_amount", "AMOUNT SEGMENTS")]:
        section(segments, start, label, end_column)
        row(segments, start + 2, ["Segment", "Count", "Approval %", *names], header)
        groups = (data.get("segments") or {}).get(key) or {}
        for index, (name, group) in enumerate(groups.items(), start + 3):
            row(segments, index, [name, group.get("count"), group.get("approval_rate_pct"), *[((group.get("providers") or {}).get(p) or {}).get("approval_rate_pct") for p in names]])
        if groups:
            segments.conditional_format(start + 3, 2, start + 2 + len(groups), end_column, {"type": "3_color_scale"})
        start += len(groups) + 6
    segments.set_column(0, 0, 28)
    segments.set_column(1, end_column, 18)
    segments.freeze_panes(3, 1)

    comparison = book.add_worksheet("Period Comparison")
    section(comparison, 0, "CURRENT vs PREVIOUS PERIOD")
    row(comparison, 2, ["Metric", "Current", "Previous", "Delta"], header)
    periods = data.get("period_comparison") or {}
    current, previous = periods.get("current") or {}, periods.get("previous") or {}
    pairs = [("Operations", current.get("count"), previous.get("count")), ("Amount", current.get("amount"), previous.get("amount")),
             ("Approval rate %", current.get("approval_rate_pct"), previous.get("approval_rate_pct")), ("Avg latency sec", (current.get("latency") or {}).get("avg_sec"), (previous.get("latency") or {}).get("avg_sec"))]
    for index, (label, a, b) in enumerate(pairs, 3):
        row(comparison, index, [label, a, b], numeric)
        if a is not None and b is not None:
            comparison.write_formula(index, 3, f"=B{index + 1}-C{index + 1}", numeric, a - b)
    row(comparison, 9, ["Provider", "Current share %", "Previous share %", "Delta pp"], header)
    for index, name in enumerate(names, 10):
        a, b = [((p.get("providers") or {}).get(name) or {}).get("share_pct") for p in (current, previous)]
        row(comparison, index, [name, a, b], numeric)
        if a is not None and b is not None:
            comparison.write_formula(index, 3, f"=B{index + 1}-C{index + 1}", numeric, a - b)
    start = 13 + len(names)
    row(comparison, start, ["Period", "From (inclusive)", "To (exclusive)"], header)
    row(comparison, start + 1, ["Current", current.get("from_inclusive"), current.get("to_exclusive")])
    row(comparison, start + 2, ["Previous", previous.get("from_inclusive"), previous.get("to_exclusive")])
    comparison.set_column("A:A", 30)
    comparison.set_column("B:C", 32)
    comparison.set_column("D:F", 20)

    recommendations = book.add_worksheet("Recommendations")
    section(recommendations, 0, "RECOMMENDATIONS", 1)
    row(recommendations, 1, ["#", "Recommendation"], header)
    for index, text in enumerate(data["recommendations"], 2):
        row(recommendations, index, [index - 1, text], wrap)
        recommendations.set_row(index, max(36, 16 * math.ceil(len(text) / 100)))
    row(recommendations, len(data["recommendations"]) + 4, ["Period", data.get("recommendation_period")])
    recommendations.set_column("A:A", 8)
    recommendations.set_column("B:B", 110)
    recommendations.freeze_panes(2, 0)
    for sheet in (dashboard, providers, routing, segments, comparison, recommendations):
        sheet.hide_gridlines(2)
        sheet.set_landscape()
        sheet.fit_to_pages(1, 0)
