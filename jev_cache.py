"""Persistent exact-request cache shared by Jev controllers and processes."""

import hashlib
import json
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from controllers import ControllerError

LOGGER = logging.getLogger(__name__)
DEFAULT_PATH = Path(__file__).resolve().parent / "cache" / "jev.sqlite3"


def get_or_request(payload, request, validate):
    """Return (validated response, cache hit); never request on cache failure.

    Hold the write transaction through a cache miss so two processes cannot pay
    for the same request concurrently. Exceptions roll back without caching.
    Only response JSON and its request hash are stored, never auth headers.
    """
    key = hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    path = Path(os.environ.get("JEV_CACHE_PATH", DEFAULT_PATH))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=30)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS responses "
                "(request_hash TEXT PRIMARY KEY, response_json TEXT NOT NULL)"
            )
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT response_json FROM responses WHERE request_hash = ?", (key,)
            ).fetchone()
            if row is not None:
                result = validate(json.loads(row[0]))
                LOGGER.info("Jev cache hit key=%s", key[:12])
                return result, True
            LOGGER.info("Jev cache miss key=%s", key[:12])
            response = request()
            result = validate(response)
            # Store just the validated decision fields, excluding arbitrary
            # provider metadata and retaining the original billed cost.
            choice, confidence, probabilities, cost = result
            saved = {
                "answers": {
                    "steering": {
                        "type": "choice",
                        "choice": choice,
                        "confidence": confidence,
                        "probabilities": probabilities,
                    }
                },
                "usage": {"cost": cost},
            }
            connection.execute(
                "INSERT INTO responses VALUES (?, ?)",
                (key, json.dumps(saved, allow_nan=False)),
            )
            LOGGER.info("Jev cache stored key=%s", key[:12])
            return result, False
    except (OSError, sqlite3.Error, json.JSONDecodeError) as error:
        LOGGER.error("Jev cache unavailable error_type=%s", type(error).__name__)
        raise ControllerError(
            "Jev cache could not be read or saved. Check cache/jev.sqlite3 "
            "(or JEV_CACHE_PATH) before retrying; automatic API fallback is disabled."
        ) from None
