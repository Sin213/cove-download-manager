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
