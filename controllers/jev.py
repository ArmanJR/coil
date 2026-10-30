"""Frozen Jev v1: acceleration tracking through OpenRouter's Decisions API.

Keep this implementation and its request payloads unchanged for reproducible
comparisons and reuse of existing cache entries. New behavior belongs in a new
versioned controller file (currently ``jev-v2.py``), not in this baseline.

Each active frame offers hold, the original PID proposal, and small adjustments
around each. Jev receives acceleration feedback, recent observations, a future
plan, and computed error/jerk signals. It does NOT receive heading or lateral
offset, so it cannot explicitly recover the projected route after drift.

Known limitations: a fresh PID starts with zero integral instead of matching the
previous steering; acceleration tracking does not ensure position tracking; the
official score excludes the final ten seconds of a 600-frame segment. Reloading
resets controller memory. Warm-up updates memory without paid requests.

Responses are cached by the complete request, not by source-code contents.
The historical ``jev`` name and cache identity are intentionally preserved.
"""

import json
import logging
import math
import os
import time
from collections import deque
from copy import copy
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import dotenv_values
from jev_cache import get_or_request

from . import BaseController, ControllerError
from .pid import Controller as PIDController

LOGGER = logging.getLogger(__name__)
MODEL = "typesafe/jev-1.14"
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
DT = 0.1
DELTAS = {
    "hold": 0.0,
    "left_fine": -0.025,
    "right_fine": 0.025,
    "left_small": -0.1,
    "right_small": 0.1,
}


def steering_candidates(previous, pid_command):
    """Offer both anchors and nearby commands, with unique applied values."""
    candidates, criteria, names_by_value = {}, {}, {}
    anchors = {"current": previous, "pid": max(-2.0, min(2.0, pid_command))}
    # Keep anchor names ahead of neighbors when their applied commands overlap.
    for option, delta in DELTAS.items():
        for source, anchor in anchors.items():
            name = (
                ("pid" if option == "hold" else f"pid_{option}")
                if source == "pid"
                else option
            )
            command = round(max(-2.0, min(2.0, anchor + delta)), 6)
            description = (
                f"{'Hold previous steering' if source == 'current' else 'Use the PID proposal'}"
                if delta == 0
                else f"Adjust the {source} command by {delta:+g}"
            ) + f", applying {command:+g}."
            if command in names_by_value:
                criteria[names_by_value[command]] += " Also: " + description
                continue
            names_by_value[command] = name
            candidates[name] = command
            criteria[name] = description
    return candidates, criteria


def number(value, lower=-math.inf, upper=math.inf):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not lower <= value <= upper
    ):
        raise ValueError("Invalid numeric decision field")
    return float(value)


