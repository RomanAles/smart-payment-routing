"""Shared entry points for python -m payment_routing and bin/*.py."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
from .config import RoutingConfig, StrategyRegistry, StrategyWeightCalculator
from .database import DEFAULT_PATH, connect, create_schema
from .utils import ROOT, ensure_writable, parse_time, read_json, write_json

COMMANDS = ["menu", "create_tables", "import_data", "route", "build_decisions", "build_report", "analyze_db", "generate_excel_report", "log_operations", "update_provider_minute_stats", "demo_rating", "demo_hard_filter", "validate_10"]


def main(argv=None, command=None):
    # Keep redirected JSON and diagnostics UTF-8 on Windows as well as Unix.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    if command is None:
        if not argv:
            command = "menu"
        elif argv[0] in ("-h", "--help"):
            parser = argparse.ArgumentParser(description="Smart payment routing (Python)")
            parser.add_argument("command", choices=COMMANDS, nargs="?", default="menu")
            parser.print_help()
            return 0
        else:
            command = argv.pop(0)
    if command not in COMMANDS:
        print(f"Unknown command: {command}", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(prog=f"python -m payment_routing {command}")
    parser.add_argument("--database", type=Path, default=DEFAULT_PATH, help="SQLite database (default: db/operations.db)")
    parser.add_argument("--config", type=Path, default=ROOT / "config/routing.yml")
    if command in ("route", "build_decisions", "build_report", "analyze_db"):
        default = ROOT / ("routing_decisions_test.json" if command in ("route", "build_decisions") else "routing_report_test.json")
        parser.add_argument("--output", type=Path, default=default)
    if command == "analyze_db":
        parser.add_argument("--stdout", action="store_true")
    if command == "import_data":
        parser.add_argument("sources", nargs="*", help="providers business_parameters queue history (default: all)")
    if command == "menu":
        parser.add_argument("--output-dir", type=Path, default=ROOT)
    if command == "generate_excel_report":
        parser.description = "Default input: routing_report_test.json. Default output: routing_analytics.xlsx next to input."
        parser.add_argument("input", nargs="?", type=Path, default=ROOT / "routing_report_test.json")
        parser.add_argument("output", nargs="?", type=Path)
    if command == "log_operations":
        parser.add_argument("--operations", type=Path, required=True)
        parser.add_argument("--decisions", type=Path, required=True)
    if command == "update_provider_minute_stats":
        parser.add_argument("--at", help="ISO 8601 reference time (default: now, UTC)")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--window-seconds", default="all", help="positive integer or all")
    if command == "validate_10":
        parser.add_argument("decisions", type=Path)
    args = parser.parse_args(argv)
    try:
        return execute(command, args)
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as exc:
        prefix = "Excel report error" if command == "generate_excel_report" else "Error"
        print(f"{prefix}: {exc}", file=sys.stderr)
        return 1


def execute(command, args):
    if command == "menu":
        from .menu import App
        App(args.database, RoutingConfig(args.config), args.output_dir).run()
    elif command in ("create_tables", "import_data"):
        from .importers import import_data
        args.database.parent.mkdir(parents=True, exist_ok=True)
        db = connect(args.database)
        try:
            create_schema(db)
            if command == "import_data":
                for name, count in import_data(db, RoutingConfig(args.config), args.sources).items():
                    print(f"{name}: {count} records")
            else:
                print(f"Schema created: {args.database}")
        finally:
            db.close()
    elif command in ("route", "build_decisions"):
        from .persistence import DecisionsReader
        from .run import RoutingRun
        guard_output(args.output, args.database)
        if not args.database.is_file():
            raise ValueError("Database does not exist; run create_tables and import_data")
        db = connect(args.database, readonly=command == "build_decisions")
        try:
            if command == "route":
                result = RoutingRun(db, args.database, RoutingConfig(args.config)).call()
                if not result.processed:
                    print("Нет необработанных операций в operations_queue")
                    return 0
                decisions = result.decisions
            else:
                decisions = DecisionsReader(db).load()
            write_json(args.output, decisions, protected_roots())
            print(f"{len(decisions)} decisions written to {args.output}")
        finally:
            db.close()
    elif command in ("build_report", "analyze_db"):
        from .analytics import CanonicalDatabaseAnalytics
        if not getattr(args, "stdout", False):
            guard_output(args.output, args.database)
        analytics = CanonicalDatabaseAnalytics(args.database)
        try:
            report = analytics.report()
            if getattr(args, "stdout", False):
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                write_json(args.output, report, protected_roots())
                print(f"Analytics report written to {args.output}")
        finally:
            analytics.close()
    elif command == "generate_excel_report":
        from .excel import export_excel
        output = args.output or args.input.resolve().with_name("routing_analytics.xlsx")
        ensure_writable(output, protected_roots())
        if output.resolve() == args.database.resolve():
            raise ValueError("Excel output must not overwrite the database")
        print(f"Excel analytics report generated: {export_excel(args.input, output)}")
    elif command == "update_provider_minute_stats":
        from .stats import ProviderMinuteStats
        window = None if args.window_seconds == "all" else int(args.window_seconds)
        print(json.dumps(ProviderMinuteStats(args.database, at=args.at, dry_run=args.dry_run, window_seconds=window).run(), ensure_ascii=False, indent=2))
    elif command == "log_operations":
        from .persistence import DatabaseWriter
        operations, decisions = read_json(args.operations), read_json(args.decisions)
        writer = DatabaseWriter(args.database, [ROOT / "data", ROOT / "scripts"])
        try:
            count = writer.log_operations(operations if isinstance(operations, list) else [operations], decisions if isinstance(decisions, list) else [decisions])
            print(f"Logged {count} operations to {args.database}")
        finally:
            writer.close()
    elif command == "demo_rating":
        from .loaders import ProviderRegistry, HistoricalActualsProvider
        from .models import Operation
        from .rating import ProviderScoreCalculator
        cfg = RoutingConfig(args.config)
        db = connect(args.database, readonly=True)
        try:
            providers = ProviderRegistry(db, cfg.rated_providers).load()
            actuals = HistoricalActualsProvider(db).load()
            calculator = StrategyWeightCalculator(StrategyRegistry(cfg.strategies_file))
            for active in (["priority"], ["conversion"], ["intensity"], ["volume_share", "priority", "conversion"]):
                print(" / ".join(active))
                for row in ProviderScoreCalculator(**calculator.call(active)).rank(providers, actuals, Operation("demo_op", 30000)):
                    print(f"  {row.provider.payment_system}: {row.score:.6f}")
        finally:
            db.close()
    elif command == "demo_hard_filter":
        from .demos import hard_filter_demo
        hard_filter_demo()
    elif command == "validate_10":
        from .validation import validate_decisions
        errors, warnings = validate_decisions(read_json(args.decisions))
        for error in errors:
            print("FAIL: " + error)
        for warning in warnings:
            print("WARNING: " + warning)
        print(f"Ошибок: {len(errors)}; предупреждений: {len(warnings)}")
        return 1 if errors else 0
    return 0


def protected_roots():
    return [ROOT / "data", ROOT / "scripts", ROOT / "db"]


def guard_output(output, database):
    output = ensure_writable(output, protected_roots())
    if output == database.resolve() or (output.exists() and database.exists() and output.samefile(database)):
        raise ValueError("report output must not overwrite the source database")
