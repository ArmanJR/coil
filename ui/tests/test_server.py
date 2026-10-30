"""Verify the UI adapter against the original simulator and real ONNX model."""

import http.client
import json
import math
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from controllers.pid import Controller as PID
from controllers.zero import Controller as Zero
from tinyphysics import State, TinyPhysicsModel
from ui import server


def setUpModule():
    global results_directory, results_env
    results_directory = tempfile.TemporaryDirectory()
    results_env = patch.dict(
        os.environ, {"RUN_RESULTS_PATH": str(Path(results_directory.name) / "runs.csv")}
    )
    results_env.start()


def tearDownModule():
    results_env.stop()
    results_directory.cleanup()


class ValidationTests(unittest.TestCase):
    def test_defaults_match_original_pid(self):
        config = server.validate_request({})
        original = PID()
        for key, value in config["gains"].items():
            self.assertEqual(value, getattr(original, key))

    def test_reject_invalid_gain_values(self):
        for value in (True, "0.1", None, float("nan"), float("inf"), -2.1, 2.1):
            with self.subTest(value=value):
                with self.assertRaises(server.InputError):
                    server.validate_request({"gains": {"p": value, "i": 0.1, "d": 0}})

    def test_reject_invalid_request_shapes(self):
        for payload in (
            [],
            None,
            {"source": []},
            {"scenario": {}},
            {"controller": None},
            {"source": "remote"},
            {"scenario": "missing"},
            {"controller": "../../secrets"},
            {"gains": []},
            {"gains": {"p": 1}},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(server.InputError):
                    server.validate_request(payload)

    def test_dataset_path_traversal_is_rejected(self):
        with patch.object(server, "available_segments", return_value=["00000.csv"]):
            with self.assertRaises(server.InputError):
                server.validate_request(
                    {"source": "dataset", "segment": "../README.md"}
                )

    def test_validate_dataset_before_initializing_context(self):
        data = server.demo_data("sweeping")
        with self.assertRaisesRegex(server.InputError, "500 samples"):
            server.validate_data(data.iloc[:2])
        with self.assertRaisesRegex(server.InputError, "10,000"):
            server.validate_data(pd.concat([data] * 17))
        data.loc[12, "v_ego"] = np.nan
        with self.assertRaisesRegex(server.InputError, "non-finite"):
            server.validate_data(data)

    def test_scenarios_are_finite_and_distinct(self):
        targets = []
        for scenario in server.SCENARIOS:
            data = server.demo_data(scenario)
            self.assertEqual(len(data), 600)
            self.assertTrue(np.isfinite(data.to_numpy()).all())
            self.assertTrue((data["v_ego"] > 0).all())
            targets.append(data["target_lataccel"].to_numpy())
        for left, right in zip(targets, targets[1:]):
            self.assertFalse(np.array_equal(left, right))

    def test_missing_steering_after_handover_is_allowed(self):
        data = server.demo_data("sweeping")
        data.loc[100:, "steer_command"] = np.nan
        self.assertIs(server.validate_data(data), data)

    def test_missing_warmup_steering_is_rejected(self):
        data = server.demo_data("sweeping")
        data.loc[99, "steer_command"] = np.nan
        with self.assertRaisesRegex(server.InputError, "non-finite"):
            server.validate_data(data)

    def test_trace_records_terms_without_changing_pid(self):
        original = PID()
        traced = server.TracedController(PID())
        for target, actual in ((1, 0), (0.5, 0.1), (-1, 0.3)):
            vehicle = State(0, 20, 0)
            result = traced.update(target, actual, vehicle, None)
            self.assertEqual(result, original.update(target, actual, vehicle, None))
            self.assertAlmostEqual(sum(traced.trace[-1]["terms"].values()), result)
            self.assertEqual(traced.trace[-1]["input"], actual)

    def test_nonfinite_controller_output_rejected(self):
        class InvalidController:
            def update(self, *_args):
                return float("nan")

        with self.assertRaisesRegex(server.InputError, "non-finite steering"):
            server.TracedController(InvalidController()).update(0, 0, None, None)

    def test_csv_uses_original_column_and_sign_conventions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "segment.csv"
            pd.DataFrame(
                {
                    "roll": [0.1] * 500,
                    "vEgo": [20] * 500,
                    "aEgo": [0.2] * 500,
                    "targetLateralAcceleration": [0.4] * 500,
                    "steerCommand": [0.25] * 500,
                }
            ).to_csv(path, index=False)
            sim = server.DatasetSimulator(None, str(path), Zero())
            self.assertAlmostEqual(
                sim.data["roll_lataccel"].iloc[0], math.sin(0.1) * 9.81
            )
            self.assertEqual(sim.action_history[0], -0.25)
            self.assertEqual(sim.current_lataccel, 0.4)


class RealModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = TinyPhysicsModel(
            str(server.ROOT / "models/tinyphysics.onnx"), False
        )
        cls.config = server.validate_request({})
        cls.result = server.run_simulation(cls.model, cls.config)

    def test_costs_match_returned_scored_samples(self):
        result = self.result
        actual = np.array(result["actual"])[100:500]
        target = np.array(result["target"])[100:500]
        tracking = float(np.mean(np.square(target - actual)) * 100)
        jerk = float(np.mean(np.square(np.diff(actual) / 0.1)) * 100)
        self.assertAlmostEqual(result["costs"]["lataccel_cost"], tracking)
        self.assertAlmostEqual(result["costs"]["jerk_cost"], jerk)
        self.assertAlmostEqual(result["costs"]["total_cost"], 50 * tracking + jerk)

    def test_trace_alignment_and_control_boundaries(self):
        result = self.result
        for field in (
            "target",
            "actual",
            "baselineActual",
            "steering",
            "speed",
            "trace",
        ):
            self.assertEqual(len(result[field]), 600)
        self.assertTrue(all(item is None for item in result["trace"][:20]))
        np.testing.assert_array_equal(result["target"][:100], result["actual"][:100])
        for index in (20, 99, 100, 300, 599):
            self.assertEqual(
                result["trace"][index]["input"], result["actual"][index - 1]
            )
        self.assertLessEqual(max(abs(value) for value in result["steering"]), 2)
        self.assertLessEqual(
            np.max(np.abs(np.diff(result["actual"][99:]))), 0.5 + 1e-10
        )

    def test_repeat_run_is_identical(self):
        repeated = server.run_simulation(self.model, self.config)
        self.assertEqual(repeated["actual"], self.result["actual"])
        self.assertEqual(repeated["costs"], self.result["costs"])
        self.assertEqual(repeated["baselineActual"], self.result["baselineActual"])

    def test_zero_controller_swaps_baseline_consistently(self):
        zero = server.run_simulation(
            self.model, server.validate_request({"controller": "zero"})
        )
        self.assertEqual(zero["baseline"], "pid")
        self.assertEqual(zero["actual"], self.result["baselineActual"])
        self.assertEqual(zero["baselineActual"], self.result["actual"])
        self.assertTrue(all(value == 0 for value in zero["steering"][100:]))

    def test_zero_gains_match_zero_controller(self):
        config = server.validate_request({"gains": {"p": 0, "i": 0, "d": 0}})
        result = server.run_simulation(self.model, config)
        self.assertEqual(result["actual"], result["baselineActual"])
        self.assertEqual(result["costs"], result["baselineCosts"])

    def test_response_is_strict_json(self):
        json.dumps(self.result, allow_nan=False)


class LiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = TinyPhysicsModel(
            str(server.ROOT / "models/tinyphysics.onnx"), False
        )

    def setUp(self):
        server.LIVE_SESSIONS.clear()

    def tearDown(self):
        server.LIVE_SESSIONS.clear()

    def advance(self, state, **settings):
        result = server.step_live(
            {"id": state["id"], "expectedStep": state["step"], **settings}
        )
        return {**state, **result}

    def test_live_results_match_original_full_rollout(self):
        config = server.validate_request({})
        full = server.run_simulation(self.model, config)
        live = server.start_live(self.model, {})
        self.assertEqual(live["step"], 99)
        actual = []
        for _ in range(500):
            live = self.advance(live)
            actual.append(live["sample"]["actual"])
        self.assertEqual(actual, full["actual"][100:])
        self.assertEqual(live["costs"], full["costs"])
        self.assertTrue(live["finished"])
        self.assertEqual(live["scoredSamples"], 400)

    def test_interleaved_sessions_have_independent_rng_streams(self):
        first = server.start_live(self.model, {})
        second = server.start_live(self.model, {})
        for _ in range(15):
            first = self.advance(first)
            second = self.advance(second)
            self.assertEqual(first["sample"], second["sample"])

    def test_manual_steering_applies_on_next_step(self):
        state = server.start_live(self.model, {"controller": "manual"})
        state = self.advance(state, steering=1.25)
        self.assertEqual(state["sample"]["steering"], 1.25)
        state = self.advance(state, steering=-0.75)
        self.assertEqual(state["sample"]["steering"], -0.75)
        self.assertIsNone(state["sample"]["terms"])

    def test_manual_input_changes_the_model_response(self):
        straight = server.start_live(self.model, {"controller": "manual"})
        turning = server.start_live(self.model, {"controller": "manual"})
        for _ in range(25):
            straight = self.advance(straight, steering=0)
            turning = self.advance(turning, steering=1)
        self.assertGreater(
            turning["sample"]["actual"], straight["sample"]["actual"] + 0.2
        )

    def test_gain_change_preserves_integral_state(self):
        state = server.start_live(self.model, {})
        state = self.advance(state)
        controller = server.LIVE_SESSIONS[state["id"]].sim.controller.controller
        integral = controller.error_integral
        state = self.advance(state, gains={"p": 0.4, "i": 0.2, "d": 0})
        self.assertIs(
            server.LIVE_SESSIONS[state["id"]].sim.controller.controller, controller
        )
        self.assertAlmostEqual(
            state["sample"]["terms"]["i"], 0.2 * (integral + state["sample"]["error"])
        )

    def test_switching_driver_preserves_physics_context(self):
        state = server.start_live(self.model, {})
        state = self.advance(state)
        previous = state["sample"]["actual"]
        state = self.advance(state, controller="manual", steering=-1)
        self.assertEqual(state["sample"]["steering"], -1)
        self.assertEqual(state["sample"]["input"], previous)
        self.assertEqual(state["step"], 101)

    def test_reload_replaces_controller_but_preserves_car_state(self):
        state = server.start_live(self.model, {})
        state = self.advance(state)
        instance = server.LIVE_SESSIONS[state["id"]].sim.controller.controller
        previous = state["sample"]["actual"]
        state = self.advance(state, reload=True)
        self.assertIsNot(
            server.LIVE_SESSIONS[state["id"]].sim.controller.controller, instance
        )
        self.assertEqual(state["sample"]["input"], previous)

    def test_duplicate_step_is_rejected_without_advancing(self):
        state = server.start_live(self.model, {})
        self.advance(state)
        with self.assertRaisesRegex(server.InputError, "out of sync"):
            self.advance(state)
        self.assertEqual(server.LIVE_SESSIONS[state["id"]].sim.step_idx, 101)

    def test_invalid_steering_does_not_mutate_simulation(self):
        state = server.start_live(self.model, {"controller": "manual"})
        for value in (None, True, 3, float("nan")):
            with self.subTest(value=value), self.assertRaises(server.InputError):
                self.advance(state, steering=value)
        self.assertEqual(server.LIVE_SESSIONS[state["id"]].sim.step_idx, 100)

    def test_reset_replaces_prior_session(self):
        state = server.start_live(self.model, {})
        new = server.start_live(self.model, {"previousId": state["id"]})
        self.assertNotIn(state["id"], server.LIVE_SESSIONS)
        self.assertIn(new["id"], server.LIVE_SESSIONS)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = tempfile.TemporaryDirectory()
        dist = Path(cls.assets.name)
        (dist / "assets").mkdir()
        (dist / "index.html").write_text('<div id="root"></div>')
        (dist / "assets/app.js").write_text('console.info("test fixture");')
        (dist / "assets/style.css").write_text("body { color: white; }")
        cls.dist_patch = patch.object(server, "DIST", dist)
        cls.dist_patch.start()
        cls.server = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.dist_patch.stop()
        cls.assets.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=10
        )
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), response.getheader("Content-Type")
        finally:
            connection.close()

    def test_static_and_config_routes(self):
        for path, content_type in (
            ("/", "text/html"),
            ("/assets/app.js", "javascript"),
            ("/assets/style.css", "text/css"),
        ):
            with self.subTest(path=path):
                status, content, mime = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIn(content_type, mime)
                self.assertGreater(len(content), 0)
        status, content, _ = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertIn("pid", json.loads(content)["controllers"])

    def test_nonpublic_files_are_not_served(self):
        for path in ("/server.py", "/../README.md", "/models/tinyphysics.onnx"):
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_analysis_route_is_read_only_and_reports_storage_errors(self):
        fixture = {
            "runs": [],
            "warnings": [],
            "loaded_at_utc": "2026-10-30T05:00:00+00:00",
        }
        with (
            patch.object(server, "load_analysis", return_value=fixture),
            patch.object(server, "start_live") as start,
        ):
            status, body, mime = self.request("GET", "/api/analysis")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), fixture)
            self.assertIn("application/json", mime)
            start.assert_not_called()
        with patch.object(
            server,
            "load_analysis",
            side_effect=server.ResultsError("Saved results could not be read."),
        ):
            status, body, _ = self.request("GET", "/api/analysis")
            self.assertEqual(status, 503)
            self.assertIn("could not be read", json.loads(body)["error"])

    def test_invalid_json_returns_readable_error(self):
        status, body, _ = self.request(
            "POST", "/api/simulate", "{", {"Content-Type": "application/json"}
        )
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))

    def test_invalid_gains_do_not_start_model(self):
        status, body, _ = self.request(
            "POST",
            "/api/simulate",
            '{"gains":{"p":9,"i":0,"d":0}}',
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertIn("between -2 and 2", json.loads(body)["error"])

    def test_cross_origin_and_non_json_requests_rejected(self):
        status, _, _ = self.request(
            "POST",
            "/api/simulate",
            "{}",
            {"Origin": "https://example.com", "Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.request("POST", "/api/simulate", "{}")[0], 415)


if __name__ == "__main__":
    unittest.main()
