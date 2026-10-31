"""Offline tests for dataset preparation and Git-backed cache merging."""

import json
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import dev_prepare as dev


def database(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(dev.SCHEMA)
        connection.executemany("INSERT INTO responses VALUES (?, ?)", rows)


def records(path):
    with sqlite3.connect(path) as connection:
        return dict(connection.execute("SELECT * FROM responses"))


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_remote_wins_and_local_only_survives_idempotently(self):
        local, remote = self.root / "local.db", self.root / "remote.db"
        database(local, [("a" * 64, '{"v":1}'), ("b" * 64, "{}")])
        database(remote, [("a" * 64, '{"v":2}'), ("c" * 64, "{}")])
        dev.merge_cache(local, remote)
        dev.merge_cache(local, remote)
        self.assertEqual(
            records(local), {"a" * 64: '{"v":2}', "b" * 64: "{}", "c" * 64: "{}"}
        )

    def test_invalid_remote_leaves_local_unchanged(self):
        local, remote = self.root / "local.db", self.root / "remote.db"
        database(local, [("a" * 64, "{}")])
        database(remote, [("b" * 64, "{}"), ("c" * 64, "invalid")])
        with self.assertRaises(json.JSONDecodeError):
            dev.merge_cache(local, remote)
        self.assertEqual(records(local), {"a" * 64: "{}"})

    def test_restore_keeps_pulled_values_and_unchanged_bytes(self):
        local, backup = self.root / "local.db", self.root / "backup.db"
        database(local, [("a" * 64, '{"v":2}')])
        database(backup, [("a" * 64, '{"v":1}')])
        before = local.read_bytes()
        dev.merge_cache(local, backup, overwrite=False)
        self.assertEqual(local.read_bytes(), before)
        with sqlite3.connect(backup) as connection:
            connection.execute("INSERT INTO responses VALUES (?, ?)", ("b" * 64, "{}"))
        dev.merge_cache(local, backup, overwrite=False)
        self.assertEqual(records(local), {"a" * 64: '{"v":2}', "b" * 64: "{}"})

    def test_split_counts_records_excluding_header_and_is_repeatable(self):
        for folder, name, frames in [
            ("data", "short", 599),
            ("data", "long", 601),
            ("data-non-600", "keep", 600),
        ]:
            directory = self.root / folder
            directory.mkdir(exist_ok=True)
            (directory / f"{name}.csv").write_text(
                "t,note\n" + '1,"two\nlines"\n' * frames
            )
        dev.split_data(self.root)
        dev.split_data(self.root)
        self.assertEqual(
            [p.name for p in (self.root / "data").glob("*.csv")], ["keep.csv"]
        )
        self.assertEqual(len(list((self.root / "data-non-600").glob("*.csv"))), 2)

    def test_split_refuses_collision(self):
        for folder in ["data", "data-non-600"]:
            (self.root / folder).mkdir()
            (self.root / folder / "same.csv").write_text("t\n1\n")
        with self.assertRaises(FileExistsError):
            dev.split_data(self.root)
        self.assertTrue((self.root / "data/same.csv").exists())

    def test_download_extract_and_reuse(self):
        archive = self.root / "fixture.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("nested/keep.csv", "t\n" + "1\n" * 600)
            bundle.writestr("nested/short.csv", "t\n1\n")
        with patch.object(
            dev.urllib.request, "urlopen", return_value=archive.open("rb")
        ) as download:
            dev.prepare_data(self.root)
            dev.prepare_data(self.root)
        download.assert_called_once()
        self.assertTrue((self.root / "data/keep.csv").exists())
        self.assertTrue((self.root / "data-non-600/short.csv").exists())

    def test_pull_preserves_dirty_cache_and_sync_fetches_without_pulling(self):
        remote, local = self.root / "remote", self.root / "local"
        remote.mkdir()

        def git_at(where, *args):
            return subprocess.run(
                ["git", "-C", str(where), *args], check=True, capture_output=True
            ).stdout

        git_at(remote, "init", "--initial-branch=main")
        git_at(remote, "config", "user.email", "test@example.invalid")
        git_at(remote, "config", "user.name", "Test")
        database(remote / dev.CACHE, [("a" * 64, '{"v":1}')])
        git_at(remote, "add", ".")
        git_at(remote, "commit", "-m", "fixture")
        git_at(self.root, "clone", str(remote), str(local))
        with sqlite3.connect(local / dev.CACHE) as connection:
            connection.execute("INSERT INTO responses VALUES (?, ?)", ("b" * 64, "{}"))
        with sqlite3.connect(remote / dev.CACHE) as connection:
            connection.execute("UPDATE responses SET response_json=?", ('{"v":2}',))
        git_at(remote, "commit", "-am", "remote change")
        with patch.object(dev, "ROOT", local):
            dev.pull_changes()
            backup = next((local / "cache/backups").glob("*.sqlite3"))
            self.assertEqual(records(backup)["a" * 64], '{"v":1}')
            self.assertEqual(
                records(local / dev.CACHE), {"a" * 64: '{"v":2}', "b" * 64: "{}"}
            )
            self.assertEqual(git_at(local, "stash", "list"), b"")
            before = git_at(local, "rev-parse", "HEAD")
            with sqlite3.connect(remote / dev.CACHE) as connection:
                connection.execute("UPDATE responses SET response_json=?", ('{"v":3}',))
            git_at(remote, "commit", "-am", "new cache")
            dev.sync_cache()
            self.assertEqual(git_at(local, "rev-parse", "HEAD"), before)
            self.assertEqual(records(local / dev.CACHE)["a" * 64], '{"v":3}')
            # A failed pull restores local rows and leaves a recovery stash.
            original_git = dev.git

            def fail_pull(*args):
                if args[0] == "pull":
                    raise RuntimeError("simulated pull failure")
                return original_git(*args)

            with patch.object(dev, "git", side_effect=fail_pull):
                with self.assertRaisesRegex(RuntimeError, "simulated pull failure"):
                    dev.pull_changes()
            self.assertEqual(records(local / dev.CACHE)["a" * 64], '{"v":3}')
            self.assertIn("b" * 64, records(local / dev.CACHE))
            self.assertTrue(git_at(local, "stash", "list"))


if __name__ == "__main__":
    unittest.main()
