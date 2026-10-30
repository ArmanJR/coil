# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#   "numpy", "onnxruntime", "pandas==2.1.2", "matplotlib==3.8.1",
#   "seaborn==0.13.2", "tqdm", "python-dotenv==1.2.1",
# ]
# ///
"""Local UI for the existing TinyPhysics simulator. Run with `uv run ui/server.py`."""

import argparse
import importlib
import json
import logging
import math
import mimetypes
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

os.environ.setdefault("MPLBACKEND", "Agg")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from controllers import ControllerError  # noqa: E402
from run_results import ResultsError  # noqa: E402
from tinyphysics import (  # noqa: E402
    CONTEXT_LENGTH,
    CONTROL_START_IDX,
    COST_END_IDX,
    DEL_T,
    LAT_ACCEL_COST_MULTIPLIER,
    TinyPhysicsModel,
    TinyPhysicsSimulator,
)

LOGGER = logging.getLogger("controls.ui")
STATIC = Path(__file__).resolve().parent
DIST = STATIC / "dist"
MODEL_LOCK = threading.Lock()
LIVE_SESSIONS = {}
SCENARIOS = {
    "sweeping": {
        "label": "Sweeping bends",
        "description": "Smooth changes in curvature reveal tracking lag and overshoot.",
    },
    "lane_change": {
        "label": "Lane-change pulses",
        "description": "Alternating acceleration pulses test recovery after sharp transitions.",
    },
    "banked": {
        "label": "Banked road",
        "description": "Changing road roll adds a disturbance for the controller to reject.",
    },
}
DEFAULT_GAINS = {"p": 0.195, "i": 0.100, "d": -0.053}


class InputError(ValueError):
    """A validation message that is safe to display in the browser."""


def available_segments():
    directory = ROOT / "data"
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.glob("*.csv") if path.is_file())


def available_controllers():
    return sorted(
        path.stem
        for path in (ROOT / "controllers").glob("*.py")
        if path.stem != "__init__" and path.stem.replace("-", "_").isidentifier()
    )


def controller_modes():
    modes = {}
    for name in available_controllers():
        try:
            controller = importlib.import_module(f"controllers.{name}").Controller
            modes[name] = getattr(controller, "decision_mode", "local")
        except Exception:
            LOGGER.warning("Cannot inspect controller metadata controller=%s", name)
            modes[name] = "local"
    return modes


def validate_request(payload):
    if not isinstance(payload, dict):
        raise InputError("The request must be a JSON object.")
    source = payload.get("source", "demo")
    scenario = payload.get("scenario", "sweeping")
    controller = payload.get("controller", "pid")
    segment = payload.get("segment", "")
    if not all(
        isinstance(value, str) for value in (source, scenario, controller, segment)
    ):
        raise InputError("Source, scenario, controller, and segment must be strings.")
    if source not in ("demo", "dataset"):
        raise InputError("Choose a synthetic scenario or a local dataset segment.")
    if controller not in available_controllers():
        raise InputError("The selected controller is not available.")
    if source == "demo" and scenario not in SCENARIOS:
        raise InputError("The selected scenario is not available.")
    if source == "dataset" and segment not in available_segments():
        raise InputError(
            "Select an existing CSV file from the project's data directory."
        )
    gains = payload.get("gains", DEFAULT_GAINS)
    if not isinstance(gains, dict) or set(gains) != set(DEFAULT_GAINS):
        raise InputError("Provide all three PID gains: p, i, and d.")
    checked_gains = {}
    for key, value in gains.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InputError(f"The {key.upper()} gain must be a number.")
        if not math.isfinite(value) or not -2 <= value <= 2:
            raise InputError(f"The {key.upper()} gain must be between -2 and 2.")
        checked_gains[key] = float(value)
    return {
        "source": source,
        "scenario": scenario,
        "controller": controller,
        "segment": segment,
        "gains": checked_gains,
    }


