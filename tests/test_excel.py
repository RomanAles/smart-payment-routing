import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET
from payment_routing.excel import export_excel, validate_report
from payment_routing.utils import ROOT, read_json, write_json

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def xml(archive, name):
    return ET.fromstring(archive.read(name))


def minimal_report():
    return {"period": None, "total_operations": 0, "distribution": {}, "skip_reasons": {}, "projected_daily_utilization": {}, "recommendations": []}


class ExcelTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / "tmp")
        self.input = Path(self.directory.name) / "input.json"
        self.output = Path(self.directory.name) / "nested/report.xlsx"

    def tearDown(self):
        self.directory.cleanup()

    def export(self, data):
        write_json(self.input, data)
        export_excel(self.input, self.output)
        return zipfile.ZipFile(self.output)

    def test_real_report_has_six_sheets_four_charts_and_cached_formula(self):
        data = read_json(ROOT / "tests/fixtures/ruby_report.json")
        with self.export(data) as archive:
            workbook = xml(archive, "xl/workbook.xml")
            self.assertEqual(["Dashboard", "Providers", "Routing", "Segments", "Period Comparison", "Recommendations"], [s.attrib["name"] for s in workbook.findall("m:sheets/m:sheet", NS)])
            dashboard = xml(archive, "xl/worksheets/sheet1.xml")
            self.assertEqual(data["total_operations"], int(dashboard.find(".//m:c[@r='A6']/m:v", NS).text))
            comparison = xml(archive, "xl/worksheets/sheet5.xml")
            self.assertEqual("B4-C4", comparison.find(".//m:c[@r='D4']/m:f", NS).text)
            self.assertEqual(data["period_comparison"]["current"]["count"] - data["period_comparison"]["previous"]["count"], int(comparison.find(".//m:c[@r='D4']/m:v", NS).text))
            self.assertEqual(4, len([n for n in archive.namelist() if n.startswith("xl/charts/chart") and n.endswith(".xml")]))
            self.assertIsNone(archive.testzip())

    def test_empty_data_has_no_charts_and_safe_segment_range(self):
        with self.export(minimal_report()) as archive:
            self.assertFalse(any(n.startswith("xl/charts/") for n in archive.namelist()))
            sheet = xml(archive, "xl/worksheets/sheet4.xml")
            self.assertEqual("A1:C1", sheet.find("m:mergeCells/m:mergeCell", NS).attrib["ref"])

    def test_many_providers_and_missing_values(self):
        data = minimal_report()
        data["distribution"] = {f"provider{i}": {"share_pct": 3.5} for i in range(1, 31)}
        data["distribution"]["provider2"]["target_pct"] = 2
        with self.export(data) as archive:
            segments = xml(archive, "xl/worksheets/sheet4.xml")
            self.assertEqual("A1:AG1", segments.find("m:mergeCells/m:mergeCell", NS).attrib["ref"])
            providers = xml(archive, "xl/worksheets/sheet2.xml")
            self.assertIsNone(providers.find(".//m:c[@r='E2']/m:v", NS))
            self.assertEqual(1.5, float(providers.find(".//m:c[@r='E3']/m:v", NS).text))

    def test_input_strings_never_become_formulas(self):
        data = minimal_report()
        data["distribution"] = {"=1+1": {}}
        data["recommendations"] = ["=1+1", "+cmd", "@SUM(A1)"]
        with self.export(data) as archive:
            for n in (1, 2, 6):
                self.assertEqual([], xml(archive, f"xl/worksheets/sheet{n}.xml").findall(".//m:f", NS))
            self.assertIn("=1+1", archive.read("xl/sharedStrings.xml").decode("utf-8"))

    def test_validation_errors_include_paths(self):
        cases = [([], "$"), ({**minimal_report(), "distribution": []}, "distribution"),
                 ({**minimal_report(), "distribution": {"vipay": {"share_pct": "oops"}}}, "distribution.vipay.share_pct"),
                 ({**minimal_report(), "segments": {"by_bank": {"bank": None}}}, "segments.by_bank.bank"),
                 ({**minimal_report(), "attempt_cascades": {"attempt_count_distribution": {"oops": 1}}}, "attempt_count_distribution.oops")]
        for data, path in cases:
            with self.subTest(path=path), self.assertRaises(ValueError) as caught:
                validate_report(data)
            self.assertIn(path, str(caught.exception))

    def test_invalid_input_preserves_existing_output(self):
        self.input.write_text("{broken", encoding="utf-8")
        self.output.parent.mkdir()
        self.output.write_bytes(b"existing report")
        with self.assertRaises(ValueError):
            export_excel(self.input, self.output)
        self.assertEqual(b"existing report", self.output.read_bytes())

    def test_bom_and_default_output(self):
        self.input.write_text(json.dumps(minimal_report()), encoding="utf-8-sig")
        output = export_excel(self.input)
        self.assertEqual(self.input.with_name("routing_analytics.xlsx"), output)
        with zipfile.ZipFile(output) as archive:
            self.assertIn("xl/workbook.xml", archive.namelist())
