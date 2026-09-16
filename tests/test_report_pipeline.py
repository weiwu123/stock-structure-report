import copy
import io
import json
import os
import tempfile
import unittest
import sys
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

import report_pipeline as pipeline
import structure_analysis_charts as report


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.session = "2026-09-15"
        self.symbols = [f"TEST{i}" for i in range(10)]
        self.data = {
            "schema_version": pipeline.SCHEMA_VERSION,
            "as_of_date": self.session,
            "quality": {"status": "complete"},
            "symbols": {ticker: {
                "as_of_date": self.session, "price": 100.0,
                "support": 95.0, "resistance": 110.0,
            } for ticker in self.symbols},
        }

    def test_complete_report_skips_recovery(self):
        self.assertTrue(pipeline.report_is_current(self.data, self.symbols, self.session))

    def test_stale_partial_and_legacy_reports_retry(self):
        for mutation in (
            lambda d: d.pop("schema_version"),
            lambda d: d.update(as_of_date="2026-09-14"),
            lambda d: d["quality"].update(status="partial"),
            lambda d: d["symbols"].pop("TEST0"),
        ):
            data = copy.deepcopy(self.data)
            mutation(data)
            self.assertFalse(pipeline.report_is_current(data, self.symbols, self.session))

    def test_existing_symbol_cannot_silently_disappear(self):
        data = copy.deepcopy(self.data)
        del data["symbols"]["TEST0"]
        with self.assertRaisesRegex(ValueError, "Previously published"):
            pipeline.validate_publication(data, self.symbols, self.session, self.data)

    def test_mass_download_failure_blocks_publication(self):
        self.data["symbols"] = {"TEST0": self.data["symbols"]["TEST0"]}
        with self.assertRaisesRegex(ValueError, "Coverage"):
            pipeline.validate_publication(self.data, self.symbols, self.session)

    def test_stale_or_invalid_values_block_publication(self):
        for key, value in [("as_of_date", "2026-09-14"), ("price", float("nan")),
                           ("support", -1), ("ma20", float("inf")), ("support", 120)]:
            with self.subTest(key=key, value=value):
                data = copy.deepcopy(self.data)
                data["symbols"]["TEST0"][key] = value
                with self.assertRaises(ValueError):
                    pipeline.validate_publication(data, self.symbols, self.session)

    def test_new_symbol_failure_is_explicit_and_retryable(self):
        data = copy.deepcopy(self.data)
        del data["symbols"]["TEST0"]
        data["quality"] = {"status": "partial", "failed_symbols": {"TEST0": "unavailable"}}
        pipeline.validate_publication(data, self.symbols, self.session)
        self.assertFalse(pipeline.report_is_current(data, self.symbols, self.session))


class CalendarTests(unittest.TestCase):
    def test_completed_sessions_include_weekends_holidays_dst_and_early_close(self):
        cases = {
            "2026-09-16T08:05:00+08:00": "2026-09-15",
            "2026-09-19T08:05:00+08:00": "2026-09-18",
            "2026-09-08T08:05:00+08:00": "2026-09-04",  # Labor Day
            "2026-01-06T08:05:00+08:00": "2026-01-05",  # winter
            "2026-09-15T19:59:00+00:00": "2026-09-14",  # before close
            "2026-11-27T17:59:00+00:00": "2026-11-25",  # Thanksgiving Friday
            "2026-11-27T18:01:00+00:00": "2026-11-27",  # early close
        }
        for now, expected in cases.items():
            with self.subTest(now=now):
                self.assertEqual(pipeline.latest_completed_session(datetime.fromisoformat(now)), expected)


class ReportIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        old_cwd = os.getcwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, old_cwd)
        self.env = patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        index = pd.bdate_range(end="2026-09-16", periods=260, tz="America/New_York")
        close = 100 + np.sin(np.arange(260) / 5) * 5
        self.hist = pd.DataFrame({"Open": close, "Close": close, "High": close + 1,
                                  "Low": close - 1, "Volume": 10000}, index=index)

    def run_report(self, download, render=None):
        with patch.object(report, "CORE_LIST", ["TEST"]), \
             patch.object(report, "latest_completed_session", return_value="2026-09-15"), \
             patch.object(report, "get_history", side_effect=download), \
             patch.object(report.time, "sleep"), \
             patch.object(report, "build_html", side_effect=render or (lambda *_: "<body>report</body>")), \
             redirect_stdout(io.StringIO()):
            report.main()

    def test_retry_and_exclude_unfinished_bar_preserve_legacy_fields(self):
        self.run_report([None, self.hist])
        data = json.loads(Path("structure_data.json").read_text(encoding="utf-8"))
        row = data["symbols"]["TEST"]
        self.assertEqual(row["as_of_date"], "2026-09-15")
        self.assertEqual(row["history_rows"], 259)
        self.assertAlmostEqual(row["price"], self.hist["Close"].iloc[-2])
        self.assertIn("ma200", row)
        self.assertIn("fib60", row)
        self.assertIn("2026-09-15", Path("report_charts.html").read_text(encoding="utf-8"))

    def test_failed_download_or_render_keeps_previous_files(self):
        for downloads, render in [([None, None, None], None),
                                  ([self.hist], lambda *_: (_ for _ in ()).throw(RuntimeError("chart failed")))]:
            Path("structure_data.json").write_text('{"symbols": {}}', encoding="utf-8")
            Path("report_charts.html").write_text("previous chart", encoding="utf-8")
            with self.assertRaises((ValueError, RuntimeError)):
                self.run_report(downloads, render)
            self.assertEqual(Path("structure_data.json").read_text(), '{"symbols": {}}')
            self.assertEqual(Path("report_charts.html").read_text(), "previous chart")

    def test_provider_delay_keeps_previous_files_for_scheduled_retry(self):
        stale = self.hist.loc[self.hist.index.strftime("%Y-%m-%d") <= "2026-09-14"]
        Path("structure_data.json").write_text('{"symbols": {}}', encoding="utf-8")
        Path("report_charts.html").write_text("previous chart", encoding="utf-8")
        self.run_report([stale, stale, stale])
        self.assertEqual(Path("structure_data.json").read_text(), '{"symbols": {}}')
        self.assertEqual(Path("report_charts.html").read_text(), "previous chart")


if __name__ == "__main__":
    unittest.main()