def demo_data(scenario):
    """Generate illustrative inputs; the ONNX model still supplies the response."""
    t = np.arange(600) * DEL_T
    envelope = np.clip((t - 4) / 5, 0, 1)
    if scenario == "sweeping":
        target = envelope * (1.2 * np.sin((t - 10) * 0.24) + 0.35 * np.sin(t * 0.61))
        roll = 0.12 * np.sin(t * 0.13)
    elif scenario == "lane_change":
        target = np.zeros_like(t)
        for center, direction in ((15, 1), (28, -1), (41, 1), (53, -1)):
            target += (
                direction
                * 1.8
                * (
                    np.exp(-(((t - center) / 1.5) ** 2))
                    - np.exp(-(((t - center - 3) / 1.5) ** 2))
                )
            )
        roll = 0.08 * np.sin(t * 0.21)
    else:
        target = envelope * np.sin((t - 8) * 0.2)
        roll = 0.8 * np.sin(t * 0.28)
    speed = 22 + 3 * np.sin(t * 0.09)
    return pd.DataFrame(
        {
            "roll_lataccel": roll,
            "v_ego": speed,
            "a_ego": np.gradient(speed, DEL_T),
            "target_lataccel": target,
            "steer_command": np.clip(0.3 * (target - roll), -2, 2),
        }
    )


def validate_data(data):
    if len(data) < COST_END_IDX:
        raise InputError("A segment needs at least 500 samples for a full score.")
    if len(data) > 10000:
        raise InputError("The local UI supports segments up to 10,000 samples.")
    # Official challenge segments omit recorded steering after handover. The
    # controller supplies those actions; only warm-up steering is consumed.
    states = data.drop(columns="steer_command").to_numpy(dtype=float)
    warmup_steering = (
        data["steer_command"].iloc[:CONTROL_START_IDX].to_numpy(dtype=float)
    )
    if not np.isfinite(states).all() or not np.isfinite(warmup_steering).all():
        raise InputError("The selected segment contains missing or non-finite values.")
    return data


class DatasetSimulator(TinyPhysicsSimulator):
    def get_data(self, data_path):
        # Validate before the simulator indexes the initial context in reset().
        return validate_data(super().get_data(data_path))


class DemoSimulator(TinyPhysicsSimulator):
    def get_data(self, data_path):
        return validate_data(demo_data(data_path.removeprefix("demo:")))


class TracedController:
    def __init__(self, controller):
        self.controller = controller
        self.trace = []

    def prepare_step(self, step_index, previous_steering, active):
        prepare = getattr(self.controller, "prepare_step", None)
        if prepare is not None:
            prepare(step_index, previous_steering, active)

    def prepare_path(self, feedback):
        prepare = getattr(self.controller, "prepare_path", None)
        if prepare is not None:
            prepare(feedback)

    def update(self, target_lataccel, current_lataccel, state, future_plan):
        error = float(target_lataccel - current_lataccel)
        pid = all(
            hasattr(self.controller, attr)
            for attr in ("p", "i", "d", "error_integral", "prev_error")
        )
        terms = (
            {
                "p": float(self.controller.p * error),
                "i": float(
                    self.controller.i * (self.controller.error_integral + error)
                ),
                "d": float(self.controller.d * (error - self.controller.prev_error)),
            }
            if pid
            else None
        )
        action = float(
            self.controller.update(
                target_lataccel, current_lataccel, state, future_plan
            )
        )
        if not math.isfinite(action):
            raise InputError("The controller produced a non-finite steering command.")
        self.trace.append(
            {
                "error": error,
                "input": float(current_lataccel),
                "raw": action,
                "terms": terms,
                "decision": getattr(self.controller, "decision", None),
            }
        )
        return action


class ManualController:
    def __init__(self):
        self.steering = 0.0

    def update(self, *_args):
        return self.steering


def live_config(payload):
    if not isinstance(payload, dict):
        raise InputError("The request must be a JSON object.")
    manual = payload.get("controller") == "manual"
    config = validate_request({**payload, "controller": "pid"} if manual else payload)
    if manual:
        config["controller"] = "manual"
    return config


