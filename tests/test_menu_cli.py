import json
import subprocess
import sys
from pathlib import Path
import yaml
from payment_routing.cli import COMMANDS
from payment_routing.config import RoutingConfig, load_yaml
from payment_routing.database import transaction
from payment_routing.menu import App, DataManager, YamlEditor
from payment_routing.utils import ROOT
from tests.support import DatabaseCase


class MenuCliTests(DatabaseCase):
    def run_script(self, name, *args, stdin=None):
        return subprocess.run([sys.executable, str(ROOT / "bin" / (name + ".py")), *map(str, args)], cwd=self.directory.name, input=stdin, text=True, encoding="utf-8", capture_output=True, timeout=30)

    def test_every_cli_help_works_from_another_directory(self):
        for command in COMMANDS:
            folder = "db" if command == "create_tables" else "scripts" if command == "validate_10" else "bin"
            result = subprocess.run([sys.executable, str(ROOT / folder / (command + ".py")), "--help"], cwd=self.directory.name, text=True, capture_output=True, timeout=10)
            with self.subTest(command=command):
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_cli_pipeline_and_no_duplicate_second_run(self):
        decisions = Path(self.directory.name) / "decisions.json"
        report = Path(self.directory.name) / "report.json"
        workbook = Path(self.directory.name) / "report.xlsx"
        for name, args in [("import_data", []), ("route", ["--output", decisions]), ("build_report", ["--output", report]), ("generate_excel_report", [report, workbook])]:
            result = self.run_script(name, "--database", self.path, *args)
            self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(90, len(json.loads(decisions.read_text(encoding="utf-8"))))
        self.assertEqual(190, json.loads(report.read_text(encoding="utf-8"))["total_operations"])
        before = self.snapshot()
        result = self.run_script("route", "--database", self.path, "--output", decisions)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(workbook.is_file())

    def test_menu_route_and_clear_mode(self):
        self.seed()
        self.db.close()
        inputs = iter(["1", "", "0"])
        output = []
        app = App(self.path, self.config, Path(self.directory.name), input_fn=lambda _: next(inputs), output_fn=output.append)
        app.clear_mode = True
        app.run()
        from payment_routing.database import connect
        self.db = connect(self.path)
        self.assertEqual(100, self.db.execute("SELECT COUNT(*) FROM operations_history").fetchone()[0])
        self.assertEqual(0, self.db.execute("SELECT COUNT(*) FROM routing_decisions").fetchone()[0])
        self.assertTrue((Path(self.directory.name) / "routing_decisions_test.json").is_file())
        self.assertTrue(any("restored to original" in text for text in output))

    def test_menu_eof_and_readiness(self):
        result = self.run_script("menu", "--database", self.path, stdin="0\n")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Start Route", result.stdout)
        result = self.run_script("menu", "--database", self.path, stdin="1\n\n0\n")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Route not started", result.stdout)

    def test_data_replace_failure_keeps_original(self):
        self.seed()
        before = self.snapshot()
        with self.assertRaises(Exception):
            DataManager(self.db, self.config).replace("providers")
        self.assertEqual(before, self.snapshot())

    def test_yaml_edits_preserve_comments_and_other_sections(self):
        config = Path(self.directory.name) / "routing.yml"
        config.write_text("# heading\nactive_strategies:\n  - priority\n\n# provider list\nrated_providers:\n  - vipay\nfallback_provider: spacepayments\n", encoding="utf-8")
        self.assertEqual(["priority", "conversion"], YamlEditor.toggle_active_strategy(config, "conversion"))
        self.assertEqual(["conversion"], YamlEditor.toggle_active_strategy(config, "priority"))
        self.assertEqual([], YamlEditor.toggle_active_strategy(config, "conversion"))
        self.assertEqual([], YamlEditor.active_strategies(config))
        self.assertEqual(["vipay"], load_yaml(config)["rated_providers"])
        self.assertIn("# provider list", config.read_text(encoding="utf-8"))
        self.assertEqual(["intensity"], YamlEditor.toggle_active_strategy(config, "intensity"))

    def test_business_and_coefficient_editor(self):
        business = Path(self.directory.name) / "business.yml"
        business.write_text("# parameters\nproviders:\n  vipay:\n    volume_share_pct: 50\n  payflow:\n    volume_share_pct: 65\n", encoding="utf-8")
        YamlEditor.set_business_parameter(business, "vipay", "daily_turnover_min", 1000)
        YamlEditor.set_business_parameter(business, "vipay", "volume_share_pct", None)
        YamlEditor.set_business_parameter(business, "new", "daily_turnover_max", 5000)
        data = load_yaml(business)["providers"]
        self.assertEqual(1000, data["vipay"]["daily_turnover_min"])
        self.assertIsNone(data["vipay"]["volume_share_pct"])
        self.assertEqual(65, data["payflow"]["volume_share_pct"])
        strategies = Path(self.directory.name) / "strategies.yml"
        strategies.write_text("# weights\nstrategies:\n  - key: priority\n    combo_coefficient: 1.6\n  - key: intensity\n    combo_coefficient: 0.6\n", encoding="utf-8")
        YamlEditor.set_combo_coefficient(strategies, "priority", 2.5)
        self.assertEqual({"priority": 2.5, "intensity": .6}, YamlEditor.combo_coefficients(strategies))
        self.assertIn("# weights", strategies.read_text(encoding="utf-8"))

    def test_bad_cli_arguments_and_output_guard(self):
        result = self.run_script("generate_excel_report", "--unknown")
        self.assertNotEqual(0, result.returncode)
        before = self.path.read_bytes()
        result = self.run_script("analyze_db", "--database", self.path, "--output", self.path)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(before, self.path.read_bytes())
