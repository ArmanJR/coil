"""Cold-start remote drivers identically across selection, reset, reload and CLI."""

import csv
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tinyphysics import TinyPhysicsModel
from ui import server


def response(request, **_kwargs):
    payload = json.loads(request.data)
    candidates = payload["state"]["candidate_steering_commands"]
    choice = "pid" if "pid" in candidates else "hold"
    value = {
        "answers": {
            "steering": {
                "type": "choice",
                "choice": choice,
                "confidence": 1.0,
                "probabilities": {key: float(key == choice) for key in candidates},
            }
        },
        "usage": {"cost": 0.001},
    }
    result = MagicMock()
    result.__enter__.return_value.read.return_value = json.dumps(value).encode()
    return result


class StartupTests(unittest.TestCase):
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
        # Discovery can import controllers before this test starts. Patch both
        # their bound urlopen names and the source used by later module reloads.
        modules = [
            importlib.import_module(f"controllers.{name}") for name in ("jev", "jev-v2")
        ]
        network = patch("urllib.request.urlopen", side_effect=response)
        self.network = network.start()
        self.addCleanup(network.stop)
        for module in modules:
            binding = patch.object(module, "urlopen", self.network)
            binding.start()
            self.addCleanup(binding.stop)
        guard = patch(
            "http.client.HTTPSConnection.connect",
            side_effect=AssertionError("Real network is forbidden in startup tests"),
        )
        guard.start()
        self.addCleanup(guard.stop)
        server.LIVE_SESSIONS.clear()
        self.addCleanup(server.LIVE_SESSIONS.clear)

    def advance(self, live, **settings):
        return {
            **live,
            **server.step_live(
                {
                    "id": live["id"],
                    "expectedStep": live["step"],
                    **settings,
                }
            ),
        }

    def verify_paths(self, name):
        # Select after warm-up: this is the historical cold-start baseline.
        first = server.start_live(self.model, {"controller": "manual"})
        samples = []
        for index in range(500):
            first = self.advance(first, controller=name)
            sample = first["sample"]
            samples.append(sample)
            if index == 0:
                self.assertEqual(sample["decision"]["inputs"]["recent_frames"], [])
        self.assertEqual(self.network.call_count, 500)
        self.network.side_effect = AssertionError("Equivalent startup must use cache")

        # Reset with the driver selected: every full request must match, not just
        # the selected command or score. A missing cache entry fails this test.
        second = server.start_live(
            self.model, {"controller": name, "previousId": first["id"]}
        )
        driver = server.LIVE_SESSIONS[second["id"]].sim.controller.controller
        self.assertEqual(list(driver.history), [])
        self.assertEqual(driver._pid.error_integral, 0)
        for index in range(500):
            second = self.advance(second)
            sample = second["sample"]
            self.assertTrue(sample["decision"]["cacheHit"])
            self.assertEqual(
                sample["decision"]["inputs"], samples[index]["decision"]["inputs"]
            )
            self.assertEqual(sample["actual"], samples[index]["actual"])
            self.assertEqual(sample["steering"], samples[index]["steering"])
        self.assertEqual(second["costs"], first["costs"])

        # A paused reload on the first active frame has the same initial memory.
        reloaded = server.start_live(
            self.model, {"controller": name, "previousId": second["id"]}
        )
        for index in range(10):
            reloaded = self.advance(reloaded, reload=index == 0)
            self.assertEqual(
                reloaded["sample"]["decision"]["inputs"],
                samples[index]["decision"]["inputs"],
            )
            self.assertTrue(reloaded["sample"]["decision"]["cacheHit"])

        # Direct simulator controllers (as used by the CLI) follow the same rule
        # without depending on the UI's TracedController wrapper.
        module = importlib.import_module(f"controllers.{name}")
        direct = server.DemoSimulator(self.model, "demo:sweeping", module.Controller())
        while direct.step_idx < len(direct.data):
            direct.step()
        self.assertEqual(
            direct.current_lataccel_history[100:], [s["actual"] for s in samples]
        )
        self.assertEqual(direct.controller.cache_hits, 500)
        with (self.directory / "runs.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1, "All equivalent full rides must deduplicate")

        # Batch/legacy API traces still line up with their simulation frames.
        bulk = server.run_simulation(
            self.model, server.validate_request({"controller": name})
        )
        self.assertEqual(len(bulk["trace"]), 600)
        self.assertTrue(all(entry is None for entry in bulk["trace"][:100]))
        self.assertEqual(bulk["trace"][100]["decision"]["inputs"]["frame"], 100)
        self.assertEqual(bulk["trace"][599]["decision"]["inputs"]["frame"], 599)
        self.assertEqual(bulk["actual"][100:], [s["actual"] for s in samples])
        self.assertEqual(self.network.call_count, 500)

    def test_jev_v1_startup_and_full_cached_replay(self):
        self.verify_paths("jev")

    def test_jev_v2_startup_and_full_cached_replay(self):
        self.verify_paths("jev-v2")


if __name__ == "__main__":
    unittest.main()