def make_controller(config, reload_code=False):
    name = config["controller"]
    if name == "manual":
        return TracedController(ManualController())
    module = importlib.import_module(f"controllers.{name}")
    if reload_code:
        importlib.invalidate_caches()
        module = importlib.reload(module)
        LOGGER.info("Controller code reloaded controller=%s", name)
    controller = module.Controller()
    if name == "pid":
        for key, value in config["gains"].items():
            setattr(controller, key, value)
    return TracedController(controller)


class LiveSession:
    """One model context and RNG stream, stepped on demand by the browser."""

    def __init__(self, model, config):
        self.config = config
        self.updated_at = time.monotonic()
        simulator_class = (
            DemoSimulator if config["source"] == "demo" else DatasetSimulator
        )
        data_path = (
            f"demo:{config['scenario']}"
            if config["source"] == "demo"
            else str(ROOT / "data" / config["segment"])
        )
        self.sim = simulator_class(
            model, data_path, make_controller(config, reload_code=True)
        )
        while self.sim.step_idx < CONTROL_START_IDX:
            self.sim.step()
        self.rng_state = np.random.get_state()
        self.failed = False

    def snapshot(self):
        return {
            "step": self.sim.step_idx - 1,
            "finished": self.sim.step_idx >= len(self.sim.data),
            "dt": DEL_T,
            "controlStart": CONTROL_START_IDX,
            "costEnd": COST_END_IDX,
            "totalSteps": len(self.sim.data),
            "config": self.config,
            "pathMetrics": self.sim.path_tracker.metrics(),
        }

    def advance(self, payload):
        if self.failed:
            raise InputError("This simulation failed. Reset before continuing.")
        if payload.get("expectedStep") != self.sim.step_idx - 1:
            raise InputError("The simulation step is out of sync. Reset the run.")
        if self.sim.step_idx >= len(self.sim.data):
            return {**self.snapshot(), "sample": None}
        settings = live_config({**self.config, **payload})
        for key in ("source", "scenario", "segment"):
            if settings[key] != self.config[key]:
                raise InputError("Reset the simulation to change its input source.")
        steer = payload.get("steering", 0.0)
        if (
            isinstance(steer, bool)
            or not isinstance(steer, (int, float))
            or not math.isfinite(steer)
            or not -2 <= steer <= 2
        ):
            raise InputError("Steering must be a finite number between -2 and 2.")
        reload_code = payload.get("reload", False)
        if not isinstance(reload_code, bool):
            raise InputError("Reload must be a boolean.")
        if settings["controller"] != self.config["controller"] or reload_code:
            self.sim.controller = make_controller(settings, reload_code=reload_code)
            LOGGER.info(
                "Live controller switched controller=%s step=%d",
                settings["controller"],
                self.sim.step_idx,
            )
        elif (
            settings["gains"] != self.config["gains"]
            and settings["controller"] == "pid"
        ):
            for key, value in settings["gains"].items():
                setattr(self.sim.controller.controller, key, value)
            LOGGER.info(
                "Live PID gains updated step=%d gains=%s",
                self.sim.step_idx,
                settings["gains"],
            )
        if settings["controller"] == "manual":
            self.sim.controller.controller.steering = steer
        self.config = settings
        np.random.set_state(self.rng_state)
        previous = self.sim.current_lataccel
        try:
            self.sim.step()
        except Exception:
            self.failed = True
            raise
        finally:
            self.rng_state = np.random.get_state()
        self.updated_at = time.monotonic()
        index = self.sim.step_idx - 1
        vehicle = self.sim.data.iloc[index]
        trace = self.sim.controller.trace[-1]
        sample = {
            "step": index,
            "time": (index - CONTROL_START_IDX + 1) * DEL_T,
            "target": self.sim.target_lataccel_history[-1],
            "actual": self.sim.current_lataccel,
            "steering": self.sim.action_history[-1],
            "speed": float(vehicle["v_ego"]),
            "roll": float(vehicle["roll_lataccel"]),
            "jerk": (self.sim.current_lataccel - previous) / DEL_T,
            "controller": settings["controller"],
            "headingError": math.degrees(
                self.sim.path_tracker.feedback()["heading_error_rad"]
            ),
            **trace,
        }
        count = min(self.sim.step_idx, COST_END_IDX) - CONTROL_START_IDX
        costs = self.sim.compute_cost() if count > 1 else None
        LOGGER.debug(
            "Live step=%d steer=%.3f target=%.3f actual=%.3f",
            index,
            sample["steering"],
            sample["target"],
            sample["actual"],
        )
        if index % 100 == 0 or self.sim.step_idx == len(self.sim.data):
            LOGGER.info(
                "Live progress step=%d controller=%s scored_samples=%d",
                index,
                settings["controller"],
                count,
            )
        return {
            **self.snapshot(),
            "sample": sample,
            "costs": costs,
            "scoredSamples": count,
        }


