"""Interactive console for routing, imports and editing the existing YAML files."""
import math
import re
from pathlib import Path
import yaml
from .config import RoutingConfig, StrategyRegistry, load_yaml
from .database import DEFAULT_PATH, clear_all, connect, create_schema, transaction
from .importers import BUSINESS_FIELDS, IMPORTERS, import_business_parameters, import_data
from .utils import ROOT, atomic_write, write_json


class YamlEditor:
    @staticmethod
    def active_strategies(path):
        return load_yaml(path).get("active_strategies") or []

    @staticmethod
    def toggle_active_strategy(path, key):
        text = Path(path).read_text(encoding="utf-8-sig")
        lines = text.splitlines(keepends=True)
        start = next((i for i, line in enumerate(lines) if line.startswith("active_strategies:")), None)
        if start is None:
            raise ValueError(f"active_strategies not found in {path}")
        end = start + 1
        while end < len(lines) and (lines[end].startswith("  ") or not lines[end].strip() or lines[end].lstrip().startswith("#")):
            end += 1
        current = YamlEditor.active_strategies(path)
        updated = [item for item in current if item != key] if key in current else [*current, key]
        comments = [line for line in lines[start + 1:end] if line.lstrip().startswith("#") or not line.strip()]
        replacement = ["active_strategies:\n", *[f"  - {item}\n" for item in updated], *comments]
        lines[start:end] = replacement
        atomic_write(path, "".join(lines))
        return updated

    @staticmethod
    def combo_coefficients(path):
        return {row["key"]: row["combo_coefficient"] for row in load_yaml(path)["strategies"]}

    @staticmethod
    def set_combo_coefficient(path, key, value):
        if not math.isfinite(value) or value < 0:
            raise ValueError("Coefficient must be finite and non-negative")
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines(keepends=True)
        start = next((i for i, line in enumerate(lines) if line.strip() == f"- key: {key}"), None)
        if start is None:
            raise ValueError(f"Strategy '{key}' not found")
        for i in range(start + 1, len(lines)):
            if lines[i].strip().startswith("- key:"):
                break
            if "combo_coefficient:" in lines[i]:
                indent = re.match(r" *", lines[i])[0]
                lines[i] = f"{indent}combo_coefficient: {value}\n"
                atomic_write(path, "".join(lines))
                return
        raise ValueError(f"combo_coefficient for '{key}' not found")

    @staticmethod
    def set_business_parameter(path, provider, field, value):
        if field not in BUSINESS_FIELDS:
            raise ValueError(f"Unknown business field: {field}")
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines(keepends=True)
        if not any(line.startswith("providers:") for line in lines):
            raise ValueError(f"providers not found in {path}")
        start = next((i for i, line in enumerate(lines) if line.rstrip() == f"  {provider}:"), None)
        scalar = "" if value is None else str(value)
        entry = f"    {field}: {scalar}\n"
        if start is None:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines += [f"  {provider}:\n", entry]
        else:
            end = start + 1
            while end < len(lines) and (lines[end].startswith("    ") or not lines[end].strip() or lines[end].lstrip().startswith("#")):
                end += 1
            index = next((i for i in range(start + 1, end) if lines[i].strip().startswith(field + ":")), None)
            if index is None:
                lines.insert(end, entry)
            else:
                lines[index] = entry
        atomic_write(path, "".join(lines))


class DataManager:
    def __init__(self, db, config):
        self.db, self.config = db, config

    def update(self, key):
        result = import_data(self.db, self.config, [key])
        return f"{key}: {result[key]} records added/updated"

    def replace(self, key):
        result = import_data(self.db, self.config, [key], replace=True)
        return f"{key}: table cleared, {result[key]} records loaded"

    def clear_all(self):
        clear_all(self.db)
        return "All database tables cleared"


