"""Read completed-run ledgers without modifying them or running simulations."""

import csv
import fcntl
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from run_results import DEFAULT_PATH, FIELDS, PATH_FIELDS, ResultsError

LOGGER = logging.getLogger(__name__)
TEXT_FIELDS = {
    "run_id",
    "run_fingerprint",
    "completed_at_utc",
    "source",
    "segment",
    "controller",
}


def read_rows(path, fields):
    """Use the writer's lock so a concurrent append cannot expose a partial row."""
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            reader = csv.DictReader(stream)
            if reader.fieldnames is None:
                return []
            if reader.fieldnames != list(fields):
                raise ResultsError("Saved results have an incompatible CSV header.")
            return list(reader)
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError, csv.Error) as error:
        LOGGER.error(
            "Analysis ledger read failed file=%s type=%s",
            path.name,
            type(error).__name__,
        )
        raise ResultsError(
            "Saved results could not be read. Check the server log."
        ) from None


def parse_row(row, fields):
    if None in row or any(value is None for value in row.values()):
        raise ValueError("Incomplete CSV row")
    parsed = {}
    for key in fields:
        value = row[key]
        if key in TEXT_FIELDS:
            if not value:
                raise ValueError("Missing identifier")
            parsed[key] = value
        elif key == "controller_history":
            history = json.loads(value)
            if not isinstance(history, list):
                raise ValueError("Invalid controller history")
            for entry in history:
                if not isinstance(entry, dict) or not isinstance(
                    entry.get("settings"), dict
                ):
                    raise ValueError("Invalid controller history entry")
                if not isinstance(entry["settings"].get("controller"), str):
                    raise ValueError("Invalid controller name")
                if any(
                    type(entry.get(k)) is not int for k in ("start_frame", "end_frame")
                ):
                    raise ValueError("Invalid controller frame range")
            # Reject NaN/Infinity accepted by Python's permissive JSON parser.
            json.dumps(history, allow_nan=False)
            parsed[key] = history
        else:
            number = float(value) if value else None
            if number is not None and not math.isfinite(number):
                raise ValueError("Non-finite metric")
            parsed[key] = number
    stamp = datetime.fromisoformat(parsed["completed_at_utc"])
    if stamp.tzinfo is None:
        raise ValueError("Timestamp needs timezone")
    parsed["completed_at_utc"] = stamp.astimezone(timezone.utc).isoformat()
    return parsed


def load_analysis():
    """Join optional path metrics by fingerprint; preserve missing data as null."""
    path = Path(os.environ.get("RUN_RESULTS_PATH", DEFAULT_PATH))
    warnings = []
    runs, seen = [], set()
    skipped = 0
    for line, row in enumerate(read_rows(path, FIELDS), start=2):
        try:
            parsed = parse_row(row, FIELDS)
            fingerprint = parsed["run_fingerprint"]
            if fingerprint in seen:
                raise ValueError("Duplicate fingerprint")
            seen.add(fingerprint)
            runs.append(parsed)
        except (ValueError, TypeError, KeyError) as error:
            skipped += 1
            LOGGER.warning(
                "Analysis excluded run row=%d type=%s", line, type(error).__name__
            )
    if skipped:
        warnings.append(
            f"Excluded {skipped} invalid or duplicate run rows. See the server log."
        )
    try:
        path_rows = read_rows(path.with_name("path_metrics.csv"), PATH_FIELDS)
    except ResultsError:
        path_rows = []
        warnings.append(
            "Path metrics could not be loaded; score history is still available."
        )
    metrics = {}
    invalid_metrics = 0
    for line, row in enumerate(path_rows, start=2):
        try:
            parsed = parse_row(row, PATH_FIELDS)
            key = parsed["run_fingerprint"]
            if key in metrics:
                raise ValueError("Duplicate path fingerprint")
            metrics[key] = {
                field: parsed[field]
                for field in PATH_FIELDS
                if field not in TEXT_FIELDS
            }
        except (ValueError, TypeError, KeyError) as error:
            invalid_metrics += 1
            LOGGER.warning(
                "Analysis excluded path row=%d type=%s", line, type(error).__name__
            )
    if invalid_metrics:
        warnings.append(
            f"Excluded {invalid_metrics} invalid or duplicate path-metric rows."
        )
    for row in runs:
        row.update({field: None for field in PATH_FIELDS if field not in TEXT_FIELDS})
        row.update(metrics.get(row["run_fingerprint"], {}))
    runs.sort(key=lambda row: (row["completed_at_utc"], row["run_id"]))
    LOGGER.info(
        "Analysis loaded runs=%d with_path=%d excluded=%d",
        len(runs),
        sum(r["path_frames"] is not None for r in runs),
        skipped,
    )
    return {
        "runs": runs,
        "warnings": warnings,
        "loaded_at_utc": datetime.now(timezone.utc).isoformat(),
    }
