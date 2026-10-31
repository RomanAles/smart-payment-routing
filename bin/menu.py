#!/usr/bin/env python3
"""Entry point for menu; works from any current directory."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from payment_routing.cli import main

if __name__ == "__main__":
    raise SystemExit(main(command="menu"))
