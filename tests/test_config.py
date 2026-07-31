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

    def watcher():
        while not stop.is_set():
            try:
                text = (tmp_path / "settings.json").read_text()
            except FileNotFoundError:
                # os.replace is atomic, so the path is never absent. If it
                # is, that is itself the defect.
                errors.append(AssertionError("settings.json vanished mid-save"))
                continue
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
