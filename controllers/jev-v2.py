"""Jev v2: projected-path recovery with a steering-matched PID handover.

This independent snapshot leaves jev.py/v1 and its cached decisions intact.
The simulator supplies PREVIOUS-frame path feedback, matching the acceleration
observation. Positive offset/heading are rightward. A bounded, rate-limited
acceleration correction rejects both lateral offset and heading drift. The
reference remains the original target trajectory; we never integrate the
corrected target into the reference path.

The first active PID update is seeded to reproduce the previous applied steering.
All candidates are limited to a 0.15 command change per 100 ms. PID integral
back-calculation prevents windup at those bounds. Jev still chooses the action.
The path gains/limits below are initial engineering defaults, not tuned results.
Route recovery can trade off the original acceleration score, which is unchanged.

Cache identity explicitly includes jev-v2. Results use the module name jev-v2;
full-ride geometry is saved separately from historical challenge-score rows.
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
VERSION = "jev-v2"
OFFSET_GAIN = 0.08  # 1/s^2
HEADING_GAIN = 0.8  # 1/s, multiplies speed * sin(heading error)
MAX_RECOVERY_ACCEL = 0.8  # m/s^2
MAX_RECOVERY_JERK = 0.5  # m/s^3
MAX_STEER_DELTA = 0.15  # simulator command per frame
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
    lower = max(-2.0, previous - MAX_STEER_DELTA)
    upper = min(2.0, previous + MAX_STEER_DELTA)
    anchors = {"current": previous, "pid": max(lower, min(upper, pid_command))}
    # Keep anchor names ahead of neighbors when their applied commands overlap.
    for option, delta in DELTAS.items():
        for source, anchor in anchors.items():
            name = (
                ("pid" if option == "hold" else f"pid_{option}")
                if source == "pid"
                else option
            )
            command = round(max(lower, min(upper, anchor + delta)), 6)
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
        self._handover_pending = True
        self._path_feedback = None
        self._recovery_accel = 0.0
        self.decision = None
        self.requests = 0
        self.cache_hits = 0
        self.total_cost = 0.0
        self.cost_complete = True
        LOGGER.info(
            "Jev v2 initialized model=%s path_gains=(%.3f,%.3f) steer_rate=%.3f timeout=15s",
            MODEL,
            OFFSET_GAIN,
            HEADING_GAIN,
            MAX_STEER_DELTA,
        )

    def prepare_path(self, feedback):
        """Geometry belongs to the simulator and survives controller reloads."""
        try:
            self._path_feedback = {
                "lateral_offset_m": number(feedback["lateral_offset_m"]),
                "heading_error_rad": number(
                    feedback["heading_error_rad"], -math.pi, math.pi
                ),
            }
        except (ValueError, TypeError, KeyError):
            raise ControllerError(
                "Jev v2 received invalid projected-path feedback."
            ) from None

    def recovery_target(self, target, speed):
        """Stable small-error PD correction: offset'' + k_h offset' + k_y offset."""
        if self._path_feedback is None:
            raise ControllerError(
                "Jev v2 needs simulator path feedback. Restart the updated server."
            )
        feedback = self._path_feedback
        desired = -OFFSET_GAIN * feedback[
            "lateral_offset_m"
        ] - HEADING_GAIN * speed * math.sin(feedback["heading_error_rad"])
        desired = max(-MAX_RECOVERY_ACCEL, min(MAX_RECOVERY_ACCEL, desired))
        change = MAX_RECOVERY_JERK * DT
        correction = max(
            self._recovery_accel - change, min(self._recovery_accel + change, desired)
        )
        if speed <= 0.1:
            correction = 0.0
        corrected = max(-5.0, min(5.0, target + correction))
        return corrected, corrected - target

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
        if not all(
            math.isfinite(float(value))
            for value in (
                target_lataccel,
                current_lataccel,
                state.v_ego,
                self.previous_steering,
            )
        ):
            raise ControllerError("Jev v2 received non-finite simulator inputs.")
        original_target = float(target_lataccel)
        corrected_target, correction = (
            self.recovery_target(original_target, max(0.0, float(state.v_ego)))
            if self.active
            else (original_target, 0.0)
        )
        observation = {
            "frame": self.step_index,
            "target_lataccel": float(target_lataccel),
            "current_lataccel": float(current_lataccel),
            "previous_steering": self.previous_steering,
            "corrected_target_lataccel": corrected_target,
        }
        # Preserve the baseline PID formula, but match the incoming steering on
        # handover. Memory is committed only after a valid/cached decision.
        proposed_pid = copy(self._pid)
        error = corrected_target - float(current_lataccel)
        if self.active and self._handover_pending:
            proposed_pid.prev_error = error
            proposed_pid.error_integral = (
                self.previous_steering - proposed_pid.p * error
            ) / proposed_pid.i - error
        pid_raw = float(
            proposed_pid.update(corrected_target, current_lataccel, state, future_plan)
        )
        lower = max(-2.0, self.previous_steering - MAX_STEER_DELTA)
        upper = min(2.0, self.previous_steering + MAX_STEER_DELTA)
        limited_pid = max(lower, min(upper, pid_raw))
        if self.active:
            proposed_pid.error_integral += (limited_pid - pid_raw) / proposed_pid.i
        previous = self.history[-1] if self.history else None
        previous_error = (
            previous["corrected_target_lataccel"] - previous["current_lataccel"]
            if previous
            else None
        )
        error_rate = (error - previous_error) / DT if previous else None
        absolute_error_rate = (
            (abs(error) - abs(previous_error)) / DT if previous else None
        )
        signals = {
            "tracking_error_mps2": original_target - float(current_lataccel),
            "recovery_tracking_error_mps2": error,
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
            "pid_integral_state": float(proposed_pid.error_integral),
        }
        if not self.active:
            self._pid = proposed_pid
            self.history.append(observation)
            return self.previous_steering

        candidates, criteria = steering_candidates(self.previous_steering, limited_pid)
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
            "controller_version": VERSION,
            "path_feedback": self._path_feedback,
            "path_recovery": {
                "original_target_lataccel": original_target,
                "corrected_target_lataccel": corrected_target,
                "correction_mps2": correction,
                "offset_gain": OFFSET_GAIN,
                "heading_gain": HEADING_GAIN,
                "max_correction_mps2": MAX_RECOVERY_ACCEL,
                "handover": self._handover_pending,
            },
            "dt_seconds": DT,
            "correction_signals": signals,
            "pid_proposal": {
                "raw_command": pid_raw,
                "bounded_command": limited_pid,
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
                        "Follow the projected reference path while limiting lateral jerk. "
                        "Track corrected_target_lataccel: it adds a bounded path-recovery correction "
                        "to the original target, which by itself cannot remove existing position/heading error. "
                        "Positive lateral_offset_m means right of the reference; positive heading_error_rad "
                        "means pointing right of it. Negative values need rightward recovery. "
                        "Follow this objective for the whole ride, including the last ten seconds. "
                        "Positive steering and lateral acceleration are rightward; negative are leftward. "
                        "Commands are normalized simulator units, NOT degrees, bounded [-2,2]. "
                        "Speed is m/s; accelerations are m/s^2. Steering response is delayed and noisy. "
                        "Use recent frames to judge response and upcoming targets to anticipate bends. "
                        "Correction signals are calculated in code: recovery error = corrected target minus actual; "
                        "negative absolute_error_rate means the error magnitude is shrinking. "
                        "Observed jerk is the change in observed lateral acceleration per second; "
                        "derivatives are null when prior observations are unavailable. "
                        "PID integral state includes handover seeding and anti-windup; it is not geometric error. "
                        "Candidates include the existing PID controller's proposal and small changes "
                        "around both that proposal and the current command. PID is an alternative, "
                        "not a required choice or a prediction of the vehicle response. "
                        "On handover PID matches existing steering. Every offered command obeys the steering rate limit. "
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
            {"controller_version": VERSION, "endpoint": ENDPOINT, "request": payload},
            lambda: self._request(payload),
            lambda response: self._validate_response(response, candidates),
        )
        elapsed_ms = (time.monotonic() - started) * 1000
        choice, confidence, probabilities, original_cost = result
        cost = 0.0 if cache_hit else original_cost
        self.cache_hits += int(cache_hit)
        action = candidates[choice]
        self._pid = proposed_pid
        self._handover_pending = False
        self._recovery_accel = correction
        self.total_cost += cost or 0
        self.cost_complete = self.cost_complete and cost is not None
        self.decision = {
            "controllerVersion": VERSION,
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
            "Jev v2 feedback frame=%s error=%.4f trend=%s jerk=%s integral=%.4f pid=%.4f candidates=%d",
            self.step_index,
            error,
            signals["error_trend"],
            signals["observed_jerk_mps3"],
            signals["pid_integral_state"],
            pid_raw,
            len(candidates),
        )
        LOGGER.info(
            "Jev v2 decision frame=%s source=%s choice=%s steer=%.4f confidence=%.3f latency_ms=%.1f cost=%s",
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
