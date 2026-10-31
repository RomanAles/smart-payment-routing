"""Shared parsing, numeric and atomic-file helpers."""
from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc


def parse_time(value):
    if isinstance(value, datetime):
        result = value
    elif value is None or not str(value).strip():
        return None
    else:
        try:
            result = datetime.fromisoformat(str(value).strip().replace(" UTC", "+00:00").replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return result if result.tzinfo else result.replace(tzinfo=UTC)


def iso(value, microseconds=False):
    if value is None:
        return None
    return value.isoformat(timespec="microseconds" if microseconds else "seconds").replace("+00:00", "Z")


def now():
    return datetime.now(UTC)


def number(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def clean(value, digits=2):
    if value is None:
        return None
    rounded = float(Decimal(str(value)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))
    return int(rounded) if rounded.is_integer() else rounded


def percentage(numerator, denominator, digits=2):
    if numerator is None or denominator is None or denominator == 0:
        return None
    return clean(100.0 * numerator / denominator, digits)


def share(numerator, denominator):
    return 100.0 * numerator / denominator if denominator else 0.0


def clip(value, lower=0.0, upper=1.0):
    return min(max(value, lower), upper)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def ensure_writable(path, protected_roots=()):
    target = Path(path).resolve()
    for root in protected_roots:
        source = Path(root).resolve()
        if target == source or source in target.parents:
            raise ValueError(f"refusing to write inside protected source directory: {path}")
    return target


def atomic_write(path, contents, protected_roots=()):
    path = ensure_writable(path, protected_roots)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(contents)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def write_json(path, value, protected_roots=()):
    return atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", protected_roots)
