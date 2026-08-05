"""Settings persistence for the close-to-tray option.

Settings.load() reads module-level CONFIG_FILE/DATA_DIR paths that are
resolved at import time, so each round-trip runs in a throwaway subprocess
against a temp config directory rather than mutating the real one.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _load_close_to_tray(tmp_path, raw: dict):
    script = r'''
import json, sys
from pathlib import Path
import cove.config as config

tmp = Path(sys.argv[1])
config.CONFIG_DIR = tmp
config.DATA_DIR = tmp
config.CONFIG_FILE = tmp / "settings.json"
config.CONFIG_FILE.write_text(sys.argv[2])

from cove.config import Settings
s = Settings.load()
print(json.dumps({"close_to_tray": s.close_to_tray}))
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), json.dumps(raw)],
        cwd=Path(__file__).resolve().parents[1],
        env=dict(os.environ),
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)["close_to_tray"]


def test_close_to_tray_defaults_to_false():
    from cove.config import Settings

    assert Settings().close_to_tray is False


def test_close_to_tray_round_trips_true(tmp_path):
    assert _load_close_to_tray(tmp_path, {"close_to_tray": True}) is True


def test_close_to_tray_round_trips_false(tmp_path):
    assert _load_close_to_tray(tmp_path, {"close_to_tray": False}) is False


def test_hand_edited_non_boolean_close_to_tray_sanitizes_to_false(tmp_path):
    # A hand-edited string/number/null must never be treated as "enabled":
    # a truthy non-boolean would silently hide the window on close.
    for bogus in ("yes", 1, 0, None, [], {"on": True}):
        assert _load_close_to_tray(tmp_path, {"close_to_tray": bogus}) is False


def test_magnet_fields_default_off_and_flag_absence(tmp_path, monkeypatch):
    from cove import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text("{}")

    s = config.Settings.load()
    assert s.magnet_handler_enabled is False
    assert s.magnet_prompt_shown is False
    # Absent, so the one-time migration is allowed to consider it.
    assert s.magnet_setting_missing is True


def test_explicit_false_is_never_treated_as_absent(tmp_path, monkeypatch):
    from cove import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text('{"magnet_handler_enabled": false}')

    s = config.Settings.load()
    assert s.magnet_handler_enabled is False
    assert s.magnet_setting_missing is False


def test_hand_edited_non_boolean_is_not_read_as_enabled(tmp_path, monkeypatch):
    from cove import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text('{"magnet_handler_enabled": "yes"}')

    s = config.Settings.load()
    assert s.magnet_handler_enabled is False


def test_hand_edited_non_boolean_prompt_shown_sanitizes_to_false(tmp_path, monkeypatch):
    """A malformed magnet_prompt_shown must not count as a prior decision.

    The design distinguishes absent, explicit False, and explicit True. A
    truthy non-boolean such as "false" or 1 would otherwise suppress the
    one-time offer forever, and the user would never be asked at all.
    """
    import json

    from cove import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "settings.json")

    for bogus in ("false", "true", 1, 0, None, [], {"on": True}):
        (tmp_path / "settings.json").write_text(
            json.dumps({"magnet_prompt_shown": bogus})
        )
        s = config.Settings.load()
        assert s.magnet_prompt_shown is False, bogus


def test_explicit_true_prompt_shown_is_preserved(tmp_path, monkeypatch):
    from cove import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text('{"magnet_prompt_shown": true}')

    assert config.Settings.load().magnet_prompt_shown is True


def test_magnet_setting_missing_is_not_persisted(tmp_path, monkeypatch):
    import json

    from cove import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text("{}")

    s = config.Settings.load()
    s.save()
    raw = json.loads((tmp_path / "settings.json").read_text())
    assert "magnet_setting_missing" not in raw


def test_concurrent_saves_never_produce_a_broken_settings_file(tmp_path, monkeypatch):
    """Two threads calling save() at once must never publish a truncated or
    half-written settings.json.

    Regression for the magnet self-heal daemon thread: it is the first code
    in this app to call Settings.save() off the GUI thread, so a save from
    that thread can now race a save from the GUI thread. A shared fixed tmp
    path let one thread's os.replace publish the other thread's
    partially-written file; save() now uses a unique temp file per call plus
    a lock.
    """
    import threading

    from cove import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "settings.json")

    base = config.Settings.load()
    errors: list[Exception] = []
    stop = threading.Event()
    iterations = 200

    # The bug's signature is a truncated file visible WHILE the race is on,
    # not after it. Checking only once every thread has joined would pass
    # against the broken implementation unless the very last os.replace
    # happened to publish a partial file, so a reader samples throughout.
    reads = []
    # "assert not reader.is_alive()" is satisfied by a thread that died from an
    # exception just as much as by one that returned, and "assert reads" needs
    # only a single sample before a crash. Without an explicit completion
    # sentinel a dead observer is indistinguishable from a healthy one, and the
    # run degrades to a warning that the default pytest invocation ignores.
    watcher_completed = []

    def _watch_loop():
        while not stop.is_set():
            try:
                text = (tmp_path / "settings.json").read_text()
            except FileNotFoundError:
                # os.replace is atomic, so the path is never absent. If it
                # is, that is itself the defect.
                errors.append(AssertionError("settings.json vanished mid-save"))
                continue
            except PermissionError as e:
                # Windows only: opening a delete-pending target during the
                # replace loses a sharing race. That is an expected
                # interleaving, not a torn file - keep sampling instead of
                # letting the thread die, which would end the observation
                # window almost immediately and hollow out this test.
                if config._is_transient_sharing_error(e):
                    continue
                raise
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as e:
                errors.append(
                    AssertionError("observed a partial settings.json: %s" % e)
                )
                continue
            if not isinstance(parsed, dict) or "rpc_secret" not in parsed:
                errors.append(
                    AssertionError("observed an incomplete settings object")
                )
                continue
            reads.append(1)

    def watcher():
        # Surface a crash as a test failure instead of an unhandled-thread
        # warning, and record that the loop actually ran to completion.
        try:
            _watch_loop()
        except BaseException as e:  # pragma: no cover - failure path only
            errors.append(e)
        else:
            watcher_completed.append(True)

    def hammer(rpc_secret_value: str):
        try:
            fields = {
                k: v for k, v in base.__dict__.items()
                if k in config.Settings.__dataclass_fields__
            }
            fields["rpc_secret"] = rpc_secret_value
            s = config.Settings(**fields)
            for _ in range(iterations):
                s.save()
        except Exception as e:  # pragma: no cover - failure path only
            errors.append(e)

    threads = [
        threading.Thread(target=hammer, args=(f"secret-{i}",)) for i in range(6)
    ]
    reader = threading.Thread(target=watcher, daemon=True)
    reader.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()
    stop.set()
    reader.join(timeout=10)
    assert not reader.is_alive()

    assert not errors
    # A watcher that never got a sample in would prove nothing.
    assert reads, "the reader observed no writes, so the race was never exercised"
    assert watcher_completed, "the reader thread died before the run finished"

    # The file must exist, parse as JSON, and be a complete settings object
    # after every interleaving - never truncated or a mix of two writes.
    raw_text = (tmp_path / "settings.json").read_text()
    parsed = json.loads(raw_text)
    assert isinstance(parsed, dict)
    assert "rpc_secret" in parsed
    assert parsed["rpc_secret"].startswith("secret-")

    # No leftover unique temp files: every save cleaned up after itself.
    leftovers = [
        p for p in tmp_path.iterdir()
        if p.name != "settings.json" and p.name.startswith("settings.json")
    ]
    assert leftovers == []


def test_unreadable_settings_never_regenerates_secrets(tmp_path, monkeypatch):
    """A file that exists but cannot be read is not corruption.

    Windows shares by handle, so a backup or antivirus agent can hold
    settings.json open for longer than the sharing retry window. Treating that
    as corruption would regenerate rpc_secret and api_token and overwrite every
    stored setting with defaults, silently destroying the user's configuration
    and rotating their secrets. load() must fail closed and leave the file
    exactly as it found it.
    """
    from cove import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "settings.json")

    seeded = config.Settings.load()
    seeded.download_dir = str(tmp_path / "chosen")
    seeded.save()
    before = (tmp_path / "settings.json").read_bytes()

    # Deny only this file, and only while the flag is set. monkeypatch.undo()
    # would also revert CONFIG_FILE/CONFIG_DIR and send the reload below at the
    # real user config.
    blocked = {"on": True}
    real_read_text = Path.read_text

    def unreadable(self, *args, **kwargs):
        if blocked["on"] and self == tmp_path / "settings.json":
            raise PermissionError(13, "Access is denied")
        return real_read_text(self, *args, **kwargs)

    # Outlast the retry window without actually sleeping through it.
    monkeypatch.setattr(config, "_SHARING_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(Path, "read_text", unreadable)

    with pytest.raises(OSError):
        config.Settings.load()

    blocked["on"] = False
    after = (tmp_path / "settings.json").read_bytes()
    assert after == before, "load() rewrote a file it could not read"
    reloaded = config.Settings.load()
    assert reloaded.rpc_secret == seeded.rpc_secret
    assert reloaded.api_token == seeded.api_token
    assert reloaded.download_dir == seeded.download_dir
