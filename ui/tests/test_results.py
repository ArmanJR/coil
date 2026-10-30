"""Completed runs persist once, with scores, timing, and controller history."""

import csv
import hashlib
import json
import os
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from unittest.mock import patch

from controllers.zero import Controller as Zero
from run_results import FIELDS, ResultsError, append_result
from tinyphysics import TinyPhysicsModel, run_rollout
from ui import server


def write_fixture(index):
    append_result(
        {
            "run_id": str(index),
            "run_fingerprint": hashlib.sha256(str(index).encode()).hexdigest(),
            "controller": "zero",
            "total_cost": 0,
        }
    )


class ResultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = TinyPhysicsModel(
            str(server.ROOT / "models/tinyphysics.onnx"), False
        )

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "runs.csv"
        env = patch.dict(os.environ, {"RUN_RESULTS_PATH": str(self.path)})
        env.start()
        self.addCleanup(env.stop)
        server.LIVE_SESSIONS.clear()
        self.addCleanup(server.LIVE_SESSIONS.clear)

    def rows(self):
        with self.path.open(newline="") as stream:
            return list(csv.DictReader(stream))

    def test_rollout_and_exact_rerun_are_recorded_only_once(self):
        sim = server.DemoSimulator(self.model, "demo:sweeping", Zero())
        costs = sim.rollout()
        sim.step()
        sim.results.complete(sim, 100, 500, 0.1)
        (row,) = self.rows()
        for key, value in costs.items():
            self.assertEqual(float(row[key]), value)
        self.assertEqual(row["controller"], "zero")
        self.assertEqual(row["source"], "demo")
        self.assertEqual(row["segment"], "sweeping")
        self.assertEqual(row["active_frames"], "500")
        self.assertEqual(row["scored_frames"], "400")
        self.assertAlmostEqual(
            float(row["tracking_rmse"]) ** 2, costs["lataccel_cost"] / 100
        )
        self.assertGreater(float(row["processing_seconds"]), 0)
        self.assertGreaterEqual(
            float(row["elapsed_seconds"]), float(row["processing_seconds"])
        )
        self.assertEqual(row["api_requests"], "0")
        self.assertEqual(row["api_cost_usd"], "0.0")
        sim.reset()
        sim.rollout()
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], row)
        self.assertNotEqual(rows[0]["run_id"], sim.results.run_id)
        self.assertTrue(sim.results.saved)

    def test_new_simulator_deduplicates_even_with_different_cache_cost(self):
        first = Zero()
        first.decision = {"cacheHit": False, "cost": 0.001}
        server.DemoSimulator(self.model, "demo:sweeping", first).rollout()
        second = Zero()
        second.decision = {"cacheHit": True, "cost": 0}
        server.DemoSimulator(self.model, "demo:sweeping", second).rollout()
        (row,) = self.rows()
        self.assertEqual(row["api_requests"], "500")
        self.assertEqual(row["cache_hits"], "0")

    def test_same_score_with_different_controller_is_kept(self):
        from controllers.pid import Controller as PID

        pid = PID()
        pid.p = pid.i = pid.d = 0
        zero_scores = server.DemoSimulator(
            self.model, "demo:sweeping", Zero()
        ).rollout()
        pid_scores = server.DemoSimulator(self.model, "demo:sweeping", pid).rollout()
        self.assertEqual(zero_scores, pid_scores)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["run_fingerprint"], rows[1]["run_fingerprint"])

    def test_changed_road_inputs_with_same_name_are_kept(self):
        server.DemoSimulator(self.model, "demo:sweeping", Zero()).rollout()
        modified = server.demo_data("sweeping")
        modified["v_ego"] += 1
        with patch.object(server.DemoSimulator, "get_data", return_value=modified):
            server.DemoSimulator(self.model, "demo:sweeping", Zero()).rollout()
        self.assertEqual(len(self.rows()), 2)

    def test_partial_or_failed_run_is_not_saved(self):
        sim = server.DemoSimulator(self.model, "demo:sweeping", Zero())
        for _ in range(490):
            sim.step()
        self.assertFalse(self.path.exists())  # Scoring ended; the ride has not.
        with patch.object(
            sim.controller, "update", side_effect=RuntimeError("failure")
        ):
            with self.assertRaises(RuntimeError):
                sim.step()
        self.assertFalse(self.path.exists())

    def test_live_mixed_run_and_finished_poll(self):
        live = server.start_live(self.model, {"controller": "zero"})
        for index in range(500):
            settings = {"controller": "manual", "steering": 0.1} if index >= 250 else {}
            live = {
                **live,
                **server.step_live(
                    {"id": live["id"], "expectedStep": live["step"], **settings}
                ),
            }
        server.step_live({"id": live["id"], "expectedStep": live["step"]})
        (row,) = self.rows()
        self.assertEqual(row["controller"], "mixed")
        history = json.loads(row["controller_history"])
        self.assertEqual([item["start_frame"] for item in history], [100, 350])
        self.assertEqual([item["end_frame"] for item in history], [349, 599])
        self.assertEqual(float(row["total_cost"]), live["costs"]["total_cost"])

    def test_cli_entrypoint_and_ui_comparison_are_recorded(self):
        data = server.demo_data("sweeping")
        # Use the real CLI entry point, replacing only its CSV reader with demo data.
        with patch("tinyphysics.TinyPhysicsSimulator.get_data", return_value=data):
            run_rollout(
                "fixture.csv", "zero", str(server.ROOT / "models/tinyphysics.onnx")
            )
        server.run_simulation(
            self.model, server.validate_request({"controller": "pid"})
        )
        self.assertEqual(
            [row["controller"] for row in self.rows()], ["zero", "pid", "zero"]
        )

    def test_decision_costs_accumulate_across_controller_instances(self):
        sim = server.DemoSimulator(self.model, "demo:sweeping", Zero())
        while sim.step_idx < 100:
            sim.step()
        for index in range(500):
            controller = Zero()
            controller.decision = {"cacheHit": index % 2 == 0, "cost": 0.001}
            sim.controller = controller
            sim.step()
        (row,) = self.rows()
        self.assertEqual(row["api_requests"], "250")
        self.assertEqual(row["cache_hits"], "250")
        self.assertAlmostEqual(float(row["api_cost_usd"]), 0.25)

    def test_parallel_processes_append_one_header_and_complete_rows(self):
        with ProcessPoolExecutor(max_workers=3) as pool:
            list(pool.map(write_fixture, list(range(9)) * 3))
        rows = self.rows()
        self.assertEqual(len(rows), 9)
        self.assertEqual({row["run_id"] for row in rows}, {str(i) for i in range(9)})
        self.assertEqual(list(rows[0]), list(FIELDS))

    def test_incompatible_csv_is_preserved_and_write_failure_is_visible(self):
        self.path.write_text("other,header\noriginal,data\n")
        with self.assertRaisesRegex(ResultsError, "incompatible header"):
            write_fixture(1)
        self.assertEqual(self.path.read_text(), "other,header\noriginal,data\n")
        with patch.dict(os.environ, {"RUN_RESULTS_PATH": str(self.path.parent)}):
            with self.assertRaisesRegex(ResultsError, "could not be saved"):
                write_fixture(2)
