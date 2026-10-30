"""Offline Jev contract and simulator lifecycle tests; no billed requests."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from controllers import ControllerError
from controllers.jev import Controller, DELTAS, MODEL, steering_candidates
from controllers.pid import Controller as PIDController
from tinyphysics import FuturePlan, State, TinyPhysicsModel
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


def answer(choice="right_small", cost=0.0001, candidates=None):
    if candidates is None:
        candidates = [
            *DELTAS,
            "pid",
            *[f"pid_{name}" for name in DELTAS if name != "hold"],
        ]
    return {
        "answers": {
            "steering": {
                "type": "choice",
                "choice": choice,
                "confidence": 0.9,
                "probabilities": {key: float(key == choice) for key in candidates},
            }
        },
        "usage": {"cost": cost},
    }


def http_answer(request, *, timeout, choice="right_small"):
    payload = json.loads(request.data)
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        answer(choice, candidates=payload["state"]["candidate_steering_commands"])
    ).encode()
    return response


class JevTests(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        self.cache_path = str(Path(self.cache.name) / "jev.sqlite3")
        self.env = patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "test-secret",
                "JEV_CACHE_PATH": self.cache_path,
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.controller = Controller()
        self.state = State(0.1, 20, 0.2)
        self.future = FuturePlan([0.5] * 49, [0] * 49, [20] * 49, [0] * 49)

    def update(self):
        return self.controller.update(0.5, 0.1, self.state, self.future)

    def test_correction_signals_have_correct_signs_and_units(self):
        with patch.object(
            self.controller,
            "_request",
            side_effect=lambda payload: answer(
                "hold", candidates=payload["state"]["candidate_steering_commands"]
            ),
        ):
            self.update()
            first = self.controller.decision["inputs"]["correction_signals"]
            self.assertAlmostEqual(first["tracking_error_mps2"], 0.4)
            self.assertAlmostEqual(first["accumulated_error_mps"], 0.04)
            self.assertIsNone(first["observed_jerk_mps3"])
            self.assertIsNone(first["error_rate_mps3"])
            self.assertEqual(first["error_trend"], "unknown")
            self.controller.update(0.5, 0.3, self.state, self.future)
            second = self.controller.decision["inputs"]["correction_signals"]
            self.assertAlmostEqual(second["error_rate_mps3"], -2)
            self.assertAlmostEqual(second["absolute_error_rate_mps3"], -2)
            self.assertAlmostEqual(second["observed_jerk_mps3"], 2)
            self.assertAlmostEqual(second["accumulated_error_mps"], 0.06)
            self.assertEqual(second["error_trend"], "shrinking")
            self.controller.update(-0.5, -0.1, self.state, self.future)
            third = self.controller.decision["inputs"]["correction_signals"]
            self.assertAlmostEqual(third["error_rate_mps3"], -6)
            self.assertAlmostEqual(third["absolute_error_rate_mps3"], 2)
            self.assertAlmostEqual(third["observed_jerk_mps3"], -4)
            self.assertAlmostEqual(third["accumulated_error_mps"], 0.02)
            self.assertEqual(third["error_trend"], "growing")

    def test_pid_proposal_matches_existing_pid_including_warmup(self):
        baseline = PIDController()
        for frame, target, current in ((98, 0.4, 0.1), (99, 0.3, 0.4)):
            self.controller.prepare_step(frame, 0.7, False)
            self.controller.update(target, current, self.state, self.future)
            baseline.update(target, current, self.state, self.future)
        self.controller.prepare_step(100, 0.7, True)
        expected = baseline.update(0.5, 0.1, self.state, self.future)
        with patch.object(
            self.controller,
            "_request",
            side_effect=lambda payload: answer(
                "pid", candidates=payload["state"]["candidate_steering_commands"]
            ),
        ):
            action = self.update()
        self.assertAlmostEqual(
            self.controller.decision["inputs"]["pid_proposal"]["raw_command"], expected
        )
        self.assertEqual(action, round(expected, 6))
        self.assertEqual(self.controller.decision["choice"], "pid")
        self.assertAlmostEqual(
            self.controller.decision["inputs"]["correction_signals"][
                "observed_jerk_mps3"
            ],
            -3,
        )

    def test_candidates_cover_both_anchors_and_deduplicate_limits(self):
        candidates, criteria = steering_candidates(0.4, 0.9)
        self.assertEqual(candidates["hold"], 0.4)
        self.assertEqual(candidates["pid"], 0.9)
        self.assertEqual(candidates["left_small"], 0.3)
        self.assertEqual(candidates["pid_right_small"], 1)
        self.assertEqual(len(candidates), 10)
        self.assertEqual(set(candidates), set(criteria))
        for previous, raw_pid in ((2, 3), (-2, -3), (0, 0), (1.99, 2.1)):
            with self.subTest(previous=previous, pid=raw_pid):
                options, descriptions = steering_candidates(previous, raw_pid)
                self.assertEqual(len(options), len(set(options.values())))
                self.assertTrue(all(-2 <= value <= 2 for value in options.values()))
                self.assertIn(round(max(-2, min(2, raw_pid)), 6), options.values())
                self.assertEqual(options["hold"], previous)
                self.assertTrue(
                    any("PID proposal" in value for value in descriptions.values())
                )

    def test_invalid_numeric_feedback_does_not_request_or_commit_pid(self):
        with patch.object(self.controller, "_request") as request:
            with self.assertRaisesRegex(ControllerError, "non-finite"):
                self.controller.update(float("nan"), 0.1, self.state, self.future)
            request.assert_not_called()
        self.assertEqual(self.controller._pid.error_integral, 0)
        self.assertEqual(len(self.controller.history), 0)

    def test_env_file_and_missing_key(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "controllers.jev.dotenv_values",
                return_value={"OPENROUTER_API_KEY": "file-secret"},
            ),
        ):
            self.assertEqual(Controller()._api_key, "file-secret")
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("controllers.jev.dotenv_values", return_value={}),
        ):
            missing = Controller()
            self.assertEqual(missing._api_key, "")
            with self.assertRaisesRegex(ControllerError, "OPENROUTER_API_KEY"):
                missing._request({})
        with patch("controllers.jev.dotenv_values") as read_file:
            self.assertEqual(Controller()._api_key, "test-secret")
            read_file.assert_not_called()

    def test_one_call_per_frame_and_trace(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            answer()
        ).encode()
        with patch("controllers.jev.urlopen", return_value=response) as request:
            self.controller.prepare_step(100, 0.2, True)
            self.assertAlmostEqual(self.update(), 0.3)
            payload = json.loads(request.call_args.args[0].data)
            self.assertEqual(payload["model"], MODEL)
            self.assertEqual(payload["state"]["frame"], 100)
            self.assertEqual(payload["state"]["previous_steering"], 0.2)
            self.assertEqual(len(payload["state"]["future_plan"]), 17)
            self.controller.prepare_step(101, 0.3, True)
            self.assertAlmostEqual(self.update(), 0.4)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(self.controller.decision["requests"], 2)
            self.assertAlmostEqual(self.controller.decision["totalCost"], 0.0002)
            self.assertEqual(
                len(
                    json.loads(request.call_args.args[0].data)["state"]["recent_frames"]
                ),
                1,
            )
            self.assertNotIn("test-secret", json.dumps(self.controller.decision))

    def test_warmup_is_free_and_preserves_observations(self):
        with patch.object(self.controller, "_request") as request:
            for index in range(20, 100):
                self.controller.prepare_step(index, 0.4, False)
                self.assertEqual(self.update(), 0.4)
            request.assert_not_called()
            self.assertEqual(len(self.controller.history), 8)
            self.assertIsNone(self.controller.decision)

    def test_clipping_and_empty_future(self):
        self.future = FuturePlan([], [], [], [])
        with patch.object(
            self.controller,
            "_request",
            side_effect=lambda payload: answer(
                "right_small",
                candidates=payload["state"]["candidate_steering_commands"],
            ),
        ):
            self.controller.prepare_step(599, 1.95, True)
            self.assertEqual(self.update(), 2)
            self.assertEqual(self.controller.decision["inputs"]["future_plan"], [])

    def test_invalid_answers_do_not_apply_steering(self):
        malformed = []
        for field, value in (
            ("choice", "unknown"),
            ("confidence", float("nan")),
            ("confidence", True),
            ("probabilities", {}),
            ("type", "score"),
        ):
            response = answer()
            response["answers"]["steering"][field] = value
            malformed.append(response)
        malformed.extend([{}, None, {"answers": None}])
        for response in malformed:
            with (
                self.subTest(response=response),
                patch.object(self.controller, "_request", return_value=response),
            ):
                with self.assertRaisesRegex(
                    ControllerError, "invalid steering decision"
                ):
                    self.update()
                self.assertEqual(self.controller.previous_steering, 0)
                self.assertEqual(self.controller._pid.error_integral, 0)
                self.assertIsNone(self.controller.decision)

    def test_missing_cost_is_unknown_not_free(self):
        with patch.object(self.controller, "_request", return_value=answer(cost=None)):
            self.update()
        self.assertIsNone(self.controller.decision["totalCost"])

    def test_cached_response_survives_new_controller_without_key(self):
        self.controller.prepare_step(100, 0.2, True)
        with patch.object(
            self.controller, "_request", return_value=answer()
        ) as request:
            original = self.update()
            request.assert_called_once()
        replay = Controller()
        replay._api_key = ""
        replay.prepare_step(100, 0.2, True)
        with patch(
            "controllers.jev.urlopen",
            side_effect=AssertionError("Unexpected paid request"),
        ):
            self.assertEqual(replay.update(0.5, 0.1, self.state, self.future), original)
        self.assertTrue(replay.decision["cacheHit"])
        self.assertEqual(replay.decision["cacheHits"], 1)
        self.assertEqual(replay.decision["requests"], 0)
        self.assertEqual(replay.decision["totalCost"], 0)
        self.assertNotIn(b"test-secret", Path(self.cache_path).read_bytes())

    def test_state_and_model_changes_miss_cache(self):
        with patch(
            "controllers.jev.Controller._request", return_value=answer()
        ) as request:
            # Live reloads replace the module class; instantiate from the module.
            from controllers import jev

            jev.Controller().update(0.5, 0.1, self.state, self.future)
            jev.Controller().update(0.6, 0.1, self.state, self.future)
            with patch("controllers.jev.MODEL", "changed-model"):
                jev.Controller().update(0.5, 0.1, self.state, self.future)
            self.assertEqual(request.call_count, 3)

    def test_cache_failure_does_not_fall_back_to_api(self):
        with (
            patch.dict(os.environ, {"JEV_CACHE_PATH": self.cache.name}),
            patch.object(self.controller, "_request") as request,
        ):
            with self.assertRaisesRegex(
                ControllerError, "cache could not be read or saved"
            ):
                self.update()
            request.assert_not_called()

    def test_corrupt_entry_does_not_make_paid_request(self):
        with patch.object(self.controller, "_request", return_value=answer()):
            self.update()
        with sqlite3.connect(self.cache_path) as connection:
            connection.execute("UPDATE responses SET response_json = 'broken'")
        replay = Controller()
        with patch.object(replay, "_request") as request:
            with self.assertRaisesRegex(
                ControllerError, "cache could not be read or saved"
            ):
                replay.update(0.5, 0.1, self.state, self.future)
            request.assert_not_called()

    def test_invalid_response_is_not_cached(self):
        with patch.object(
            self.controller, "_request", side_effect=[{}, answer()]
        ) as request:
            with self.assertRaises(ControllerError):
                self.update()
            self.update()
            self.assertEqual(request.call_count, 2)
            self.assertFalse(self.controller.decision["cacheHit"])

    def test_request_uses_auth_timeout_and_no_retries(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            answer()
        ).encode()
        with patch("controllers.jev.urlopen", return_value=response) as opening:
            self.update()
            request = opening.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
            self.assertEqual(opening.call_args.kwargs["timeout"], 15)
            opening.assert_called_once()
        for error in (
            HTTPError("https://example.com", 429, "private body", {}, None),
            URLError("test-secret"),
            TimeoutError("test-secret"),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch("controllers.jev.urlopen", side_effect=error) as opening,
            ):
                with self.assertRaises(ControllerError) as failure:
                    self.update()
                self.assertNotIn("test-secret", str(failure.exception))
                opening.assert_called_once()


class JevLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = TinyPhysicsModel(
            str(server.ROOT / "models/tinyphysics.onnx"), False
        )

    def setUp(self):
        server.LIVE_SESSIONS.clear()
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "test-secret",
                "JEV_CACHE_PATH": str(Path(self.cache.name) / "jev.sqlite3"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(server.LIVE_SESSIONS.clear)

    def test_start_no_call_then_exactly_one_call_per_step(self):
        # Patch urlopen because live/start reloads the controller module.
        with patch(
            "urllib.request.urlopen",
            side_effect=lambda request, **kwargs: http_answer(
                request, choice="hold", **kwargs
            ),
        ) as opening:
            live = server.start_live(self.model, {"controller": "jev"})
            opening.assert_not_called()
            for index in (100, 101):
                live = {
                    **live,
                    **server.step_live(
                        {"id": live["id"], "expectedStep": live["step"]}
                    ),
                }
                self.assertEqual(live["sample"]["decision"]["inputs"]["frame"], index)
            self.assertEqual(opening.call_count, 2)
            json.dumps(live, allow_nan=False)

    def test_switch_uses_actual_previous_command(self):
        live = server.start_live(self.model, {"controller": "manual"})
        live = {
            **live,
            **server.step_live(
                {"id": live["id"], "expectedStep": live["step"], "steering": -0.7}
            ),
        }
        with patch(
            "controllers.jev.urlopen",
            side_effect=lambda request, **kwargs: http_answer(
                request, choice="hold", **kwargs
            ),
        ):
            result = server.step_live(
                {"id": live["id"], "expectedStep": live["step"], "controller": "jev"}
            )
        self.assertEqual(result["sample"]["steering"], -0.7)

    def test_request_failure_freezes_run_until_reset(self):
        live = server.start_live(self.model, {"controller": "jev"})
        request = {"id": live["id"], "expectedStep": live["step"]}
        with patch("controllers.jev.urlopen", side_effect=TimeoutError()):
            with self.assertRaises(ControllerError):
                server.step_live(request)
            with self.assertRaisesRegex(server.InputError, "Reset"):
                server.step_live(request)

    def test_full_road_replay_uses_cache_and_preserves_physics(self):
        with patch(
            "urllib.request.urlopen",
            side_effect=lambda request, **kwargs: http_answer(
                request, choice="hold", **kwargs
            ),
        ) as opening:
            first = server.start_live(self.model, {"controller": "jev"})
            first_samples = []
            for _ in range(500):
                first = {
                    **first,
                    **server.step_live(
                        {"id": first["id"], "expectedStep": first["step"]}
                    ),
                }
                first_samples.append(first["sample"]["actual"])
            self.assertEqual(opening.call_count, 500)
            opening.side_effect = AssertionError("Replay must not contact OpenRouter")
            second = server.start_live(
                self.model, {"controller": "jev", "previousId": first["id"]}
            )
            second_samples = []
            for _ in range(500):
                second = {
                    **second,
                    **server.step_live(
                        {"id": second["id"], "expectedStep": second["step"]}
                    ),
                }
                second_samples.append(second["sample"]["actual"])
            self.assertEqual(first_samples, second_samples)
            self.assertEqual(first["costs"], second["costs"])
            self.assertEqual(second["sample"]["decision"]["cacheHits"], 500)
            self.assertEqual(second["sample"]["decision"]["requests"], 0)
            self.assertEqual(second["sample"]["decision"]["totalCost"], 0)


if __name__ == "__main__":
    unittest.main()
