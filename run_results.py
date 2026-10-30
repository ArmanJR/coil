"""Append completed simulator runs to a process-safe CSV ledger."""

import csv
import fcntl
import hashlib
import json
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from numbers import Real

LOGGER = logging.getLogger(__name__)
DEFAULT_PATH = Path(__file__).resolve().parent / "results" / "runs.csv"
FIELDS = (
    "run_id",
    "run_fingerprint",
    "completed_at_utc",
    "source",
    "segment",
    "controller",
    "controller_history",
    "total_frames",
    "active_frames",
    "scored_frames",
    "simulated_seconds",
    "elapsed_seconds",
    "processing_seconds",
    "mean_step_ms",
    "max_step_ms",
    "lataccel_cost",
    "jerk_cost",
    "total_cost",
    "tracking_rmse",
    "max_tracking_error",
    "jerk_rms",
    "max_abs_steering",
    "api_requests",
    "cache_hits",
    "api_cost_usd",
)
PATH_FIELDS = (
    "run_fingerprint",
    "completed_at_utc",
    "source",
    "segment",
    "controller",
    "path_frames",
    "path_rmse_m",
    "max_path_offset_m",
    "final_path_offset_m",
    "max_heading_error_deg",
    "final_heading_error_deg",
)


class ResultsError(RuntimeError):
    """A safe, user-visible result persistence failure."""


