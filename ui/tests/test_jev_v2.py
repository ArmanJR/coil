"""Frozen v1 contract and offline v2 recovery, handover, and integration checks."""

import ast
import hashlib
import importlib
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from controllers import ControllerError
from path_tracking import PathTracker, advance_pose
from tinyphysics import FuturePlan, State, TinyPhysicsModel, get_available_controllers
from ui import server


def fake_response(request, **kwargs):
    payload = json.loads(request.data)
    options = payload["state"]["candidate_steering_commands"]
    choice = "pid" if "pid" in options else "hold"
    result = {
        "answers": {
            "steering": {
                "type": "choice",
                "choice": choice,
                "confidence": 1.0,
                "probabilities": {key: float(key == choice) for key in options},
            }
        },
        "usage": {"cost": 0.001},
    }
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(result).encode()
    return response


class GeometryTests(unittest.TestCase):
    def test_v1_executable_snapshot_is_frozen(self):
        tree = ast.parse((server.ROOT / "controllers/jev.py").read_text())
        tree.body = tree.body[1:]  # Module documentation may evolve; behavior may not.
        digest = hashlib.sha256(ast.dump(tree).encode()).hexdigest()
        self.assertEqual(
            digest, "b0942972a9541de68543c5f3f714d4186fe70a7f69e2accdac3a3aa22750ec8a"
        )

    def test_projection_matches_ui_conventions(self):
        self.assertEqual(advance_pose((0, 0, 0), 20, 0, 0.1), (0, 2, 0))
        x, y, heading = advance_pose((0, 0, 0), 20, 2, 0.1)
        self.assertAlmostEqual(x, 0.009999916666947328)
        self.assertAlmostEqual(y, 1.999966666833333)
        self.assertAlmostEqual(heading, 0.01)
        self.assertEqual(advance_pose((1, 2, 0.3), 0, 4, 0.1), (1, 2, 0.3))

    def test_matching_acceleration_has_no_path_error(self):
        tracker = PathTracker()
        for acceleration in (0, 0.5, -2, 1):
            tracker.update(20, acceleration, acceleration, 0.1)
        self.assertEqual(
            tracker.feedback(), {"lateral_offset_m": 0, "heading_error_rad": 0}
        )
        self.assertEqual(tracker.metrics()["path_rmse_m"], 0)
        self.assertEqual(tracker.metrics()["path_frames"], 4)