class App:
    def __init__(self, database_path=DEFAULT_PATH, config=None, output_dir=ROOT, input_fn=input, output_fn=print):
        self.database_path, self.config = Path(database_path), config or RoutingConfig()
        self.output_dir = Path(output_dir)
        self.db = create_schema(connect(self.database_path))
        self.clear_mode = False
        self.input, self.output = input_fn, output_fn

    def choose(self, title, items):
        self.output("\n=== " + title + " ===")
        for index, item in enumerate(items, 1):
            self.output(f"{index}. {item}")
        return self.input("Enter item number (Enter - back, 0 - exit): ").strip()

    def press_key(self, message):
        self.output(message)
        self.input("Press Enter to continue...")

    def run(self):
        try:
            while True:
                choice = self.choose("Smart Payment Routing", ["Start Route", "Data", "Switch strategies", "Provider metrics", "Strategies priority", "Form excel analytics", f"Clear mode [{'enabled' if self.clear_mode else 'disabled'}]"])
                if choice in ("0", "q", "exit"):
                    break
                try:
                    action = {"1": self.start_route, "2": self.data_menu, "3": self.strategies_menu, "4": self.provider_metrics_menu, "5": self.strategies_priority_menu, "6": self.generate_excel_report, "7": self.toggle_clear_mode}.get(choice)
                    if action:
                        action()
                    elif choice:
                        self.press_key("Invalid input.")
                except (ValueError, OSError, RuntimeError) as exc:
                    self.press_key(f"Error: {exc}")
                except Exception as exc:
                    # Keep the terminal available after SQLite or export errors.
                    self.press_key(f"Error: {exc}")
        except (EOFError, KeyboardInterrupt):
            self.output("\nExit")
        finally:
            self.db.close()

    def start_route_blockers(self):
        blockers = []
        if not YamlEditor.active_strategies(self.config.config_file):
            blockers.append("select at least one strategy (item 3 - Switch strategies)")
        for table in ("providers", "operations_queue", "operations_history"):
            if self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0:
                blockers.append(f"no data in {table} - load it (item 2 - Data)")
        return blockers

    def start_route(self):
        from .analytics import CanonicalDatabaseAnalytics
        from .run import RoutingRun
        blockers = self.start_route_blockers()
        if blockers:
            self.press_key("Route not started:\n" + "\n".join("  - " + b for b in blockers))
            return
        cfg = RoutingConfig(self.config.config_file)
        result = RoutingRun(self.db, self.database_path, cfg).call()
        if not result.processed:
            self.press_key("No unprocessed operations in operations_queue")
            return
        decisions = self.output_dir / "routing_decisions_test.json"
        report = self.output_dir / "routing_report_test.json"
        write_json(decisions, result.decisions)
        analytics = CanonicalDatabaseAnalytics(self.database_path)
        try:
            write_json(report, analytics.report())
        finally:
            analytics.close()
        message = f"Done: {len(result.decisions)} decisions written to {decisions}\nReport written to {report}"
        if self.clear_mode:
            with transaction(self.db):
                clear_all(self.db)
                import_data(self.db, cfg)
            message += "\nClear mode: database restored to original data"
        self.press_key(message)

    def data_menu(self):
        manager = DataManager(self.db, self.config)
        while True:
            choice = self.choose("Data", ["Update", "Replace", "Clear"])
            if choice in ("", "0"):
                return
            if choice == "3":
                self.press_key(manager.clear_all())
                continue
            if choice not in ("1", "2"):
                self.press_key("Invalid input.")
                continue
            sources = ["providers", "queue", "history"]
            while True:
                selected = self.choose("Data / " + ("Update" if choice == "1" else "Replace"), [*(Path(getattr(self.config, IMPORTERS[key][2])).name for key in sources), "All"])
                if selected in ("", "0"):
                    break
                if not selected.isdecimal() or not 1 <= int(selected) <= 4:
                    self.press_key("Invalid input.")
                    continue
                targets = sources if selected == "4" else [sources[int(selected) - 1]]
                messages = []
                with transaction(self.db):
                    for key in targets:
                        messages.append(manager.update(key) if choice == "1" else manager.replace(key))
                self.press_key("\n".join(messages))

    def strategies_menu(self):
        while True:
            keys = list(YamlEditor.combo_coefficients(self.config.strategies_file))
            active = YamlEditor.active_strategies(self.config.config_file)
            selected = self.choose("Switch strategies", [f"{key} [{'x' if key in active else ' '}]" for key in keys])
            if selected in ("", "0"):
                return
            messages = []
            for token in re.split(r"[\s,]+", selected):
                if token.isdecimal() and 1 <= int(token) <= len(keys):
                    key = keys[int(token) - 1]
                    YamlEditor.toggle_active_strategy(self.config.config_file, key)
                    messages.append(f"Toggled: {key}")
                else:
                    messages.append(f"Invalid: {token}")
            self.press_key("\n".join(messages))

    def provider_metrics_menu(self):
        while True:
            names = [r[0] for r in self.db.execute("SELECT payment_system FROM providers WHERE payment_system != ? ORDER BY payment_system", (self.config.fallback_provider,))]
            if not names:
                self.press_key("No providers in the database - load data first")
                return
            selected = self.choose("Provider metrics", names)
            if selected in ("", "0"):
                return
            if selected.isdecimal() and 1 <= int(selected) <= len(names):
                self.provider_fields_menu(names[int(selected) - 1])

    def provider_fields_menu(self, name):
        import_business_parameters(self.db, self.config.business_parameters_file)
        fields = ["preferred_range_min", "preferred_range_max", "volume_share_pct", "requests_per_minute_limit", "daily_turnover_min", "daily_turnover_max"]
        while True:
            p = dict(self.db.execute("SELECT * FROM providers WHERE payment_system=?", (name,)).fetchone())
            selected = self.choose("Provider metrics / " + name, [f"{key}: {p.get(key) if p.get(key) is not None else '(not set)'}" for key in fields])
            if selected in ("", "0"):
                return
            if not selected.isdecimal() or not 1 <= int(selected) <= len(fields):
                continue
            key = fields[int(selected) - 1]
            raw = self.input(f"New value for {key} (Enter - back, 'clear' - unset): ").strip()
            if not raw:
                continue
            value = None if raw.lower() == "clear" else (float(raw) if key in ("volume_share_pct", "requests_per_minute_limit") else int(raw))
            if value is not None and (not math.isfinite(value) or value < 0 or (key == "volume_share_pct" and value > 100)):
                raise ValueError("Value must be finite and non-negative; percentage must not exceed 100")
            old = Path(self.config.business_parameters_file).read_text(encoding="utf-8")
            try:
                with transaction(self.db):
                    YamlEditor.set_business_parameter(self.config.business_parameters_file, name, key, value)
                    import_business_parameters(self.db, self.config.business_parameters_file)
            except BaseException:
                atomic_write(self.config.business_parameters_file, old)
                raise
            self.press_key(f"{key} for {name} updated: {value}")

    def strategies_priority_menu(self):
        while True:
            values = YamlEditor.combo_coefficients(self.config.strategies_file)
            keys = list(values)
            selected = self.choose("Strategies priority", [f"{key}: {values[key]}" for key in keys])
            if selected in ("", "0"):
                return
            if selected.isdecimal() and 1 <= int(selected) <= len(keys):
                raw = self.input("New combo_coefficient (Enter - back): ").strip()
                if raw:
                    YamlEditor.set_combo_coefficient(self.config.strategies_file, keys[int(selected) - 1], float(raw))
                    self.press_key("combo_coefficient updated")

    def generate_excel_report(self):
        from .excel import export_excel
        path = self.output_dir / "routing_report_test.json"
        if not path.is_file():
            self.press_key("routing_report_test.json not found - run Start Route first (item 1)")
            return
        self.press_key(f"Excel analytics report generated: {export_excel(path)}")

    def toggle_clear_mode(self):
        self.clear_mode = not self.clear_mode
        self.press_key(f"Clear mode {'enabled' if self.clear_mode else 'disabled'}.")
