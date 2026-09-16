"""Date and publication checks for the daily structure report."""

import argparse
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas_market_calendars as mcal

SCHEMA_VERSION = 2
MIN_COVERAGE = 0.90


def latest_completed_session(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    now = now.astimezone(timezone.utc)
    schedule = mcal.get_calendar("NYSE").schedule(
        start_date=(now - timedelta(days=30)).date(), end_date=now.date()
    )
    completed = schedule[schedule["market_close"] <= now]
    if completed.empty:
        raise ValueError("No completed US trading session found")
    return completed.index[-1].date().isoformat()


def read_previous(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("symbols"), dict):
            return {}
        return data
    except (OSError, ValueError):
        return {}


def validate_publication(data, expected_symbols, expected_session, previous=None):
    expected = set(expected_symbols)
    symbols = data.get("symbols", {})
    if not expected or not symbols or not set(symbols).issubset(expected):
        raise ValueError("Empty or unexpected symbol set; keeping previous report")
    coverage = len(symbols) / len(expected)
    if coverage < MIN_COVERAGE:
        raise ValueError(f"Coverage {coverage:.1%} is below {MIN_COVERAGE:.0%}")
    # Never silently delete an existing tracked symbol after a transient failure.
    missing_previous = (set((previous or {}).get("symbols", {})) & expected) - set(symbols)
    if missing_previous:
        raise ValueError(f"Previously published symbols missing: {sorted(missing_previous)}")
    for ticker, row in symbols.items():
        if row.get("as_of_date") != expected_session:
            raise ValueError(f"{ticker}: expected session {expected_session}, got {row.get('as_of_date')}")
        for key in ("price", "support", "resistance"):
            value = row.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{ticker}: invalid {key}")
        if row["support"] > row["resistance"]:
            raise ValueError(f"{ticker}: support exceeds resistance")
    json.dumps(data, allow_nan=False)
    return coverage


def report_is_current(data, expected_symbols, expected_session):
    if data.get("schema_version") != SCHEMA_VERSION:
        return False
    if data.get("as_of_date") != expected_session:
        return False
    if set(data.get("symbols", {})) != set(expected_symbols):
        return False
    if data.get("quality", {}).get("status") != "complete":
        return False
    try:
        validate_publication(data, expected_symbols, expected_session)
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def atomic_write(path, content):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def set_action_output(name, value):
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    from structure_analysis_charts import CORE_LIST

    session = latest_completed_session()
    current = report_is_current(read_previous("structure_data.json"), CORE_LIST, session)
    should_run = args.force or not current
    message = f"Expected session: {session}; report {'will run' if should_run else 'already complete, skipped'}"
    print(message)
    set_action_output("run", str(should_run).lower())
    set_action_output("expected_session", session)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(message + "\n\n")


if __name__ == "__main__":
    main()