def start_live(model, payload):
    config = live_config(payload)
    with MODEL_LOCK:
        now = time.monotonic()
        for key in list(LIVE_SESSIONS):
            if now - LIVE_SESSIONS[key].updated_at > 1800:
                del LIVE_SESSIONS[key]
        previous_id = payload.get("previousId")
        if isinstance(previous_id, str):
            LIVE_SESSIONS.pop(previous_id, None)
        if len(LIVE_SESSIONS) >= 8:
            raise InputError(
                "Eight simulations are already open. Close another run or restart the local server."
            )
        session = LiveSession(model, config)
        session_id = uuid.uuid4().hex
        LIVE_SESSIONS[session_id] = session
        LOGGER.info(
            "Live simulation ready source=%s controller=%s",
            config["source"],
            config["controller"],
        )
        return {
            **session.snapshot(),
            "id": session_id,
            "sample": None,
            "costs": None,
            "plan": {
                "target": session.sim.data["target_lataccel"]
                .iloc[CONTROL_START_IDX:]
                .tolist(),
                "speed": session.sim.data["v_ego"].iloc[CONTROL_START_IDX:].tolist(),
            },
            "initialAcceleration": session.sim.current_lataccel,
        }


def step_live(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
        raise InputError("A live simulation ID is required.")
    with MODEL_LOCK:
        session = LIVE_SESSIONS.get(payload["id"])
        if session is None:
            raise InputError("This simulation has expired. Reset to start again.")
        return session.advance(payload)


def run_simulation(model, config):
    started = time.monotonic()
    baseline_name = "zero" if config["controller"] != "zero" else "pid"
    LOGGER.info(
        "Rollout started source=%s scenario=%s controller=%s baseline=%s gains=%s",
        config["source"],
        config["scenario"] if config["source"] == "demo" else "local-csv",
        config["controller"],
        baseline_name,
        config["gains"],
    )
    simulations = []
    # The original simulator uses NumPy's global RNG. Serialize paired runs so
    # simultaneous browser requests cannot corrupt reproducibility.
    with MODEL_LOCK:
        for name in (config["controller"], baseline_name):
            controller = importlib.import_module(f"controllers.{name}").Controller()
            if not simulations and name == "pid":
                for key, value in config["gains"].items():
                    setattr(controller, key, value)
            traced = TracedController(controller)
            simulator_class = (
                DemoSimulator if config["source"] == "demo" else DatasetSimulator
            )
            data_path = (
                f"demo:{config['scenario']}"
                if config["source"] == "demo"
                else str(ROOT / "data" / config["segment"])
            )
            sim = simulator_class(model, data_path, traced, debug=False)
            costs = sim.rollout()
            if not all(math.isfinite(float(value)) for value in costs.values()):
                raise InputError("The simulator produced a non-finite score.")
            simulations.append((sim, traced, costs))
    sim, traced, costs = simulations[0]
    baseline, _, baseline_costs = simulations[1]
    elapsed = time.monotonic() - started
    LOGGER.info(
        "Rollout complete samples=%d total_cost=%.3f baseline_cost=%.3f elapsed=%.2fs",
        len(sim.data),
        costs["total_cost"],
        baseline_costs["total_cost"],
        elapsed,
    )
    return {
        "config": config,
        "baseline": baseline_name,
        "costs": costs,
        "baselineCosts": baseline_costs,
        "elapsed": round(elapsed, 3),
        "dt": DEL_T,
        "controlStart": CONTROL_START_IDX,
        "costEnd": COST_END_IDX,
        "trackingWeight": LAT_ACCEL_COST_MULTIPLIER,
        "target": sim.target_lataccel_history,
        "actual": sim.current_lataccel_history,
        "baselineActual": baseline.current_lataccel_history,
        "steering": sim.action_history,
        "speed": sim.data["v_ego"].tolist(),
        "forwardAcceleration": sim.data["a_ego"].tolist(),
        "roadRoll": sim.data["roll_lataccel"].tolist(),
        "trace": [None] * CONTEXT_LENGTH + traced.trace,
    }


class Handler(BaseHTTPRequestHandler):
    model = None

    def log_message(self, format_string, *args):
        LOGGER.debug("HTTP " + format_string, *args)

    def send_json(self, status, value):
        body = json.dumps(value, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/config":
            self.send_json(
                200,
                {
                    "scenarios": SCENARIOS,
                    "segments": available_segments(),
                    "controllers": available_controllers(),
                    "controllerModes": controller_modes(),
                    "defaultGains": DEFAULT_GAINS,
                },
            )
            return
        asset = (DIST / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
        if not asset.is_relative_to(DIST.resolve()) or not asset.is_file():
            self.send_json(404, {"error": "Not found."})
            return
        body = asset.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type", mimetypes.guess_type(asset.name)[0] + "; charset=utf-8"
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in (
            "/api/simulate",
            "/api/live/start",
            "/api/live/step",
            "/api/live/close",
        ):
            self.send_json(404, {"error": "Not found."})
            return
        # Only same-origin browser requests may trigger local computation.
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            self.send_json(403, {"error": "Cross-origin requests are not supported."})
            return
        if self.headers.get_content_type() != "application/json":
            self.send_json(415, {"error": "Send application/json."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise InputError("The request body must be between 1 and 4096 bytes.")
            payload = json.loads(self.rfile.read(length))
            if path == "/api/live/start":
                result = start_live(self.model, payload)
            elif path == "/api/live/step":
                result = step_live(payload)
            elif path == "/api/live/close":
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("id"), str
                ):
                    raise InputError("A live simulation ID is required.")
                with MODEL_LOCK:
                    LIVE_SESSIONS.pop(payload["id"], None)
                result = {"closed": True}
            else:
                config = validate_request(payload)
                result = run_simulation(self.model, config)
            self.send_json(200, result)
        except (ControllerError, ResultsError) as error:
            LOGGER.warning("Controller stopped: %s", error)
            self.send_json(502, {"error": str(error)})
        except (ValueError, KeyError, pd.errors.ParserError) as error:
            LOGGER.warning("Rollout rejected (%s)", type(error).__name__)
            # Do not return raw data parsing errors, which can include CSV contents.
            message = (
                str(error)
                if isinstance(error, InputError)
                else "Invalid JSON or CSV input. Check the required dataset columns and values."
            )
            self.send_json(400, {"error": message})
        except Exception:
            LOGGER.exception("Unexpected rollout failure")
            self.send_json(
                500, {"error": "Simulation failed. See the server log for details."}
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    LOGGER.info("Loading TinyPhysics ONNX model on CPU")
    if not (DIST / "index.html").is_file():
        LOGGER.warning(
            "React build missing. Run npm --prefix ui install, then npm --prefix ui run build."
        )
    Handler.model = TinyPhysicsModel(str(ROOT / "models" / "tinyphysics.onnx"), False)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    LOGGER.info("Control Room ready at http://127.0.0.1:%d", args.port)
    LOGGER.info(
        "Synthetic scenarios ready; local dataset segments=%d",
        len(available_segments()),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Shutting down Control Room")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