def append_result(row, *, path=None, fields=FIELDS):
    path = (
        Path(path)
        if path is not None
        else Path(os.environ.get("RUN_RESULTS_PATH", DEFAULT_PATH))
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", newline="", encoding="utf-8") as stream:
            # Lock the CSV across threads and batch-evaluation processes, including
            # header creation. Never overwrite an existing incompatible ledger.
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stream.seek(0)
            header = next(csv.reader(stream), None)
            if header is not None and header != list(fields):
                raise ResultsError(
                    "Results CSV has an incompatible header; choose a new RUN_RESULTS_PATH."
                )
            if header is not None:
                fingerprint_index = header.index("run_fingerprint")
                for existing in csv.reader(stream):
                    if len(existing) != len(fields):
                        raise ResultsError(
                            "Results CSV contains an incomplete row; repair it before saving more results."
                        )
                    if existing[fingerprint_index] == row["run_fingerprint"]:
                        LOGGER.info(
                            "Duplicate completed run skipped run=%s fingerprint=%s",
                            row.get("run_id", row["run_fingerprint"]),
                            row["run_fingerprint"][:12],
                        )
                        return False
            stream.seek(0, os.SEEK_END)
            offset = stream.tell()
            try:
                writer = csv.DictWriter(stream, fieldnames=fields)
                if header is None:
                    writer.writeheader()
                writer.writerow(row)
                stream.flush()
                os.fsync(stream.fileno())
            except OSError:
                # Avoid leaving a partial row on a failed write when possible.
                stream.seek(offset)
                stream.truncate()
                raise
        LOGGER.info(
            "Completed run saved run=%s controller=%s score=%s",
            row.get("run_id", row["run_fingerprint"]),
            row["controller"],
            row.get("total_cost", "unscored"),
        )
        return True
    except (OSError, csv.Error, UnicodeError) as error:
        LOGGER.error("Run result save failed error_type=%s", type(error).__name__)
        raise ResultsError(
            "Run completed, but results could not be saved. Check results/runs.csv "
            "(or RUN_RESULTS_PATH) and its directory permissions."
        ) from None


class RunResults:
    def __init__(self):
        self.run_id = uuid.uuid4().hex
        self.saved = False
        self.started = None
        self.processing_seconds = 0.0
        self.max_step_seconds = 0.0
        self.history = []
        self.previous_controller = None
        self.api_requests = 0
        self.cache_hits = 0
        self.api_cost = 0.0
        self.cost_known = True

    def begin_step(self):
        started = time.monotonic()
        if self.started is None:
            self.started = started
        return started

    def record_step(self, controller, index, started):
        elapsed = time.monotonic() - started
        self.processing_seconds += elapsed
        self.max_step_seconds = max(self.max_step_seconds, elapsed)
        # The UI wraps controllers to capture telemetry; unwrap exactly that layer.
        controller = getattr(controller, "controller", controller)
        module = type(controller).__module__
        name = (
            module.removeprefix("controllers.")
            if module.startswith("controllers.")
            else type(controller).__name__
        )
        if module in ("ui.server", "__main__") and name == "ManualController":
            name = "manual"
        gains = {
            key: float(value)
            for key in ("p", "i", "d")
            if isinstance(value := getattr(controller, key, None), Real)
            and not isinstance(value, bool)
            and math.isfinite(value)
        }
        settings = {"controller": name, "gains": gains}
        if (
            self.previous_controller is not controller
            or not self.history
            or self.history[-1]["settings"] != settings
        ):
            self.history.append(
                {"start_frame": index, "end_frame": index, "settings": settings}
            )
        else:
            self.history[-1]["end_frame"] = index
        self.previous_controller = controller
        decision = getattr(controller, "decision", None)
        if decision is not None:
            cached = bool(decision.get("cacheHit", False))
            self.cache_hits += int(cached)
            self.api_requests += int(not cached)
            cost = 0.0 if cached else decision.get("cost")
            if cost is None:
                self.cost_known = False
            else:
                self.api_cost += float(cost)

    def complete(self, sim, control_start, cost_end, dt):
        if self.saved or sim.step_idx < len(sim.data):
            return
        count = max(0, min(len(sim.data), cost_end) - control_start)
        active = max(0, len(sim.data) - control_start)
        scores = sim.compute_cost() if count > 1 else {}
        target = sim.target_lataccel_history[control_start:cost_end]
        actual = sim.current_lataccel_history[control_start:cost_end]
        names = list(
            dict.fromkeys(item["settings"]["controller"] for item in self.history)
        )
        source = "demo" if sim.data_path.startswith("demo:") else "dataset"
        row = {
            "run_id": self.run_id,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "segment": sim.data_path.removeprefix("demo:")
            if source == "demo"
            else Path(sim.data_path).name,
            "controller": names[0] if len(names) == 1 else "mixed",
            "controller_history": json.dumps(
                self.history, allow_nan=False, separators=(",", ":")
            ),
            "total_frames": len(sim.data),
            "active_frames": active,
            "scored_frames": count,
            "simulated_seconds": active * dt,
            "elapsed_seconds": time.monotonic() - self.started
            if self.started is not None
            else 0.0,
            "processing_seconds": self.processing_seconds,
            "mean_step_ms": self.processing_seconds * 1000 / active if active else 0.0,
            "max_step_ms": self.max_step_seconds * 1000,
            **{key: float(value) for key, value in scores.items()},
            "tracking_rmse": math.sqrt(scores["lataccel_cost"] / 100) if scores else "",
            "max_tracking_error": max(
                (abs(float(a - t)) for a, t in zip(actual, target)), default=""
            ),
            "jerk_rms": math.sqrt(scores["jerk_cost"] / 100) if scores else "",
            "max_abs_steering": max(
                (abs(float(value)) for value in sim.action_history[control_start:]),
                default="",
            ),
            "api_requests": self.api_requests,
            "cache_hits": self.cache_hits,
            "api_cost_usd": self.api_cost if self.cost_known else "",
        }
        # Exact replay identity excludes timestamps, execution speed, and API
        # usage: a cached replay of the same ride is still the same result.
        identity = {
            "version": 1,
            "source": row["source"],
            "segment": row["segment"],
            "controller_history": self.history,
            "dt": dt,
            "control_start": control_start,
            "cost_end": cost_end,
            "scores": {key: float(value) for key, value in scores.items()},
            "vehicle_states": [
                [float(value) for value in state] for state in sim.state_history
            ],
            "targets": [float(value) for value in sim.target_lataccel_history],
            "actual": [float(value) for value in sim.current_lataccel_history],
            "steering": [float(value) for value in sim.action_history],
        }
        row["run_fingerprint"] = hashlib.sha256(
            json.dumps(
                identity, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        append_result(row)
        # Preserve the historical score CSV schema and every existing row. Full
        # ride geometry is joined by fingerprint, including for duplicate replays.
        metrics_row = {
            key: row[key]
            for key in (
                "run_fingerprint",
                "completed_at_utc",
                "source",
                "segment",
                "controller",
            )
        }
        metrics_row.update(sim.path_tracker.metrics())
        append_result(
            metrics_row,
            path=Path(os.environ.get("RUN_RESULTS_PATH", DEFAULT_PATH)).with_name(
                "path_metrics.csv"
            ),
            fields=PATH_FIELDS,
        )
        self.saved = True