class V2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = TinyPhysicsModel(
            str(server.ROOT / "models/tinyphysics.onnx"), False
        )

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        env = patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "offline-test-key",
                "JEV_CACHE_PATH": str(self.directory / "cache.sqlite3"),
                "RUN_RESULTS_PATH": str(self.directory / "runs.csv"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        network = patch("urllib.request.urlopen", side_effect=fake_response)
        self.network = network.start()
        self.addCleanup(network.stop)
        self.v2 = importlib.reload(importlib.import_module("controllers.jev-v2"))
        self.controller = self.v2.Controller()
        self.state = State(0, 20, 0)
        self.future = FuturePlan([], [], [], [])
        server.LIVE_SESSIONS.clear()
        self.addCleanup(server.LIVE_SESSIONS.clear)

    def feedback(self, offset=0, heading=0):
        self.controller.prepare_path(
            {"lateral_offset_m": offset, "heading_error_rad": heading}
        )

    def test_discovery_and_explicit_version(self):
        self.assertIn("jev-v2", get_available_controllers())
        self.assertIn("jev-v2", server.available_controllers())
        self.assertEqual(server.controller_modes()["jev-v2"], "remote")
        self.assertEqual(
            server.validate_request({"controller": "jev-v2"})["controller"], "jev-v2"
        )
        self.feedback()
        self.controller.update(0.2, 0.2, self.state, self.future)
        payload = json.loads(self.network.call_args.args[0].data)
        self.assertEqual(payload["state"]["controller_version"], "jev-v2")

    def test_first_pid_proposal_matches_previous_steering(self):
        self.feedback()
        self.controller.prepare_step(100, 0.552865, True)
        action = self.controller.update(1.32, 1.32, self.state, self.future)
        self.assertAlmostEqual(action, 0.552865)
        self.assertAlmostEqual(
            self.controller.decision["inputs"]["pid_proposal"]["raw_command"], 0.552865
        )
        self.assertFalse(self.controller._handover_pending)

    def test_recovery_sign_bounds_and_low_speed(self):
        self.feedback(-2.68, math.radians(-1.46))
        target, correction = self.controller.recovery_target(0, 20)
        self.assertAlmostEqual(correction, 0.05)
        self.assertEqual(target, correction)
        self.feedback(2.68, math.radians(1.46))
        self.assertAlmostEqual(self.controller.recovery_target(0, 20)[1], -0.05)
        self.controller._recovery_accel = 0.8
        self.feedback(-100, -0.5)
        self.assertLessEqual(self.controller.recovery_target(0, 20)[1], 0.8)
        self.assertLessEqual(self.controller.recovery_target(4.9, 20)[0], 5)
        self.assertEqual(self.controller.recovery_target(0, 0)[1], 0)

    def test_geometric_recovery_reduces_straight_line_drift(self):
        tracker = PathTracker()
        tracker.actual = (-2.68, 0, math.radians(-1.46))
        for _ in range(150):
            self.controller.prepare_path(tracker.feedback())
            target, correction = self.controller.recovery_target(0, 20)
            self.controller._recovery_accel = correction
            tracker.update(20, target, 0, 0.1)
        self.assertLess(abs(tracker.feedback()["lateral_offset_m"]), 1.0)
        self.assertLess(abs(tracker.feedback()["heading_error_rad"]), math.radians(1))

    def test_all_candidates_obey_rate_limit_and_antiwindup(self):
        for previous, raw in ((0.5, -2), (2, 10), (-2, -10)):
            candidates, _ = self.v2.steering_candidates(previous, raw)
            self.assertEqual(len(candidates), len(set(candidates.values())))
            self.assertTrue(
                all(abs(value - previous) <= 0.150001 for value in candidates.values())
            )
            self.assertTrue(all(-2 <= value <= 2 for value in candidates.values()))
        self.feedback()
        self.controller._handover_pending = False
        self.controller._pid.error_integral = 1000
        self.controller.prepare_step(100, 0, True)
        self.controller.update(0, 0, self.state, self.future)
        self.assertAlmostEqual(self.controller._pid.error_integral, 1.5)

    def test_missing_feedback_or_failed_request_does_not_advance_memory(self):
        with self.assertRaisesRegex(ControllerError, "path feedback"):
            self.controller.update(0, 0, self.state, self.future)
        self.feedback(-2, -0.1)
        with patch.object(
            self.controller, "_request", side_effect=ControllerError("offline failure")
        ):
            with self.assertRaises(ControllerError):
                self.controller.update(0, 0, self.state, self.future)
        self.assertTrue(self.controller._handover_pending)
        self.assertEqual(self.controller._recovery_accel, 0)
        self.assertEqual(self.controller._pid.error_integral, 0)

    def test_cache_hit_preserves_v2_memory_and_version_namespace(self):
        self.feedback()
        self.controller.prepare_step(100, 0.4, True)
        self.controller.update(0.2, 0.1, self.state, self.future)
        replay = self.v2.Controller()
        replay.prepare_path({"lateral_offset_m": 0, "heading_error_rad": 0})
        replay.prepare_step(100, 0.4, True)
        with patch.object(replay, "_request", side_effect=AssertionError("Cache miss")):
            replay.update(0.2, 0.1, self.state, self.future)
        self.assertTrue(replay.decision["cacheHit"])
        self.assertEqual(
            replay._pid.error_integral, self.controller._pid.error_integral
        )
        self.assertFalse(replay._handover_pending)

    def test_switch_and_reload_preserve_geometry(self):
        live = server.start_live(self.model, {"controller": "manual"})
        for _ in range(15):
            live = {
                **live,
                **server.step_live(
                    {"id": live["id"], "expectedStep": live["step"], "steering": -0.5}
                ),
            }
        for reload in (False, True):
            sim = server.LIVE_SESSIONS[live["id"]].sim
            expected = sim.path_tracker.feedback()
            previous_steering = sim.action_history[-1]
            live = {
                **live,
                **server.step_live(
                    {
                        "id": live["id"],
                        "expectedStep": live["step"],
                        "controller": "jev-v2",
                        "reload": reload,
                    }
                ),
            }
            self.assertEqual(
                live["sample"]["decision"]["inputs"]["path_feedback"], expected
            )
            self.assertAlmostEqual(
                live["sample"]["decision"]["inputs"]["pid_proposal"]["raw_command"],
                previous_steering,
            )

    def test_full_ride_metrics_continue_past_scoring_window(self):
        live = server.start_live(self.model, {"controller": "jev-v2"})
        self.network.assert_not_called()
        for index in range(500):
            live = {
                **live,
                **server.step_live({"id": live["id"], "expectedStep": live["step"]}),
            }
            if index == 399:
                scored = live["costs"]
        self.assertEqual(live["costs"], scored)
        self.assertEqual(live["pathMetrics"]["path_frames"], 500)
        self.assertEqual(self.network.call_count, 500)
        self.assertTrue((self.directory / "path_metrics.csv").is_file())
        self.assertIn("jev-v2", (self.directory / "runs.csv").read_text())
