# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Prepare local data and merge Git-shared Jev caches without losing local rows."""

import argparse
import csv
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.request
import uuid
import zipfile
from contextlib import closing
from pathlib import Path

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
CACHE = Path("cache/jev.sqlite3")
DATA_URL = (
    "https://huggingface.co/datasets/commaai/commaSteeringControl/"
    "resolve/main/data/SYNTHETIC_V0.zip"
)
SCHEMA = "CREATE TABLE IF NOT EXISTS responses (request_hash TEXT PRIMARY KEY, response_json TEXT NOT NULL)"


def git(*args):
    LOGGER.info("Git: %s", " ".join(args))
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
        },
    ).stdout


def snapshot(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        closing(sqlite3.connect(source)) as src,
        closing(sqlite3.connect(destination)) as dst,
    ):
        src.backup(dst)
    LOGGER.info("Saved cache backup: %s", destination)


def merge_cache(local, remote, *, overwrite=True):
    """Merge validated records atomically, optionally retaining existing values."""
    with closing(sqlite3.connect(remote.as_uri() + "?mode=ro", uri=True)) as source:
        if source.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("Remote cache failed SQLite integrity check")
        rows = source.execute(
            "SELECT request_hash, response_json FROM responses"
        ).fetchall()
    for key, value in rows:
        if (
            not isinstance(key, str)
            or len(key) != 64
            or any(c not in "0123456789abcdef" for c in key)
        ):
            raise ValueError("Remote cache has an invalid request hash")
        if not isinstance(json.loads(value), dict):
            raise ValueError("Remote cache response must be a JSON object")
    local.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(local, timeout=30)) as target, target:
        target.execute(SCHEMA)
        target.execute("BEGIN IMMEDIATE")
        before = target.total_changes
        conflict = (
            "DO UPDATE "
            "SET response_json=excluded.response_json "
            "WHERE responses.response_json != excluded.response_json"
            if overwrite
            else "DO NOTHING"
        )
        target.executemany(
            "INSERT INTO responses VALUES (?, ?) ON CONFLICT(request_hash) " + conflict,
            rows,
        )
        LOGGER.info(
            "Cache merge: %d source records, %d inserted/updated",
            len(rows),
            target.total_changes - before,
        )


def fetch_cache(destination):
    upstream = (
        git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        .decode()
        .strip()
    )
    remote = (
        git(
            "config",
            "--get",
            f"branch.{git('branch', '--show-current').decode().strip()}.remote",
        )
        .decode()
        .strip()
    )
    git("fetch", remote)
    if not git("ls-tree", upstream, "--", CACHE.as_posix()).strip():
        LOGGER.warning(
            "Upstream %s has no shared cache yet; publish it with a normal commit/push",
            upstream,
        )
        return False
    destination.write_bytes(git("show", f"{upstream}:{CACHE.as_posix()}"))
    return True


def sync_cache():
    with tempfile.TemporaryDirectory(prefix="jev-sync-") as directory:
        remote = Path(directory) / "remote.sqlite3"
        if fetch_cache(remote):
            merge_cache(ROOT / CACHE, remote)


def pull_changes():
    """Stash only the database, pull, and reconcile with a durable local backup."""
    cache = ROOT / CACHE
    if git("diff", "--cached", "--name-only", "--", CACHE.as_posix()).strip():
        raise ValueError(
            "Cache has staged changes; commit or unstage it before dev-prepare"
        )
    backup = ROOT / "cache/backups" / f"{uuid.uuid4().hex}.sqlite3"
    if cache.exists():
        snapshot(cache, backup)
    stash = None
    if git("diff", "--name-only", "--", CACHE.as_posix()).strip():
        git("stash", "push", "-m", "dev-prepare cache backup", "--", CACHE.as_posix())
        stash = git("rev-parse", "stash@{0}").strip()
    try:
        git("pull", "--ff-only")
        if backup.exists():
            # Add local-only records to the pulled cache. Avoid copying database
            # pages: even an identical SQLite backup can dirty the tracked file.
            merge_cache(cache, backup, overwrite=False)
    except BaseException:
        if backup.exists():
            snapshot(backup, cache)
        LOGGER.exception(
            "Preparation failed; local cache restored, backup retained at %s", backup
        )
        raise
    else:
        if stash and git("rev-parse", "stash@{0}").strip() == stash:
            git("stash", "drop", "stash@{0}")


def split_data(root):
    data, other = root / "data", root / "data-non-600"
    data.mkdir(exist_ok=True)
    other.mkdir(exist_ok=True)
    counts = {600: 0, "other": 0}
    for source in sorted([*data.glob("*.csv"), *other.glob("*.csv")]):
        with source.open(newline="") as stream:
            reader = csv.reader(stream)
            if not next(reader, None):
                raise ValueError(f"Missing CSV header: {source}")
            frames = sum(1 for row in reader if row)
        destination = (data if frames == 600 else other) / source.name
        counts[600 if frames == 600 else "other"] += 1
        if destination != source:
            if destination.exists():
                raise FileExistsError(
                    f"Refusing to overwrite existing CSV: {destination}"
                )
            source.rename(destination)
            LOGGER.debug(
                "Moved %s (%d frames) to %s", source.name, frames, destination.parent
            )
    LOGGER.info(
        "Dataset ready: %d 600-frame CSVs, %d other CSVs", counts[600], counts["other"]
    )


def prepare_data(root):
    data = root / "data"
    if not any(data.glob("*.csv")) and not any((root / "data-non-600").glob("*.csv")):
        # Stage the entire download/extraction before publishing data/. A failed
        # transfer cannot leave a partial dataset that looks ready on the next run.
        with tempfile.TemporaryDirectory(prefix="dataset-", dir=root) as directory:
            stage = Path(directory)
            archive = stage / "dataset.zip"
            LOGGER.info("Downloading official dataset")
            with (
                urllib.request.urlopen(DATA_URL, timeout=120) as response,
                archive.open("wb") as output,
            ):
                shutil.copyfileobj(response, output)
            extracted = stage / "data"
            extracted.mkdir()
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    if member.is_dir() or not member.filename.endswith(".csv"):
                        continue
                    destination = extracted / Path(member.filename).name
                    with (
                        bundle.open(member) as source,
                        destination.open("xb") as output,
                    ):
                        shutil.copyfileobj(source, output)
            if not any(extracted.glob("*.csv")):
                raise ValueError("Dataset archive contains no CSVs")
            if data.exists():
                data.rmdir()  # Refuse to replace a nonempty directory.
            extracted.rename(data)
    else:
        LOGGER.info("Using existing dataset")
    split_data(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "sync-cache"])
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        if args.action == "sync-cache":
            sync_cache()
        else:
            pull_changes()
            prepare_data(ROOT)
    except subprocess.CalledProcessError as error:
        LOGGER.error(
            "Git failed (exit %d):\n%s\n%s",
            error.returncode,
            error.stdout.decode(),
            error.stderr.decode(),
        )
        raise SystemExit(1) from None
    except Exception:
        LOGGER.exception("%s failed", args.action)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