class Controller(BaseController):
    decision_mode = "remote"

    def __init__(self):
        # Read only the required credential. Never copy .env contents into logs,
        # prompts, telemetry, or browser responses. Environment takes precedence.
        self._api_key = os.environ.get("OPENROUTER_API_KEY") or dotenv_values(
            ENV_PATH, interpolate=False
        ).get("OPENROUTER_API_KEY")
        self._api_key = (self._api_key or "").strip()
        self.step_index = None
        self.previous_steering = 0.0
        self.active = True
        self.history = deque(maxlen=8)
        self._pid = PIDController()
        self.decision = None
        self.requests = 0
        self.cache_hits = 0
        self.total_cost = 0.0
        self.cost_complete = True
        LOGGER.info("Jev initialized model=%s timeout=15s automatic_retries=0", MODEL)

    def prepare_step(self, step_index, previous_steering, active):
        self.step_index = step_index
        self.previous_steering = float(previous_steering)
        self.active = active

    def _request(self, payload):
        if not self._api_key:
            raise ControllerError(
                "No cached response for this frame. Set OPENROUTER_API_KEY "
                "in the project's .env file."
            )
        self.requests += 1
        LOGGER.info("Jev request frame=%s request=%d", self.step_index, self.requests)
        request = Request(
            ENDPOINT,
            data=json.dumps(payload, allow_nan=False).encode(),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as error:
            # Provider bodies may echo request content. Expose only safe status.
            status = error.code
            error.close()
            hints = {
                401: "Check OPENROUTER_API_KEY.",
                402: "Check your OpenRouter credit balance.",
                429: "OpenRouter rate limit reached.",
            }
            LOGGER.error("Jev HTTP failure frame=%s status=%d", self.step_index, status)
            raise ControllerError(
                f"Jev request failed (HTTP {status}). "
                + hints.get(status, "Check OpenRouter availability.")
            ) from None
        except (URLError, TimeoutError, OSError):
            LOGGER.error("Jev connection failure frame=%s", self.step_index)
            raise ControllerError(
                "Jev connection failed or timed out (15s timeout)."
            ) from None
        except (ValueError, UnicodeError):
            raise ControllerError("Jev returned invalid JSON.") from None

    def update(self, target_lataccel, current_lataccel, state, future_plan):
        self.decision = None
        observation = {
            "frame": self.step_index,
            "target_lataccel": float(target_lataccel),
            "current_lataccel": float(current_lataccel),
            "previous_steering": self.previous_steering,
        }
        # Use the existing PID unchanged, including its per-sample integral.
        # Commit its memory only after a successful frame (or recorded warm-up).
        proposed_pid = copy(self._pid)
        pid_raw = float(
            proposed_pid.update(target_lataccel, current_lataccel, state, future_plan)
        )
        error = float(target_lataccel - current_lataccel)
        previous = self.history[-1] if self.history else None
        previous_error = (
            previous["target_lataccel"] - previous["current_lataccel"]
            if previous
            else None
        )
        error_rate = (error - previous_error) / DT if previous else None
        absolute_error_rate = (
            (abs(error) - abs(previous_error)) / DT if previous else None
        )
        signals = {
            "tracking_error_mps2": error,
            "error_rate_mps3": error_rate,
            "absolute_error_rate_mps3": absolute_error_rate,
            "error_trend": "unknown"
            if previous is None
            else (
                "growing"
                if absolute_error_rate > 0
                else "shrinking"
                if absolute_error_rate < 0
                else "steady"
            ),
            "observed_jerk_mps3": (
                float(current_lataccel) - previous["current_lataccel"]
            )
            / DT
            if previous
            else None,
            "accumulated_error_mps": float(proposed_pid.error_integral * DT),
        }
        if not self.active:
            self._pid = proposed_pid
            self.history.append(observation)
            return self.previous_steering

        candidates, criteria = steering_candidates(self.previous_steering, pid_raw)
        preview = []
        if future_plan is not None:
            # Next second at full resolution, then a half-second preview cadence.
            for index in range(len(future_plan.lataccel)):
                if index >= 10 and (index + 1) % 5:
                    continue
                preview.append(
                    {
                        "seconds_ahead": round((index + 1) * 0.1, 1),
                        **{
                            field: float(getattr(future_plan, field)[index])
                            for field in ("lataccel", "roll_lataccel", "v_ego", "a_ego")
                        },
                    }
                )
        inputs = {
            **observation,
            "dt_seconds": DT,
            "correction_signals": signals,
            "pid_proposal": {
                "raw_command": pid_raw,
                "bounded_command": max(-2.0, min(2.0, pid_raw)),
                "gains": {key: getattr(proposed_pid, key) for key in ("p", "i", "d")},
            },
            "v_ego": float(state.v_ego),
            "a_ego": float(state.a_ego),
            "roll_lataccel": float(state.roll_lataccel),
            "recent_frames": list(self.history),
            "future_plan": preview,
            "candidate_steering_commands": candidates,
        }
        payload = {
            "model": MODEL,
            "state": inputs,
            "questions": {
                "steering": {
                    "type": "choice",
                    "instructions": (
                        "Choose one candidate steering command for this 0.1-second car simulation frame. "
                        "Track target lateral acceleration while limiting lateral jerk. "
                        "Positive steering and lateral acceleration are rightward; negative are leftward. "
                        "Commands are normalized simulator units, NOT degrees, bounded [-2,2]. "
                        "Speed is m/s; accelerations are m/s^2. Steering response is delayed and noisy. "
                        "Use recent frames to judge response and upcoming targets to anticipate bends. "
                        "Correction signals are calculated in code: error = target minus actual; "
                        "negative absolute_error_rate means the error magnitude is shrinking. "
                        "Observed jerk is the change in observed lateral acceleration per second; "
                        "derivatives are null when prior observations are unavailable. "
                        "Accumulated error is the signed time integral since controller initialization. "
                        "Candidates include the existing PID controller's proposal and small changes "
                        "around both that proposal and the current command. PID is an alternative, "
                        "not a required choice or a prediction of the vehicle response. "
                        "Prefer small changes when tracking well; avoid escalating steering when "
                        "the existing response is already approaching the target. "
                        "Choose one candidate command; hold preserves previous steering, not zero."
                    ),
                    "criteria": criteria,
                }
            },
        }
        # Validate inputs before making a billed request.
        try:
            json.dumps(payload, allow_nan=False)
        except ValueError:
            raise ControllerError("Jev received non-finite simulator inputs.") from None
        started = time.monotonic()
        result, cache_hit = get_or_request(
            {"endpoint": ENDPOINT, "request": payload},
            lambda: self._request(payload),
            lambda response: self._validate_response(response, candidates),
        )
        elapsed_ms = (time.monotonic() - started) * 1000
        choice, confidence, probabilities, original_cost = result
        cost = 0.0 if cache_hit else original_cost
        self.cache_hits += int(cache_hit)
        action = candidates[choice]
        self._pid = proposed_pid
        self.total_cost += cost or 0
        self.cost_complete = self.cost_complete and cost is not None
        self.decision = {
            "model": MODEL,
            "choice": choice,
            "confidence": confidence,
            "probabilities": probabilities,
            "candidates": candidates,
            "latencyMs": round(elapsed_ms, 1),
            "requests": self.requests,
            "cacheHit": cache_hit,
            "cacheHits": self.cache_hits,
            "cost": cost,
            "totalCost": self.total_cost if self.cost_complete else None,
            "inputs": inputs,
        }
        self.history.append({**observation, "selected_steering": action})
        self.previous_steering = action
        LOGGER.debug(
            "Jev feedback frame=%s error=%.4f trend=%s jerk=%s integral=%.4f pid=%.4f candidates=%d",
            self.step_index,
            error,
            signals["error_trend"],
            signals["observed_jerk_mps3"],
            signals["accumulated_error_mps"],
            pid_raw,
            len(candidates),
        )
        LOGGER.info(
            "Jev decision frame=%s source=%s choice=%s steer=%.4f confidence=%.3f latency_ms=%.1f cost=%s",
            self.step_index,
            "cache" if cache_hit else "openrouter",
            choice,
            action,
            confidence,
            elapsed_ms,
            cost,
        )
        return action

    def _validate_response(self, response, candidates):
        try:
            answer = response["answers"]["steering"]
            choice = answer["choice"]
            if answer["type"] != "choice" or choice not in candidates:
                raise ValueError("Unexpected choice")
            confidence = number(answer["confidence"], 0, 1)
            probabilities = answer["probabilities"]
            if set(probabilities) != set(candidates):
                raise ValueError("Unexpected alternatives")
            probabilities = {
                key: number(value, 0, 1) for key, value in probabilities.items()
            }
            # Allow rounding to hundredths for each distinct candidate.
            if not math.isclose(
                sum(probabilities.values()), 1, abs_tol=0.005 * len(candidates) + 1e-9
            ):
                raise ValueError("Invalid probability sum")
            usage = response.get("usage") or {}
            cost = number(usage["cost"], 0) if usage.get("cost") is not None else None
        except (KeyError, TypeError, ValueError, AttributeError):
            LOGGER.error("Jev invalid decision frame=%s", self.step_index)
            raise ControllerError(
                "Jev returned an invalid steering decision; no action applied."
            ) from None
        return choice, confidence, probabilities, cost
