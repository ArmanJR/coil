"""Ledger analysis is read-only, handles historical gaps, and reports bad rows."""

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_results import FIELDS, PATH_FIELDS, ResultsError
from ui.analysis import load_analysis


def row(**overrides):
    return {
        **dict.fromkeys(FIELDS, "0"),
        "run_id": "run-1",
        "run_fingerprint": "fingerprint-1",
        "completed_at_utc": "2026-10-30T01:00:00-04:00",
        "source": "dataset",
        "segment": "13301.csv",
        "controller": "jev-v2",
        "controller_history": json.dumps(
            [
                {
                    "start_frame": 100,
                    "end_frame": 599,
                    "settings": {"controller": "jev-v2", "gains": {}},
                }
            ]
        ),
        "total_frames": "600",
        "active_frames": "500",
        "scored_frames": "400",
        "simulated_seconds": "50",
        "total_cost": "3332.79",
        **overrides,
    }


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "runs.csv"
        env = patch.dict(os.environ, {"RUN_RESULTS_PATH": str(self.path)})
        env.start()
        self.addCleanup(env.stop)

    def write(self, rows, path=None, fields=FIELDS):
        with (path or self.path).open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_missing_ledgers_are_empty_without_creating_files(self):
        result = load_analysis()
        self.assertEqual(result["runs"], [])
        self.assertEqual(result["warnings"], [])
        self.assertFalse(self.path.exists())

    def test_parses_history_numbers_and_timezone_without_modifying_ledger(self):
        self.write([row(api_cost_usd="")])
        before = self.path.read_bytes()
        result = load_analysis()["runs"][0]
        self.assertEqual(result["total_cost"], 3332.79)
        self.assertEqual(result["completed_at_utc"], "2026-10-30T05:00:00+00:00")
        self.assertEqual(
            result["controller_history"][0]["settings"]["controller"], "jev-v2"
        )
        self.assertIsNone(result["api_cost_usd"])
        self.assertIsNone(result["path_rmse_m"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_path_metrics_join_by_fingerprint_not_road_or_controller(self):
        self.write([row(), row(run_id="run-2", run_fingerprint="fingerprint-2")])
        metrics = {key: row().get(key, "0") for key in PATH_FIELDS}
        metrics.update(
            path_rmse_m="29.2", final_path_offset_m="-14.8", path_frames="500"
        )
        path = self.path.with_name("path_metrics.csv")
        self.write([metrics], path, PATH_FIELDS)
        before = path.read_bytes()
        runs = load_analysis()["runs"]
        self.assertEqual(runs[0]["path_rmse_m"], 29.2)
        self.assertEqual(runs[0]["final_path_offset_m"], -14.8)
        self.assertIsNone(runs[1]["path_rmse_m"])
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_and_duplicate_rows_are_counted_and_logged(self):
        self.write(
            [
                row(),
                row(),
                row(run_fingerprint="bad", total_cost="nan"),
                row(run_fingerprint="bad2", controller_history="{}"),
            ]
        )
        with self.assertLogs("ui.analysis", level="WARNING"):
            result = load_analysis()
        self.assertEqual(len(result["runs"]), 1)
        self.assertIn("3 invalid or duplicate", result["warnings"][0])

    def test_partial_row_does_not_hide_valid_history(self):
        self.write([row()])
        with self.path.open("a") as stream:
            stream.write("partial,row\n")
        result = load_analysis()
        self.assertEqual(len(result["runs"]), 1)
        self.assertEqual(len(result["warnings"]), 1)

    def test_bad_primary_header_fails_explicitly(self):
        self.path.write_text("wrong,header\n")
        with self.assertRaisesRegex(ResultsError, "incompatible CSV header"):
            load_analysis()

    def test_bad_sidecar_does_not_hide_scores(self):
        self.write([row()])
        self.path.with_name("path_metrics.csv").write_text("wrong,header\n")
        result = load_analysis()
        self.assertEqual(len(result["runs"]), 1)
        self.assertIn("Path metrics could not be loaded", result["warnings"][0])

    def test_nan_in_settings_is_rejected_before_json_response(self):
        self.write(
            [
                row(
                    controller_history='[{"start_frame":100,"end_frame":599,"settings":{"controller":"pid","gains":{"p":NaN}}}]'
                )
            ]
        )
        result = load_analysis()
        self.assertEqual(result["runs"], [])
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
