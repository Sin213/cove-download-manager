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

from cove import config
from cove.search.indexers import CustomTorznabIndexer


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


# --- persisted value types --------------------------------------------------
#
# The loader accepted any JSON value for a recognised key. Consumers then did
# arithmetic on it, compared it, or tested it for truth - so a hand-edited,
# partially migrated or corrupted settings file could crash a feature or, worse,
# silently invert one (a non-empty string like "false" is truthy).


def _write(tmp_path, monkeypatch, payload):
    import json

    path = tmp_path / "settings.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return path


@pytest.mark.parametrize("value", ["false", "16", None, [], {}, 3.5])
def test_a_non_boolean_setting_falls_back_to_its_default(tmp_path, monkeypatch, value):
    _write(tmp_path, monkeypatch, {"speed_limiter_enabled": value})

    loaded = config.Settings.load()

    assert loaded.speed_limiter_enabled is False


@pytest.mark.parametrize("value", ["8", None, [], {}, True, 2.5])
def test_a_non_integer_setting_falls_back_to_its_default(tmp_path, monkeypatch, value):
    _write(tmp_path, monkeypatch, {"max_concurrent": value})

    loaded = config.Settings.load()

    assert loaded.max_concurrent == config.Settings().max_concurrent


@pytest.mark.parametrize("value", [17, None, {}, True])
def test_a_non_string_setting_falls_back_to_its_default(tmp_path, monkeypatch, value):
    _write(tmp_path, monkeypatch, {"proxy_host": value})

    loaded = config.Settings.load()

    assert loaded.proxy_host == ""


def test_well_typed_settings_are_preserved(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {
        "max_concurrent": 5,
        "speed_limiter_enabled": True,
        "proxy_host": "127.0.0.1",
        "theme": "light",
    })

    loaded = config.Settings.load()

    assert loaded.max_concurrent == 5
    assert loaded.speed_limiter_enabled is True
    assert loaded.proxy_host == "127.0.0.1"
    assert loaded.theme == "light"


@pytest.mark.parametrize("value", [17, None, [], {}, True])
def test_a_nested_category_dir_of_the_wrong_type_falls_back(tmp_path, monkeypatch, value):
    """Nested settings are as reachable by a hand-edited file as top-level ones."""
    _write(tmp_path, monkeypatch, {"category_dirs": {"Videos": value}})

    loaded = config.Settings.load()

    assert loaded.category_dirs.Videos == config.CategoryDirs().Videos


@pytest.mark.parametrize("value", ["2", None, [], 2.5])
def test_a_nested_schedule_field_of_the_wrong_type_falls_back(
    tmp_path, monkeypatch, value
):
    _write(tmp_path, monkeypatch, {"schedule": {"enabled": True, "start_hour": value}})

    loaded = config.Settings.load()

    assert loaded.schedule.start_hour == config.ScheduleWindow().start_hour


def test_well_typed_nested_settings_are_preserved(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {
        "category_dirs": {"Videos": "/srv/video"},
        "schedule": {"enabled": True, "start_hour": 3, "end_hour": 5},
    })

    loaded = config.Settings.load()

    assert loaded.category_dirs.Videos == "/srv/video"
    assert loaded.schedule.start_hour == 3
    assert loaded.schedule.enabled is True


def test_saved_schedule_days_survive_loading(tmp_path, monkeypatch):
    """A day selection is a user setting, not a derived value.

    The type validator recognised only scalars, so `days: List[int]` was
    dropped on load and every schedule silently reverted to all seven days.
    """
    _write(tmp_path, monkeypatch, {
        "schedule": {"enabled": True, "start_hour": 1, "end_hour": 5,
                     "days": [1, 3, 5]},
    })

    loaded = config.Settings.load()

    assert loaded.schedule.days == [1, 3, 5]
    assert loaded.schedule.enabled is True


@pytest.mark.parametrize("days", [["1"], [9], [None], [True]])
def test_a_schedule_with_invalid_days_resets_to_defaults(tmp_path, monkeypatch, days):
    """Element checking stays with _schedule_valid, which resets the window."""
    _write(tmp_path, monkeypatch, {
        "schedule": {"enabled": True, "start_hour": 1, "days": days},
    })

    loaded = config.Settings.load()

    assert loaded.schedule.days == config.ScheduleWindow().days
    assert loaded.schedule.enabled is config.ScheduleWindow().enabled


def test_a_non_list_days_value_falls_back(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"schedule": {"days": "everyday"}})

    loaded = config.Settings.load()

    assert loaded.schedule.days == config.ScheduleWindow().days


def test_every_persisted_field_is_one_the_validator_can_check():
    """Guards the class of bug, not just this instance.

    A field the validator does not understand is dropped on load, silently
    discarding whatever the user had saved. Adding one must fail here rather
    than be discovered by a user whose settings reverted.
    """
    for klass in (config.Settings, config.ScheduleWindow, config.CategoryDirs):
        for name, annotation in klass.__annotations__.items():
            # Nested collections are removed from the payload before validation
            # runs and are constructed separately (custom_indexers is parsed
            # by cove.search.indexers.parse_custom_indexers).
            if klass is config.Settings and name in (
                "category_dirs",
                "schedule",
                "custom_indexers",
            ):
                continue
            assert config.understands(annotation), f"{klass.__name__}.{name}"


# --- custom Torznab indexer persistence -------------------------------------
#
# S2: user-configured generic Torznab indexers persist as an ordered list of
# records with a stable custom id, enabled state, display name, full endpoint
# url and optional api_key. All of these run through the real Settings
# save/load path against an isolated temp config, never the user's live file.

ID_ONE = "custom:00000000-0000-0000-0000-000000000001"
ID_TWO = "custom:00000000-0000-0000-0000-000000000002"
ENDPOINT = "http://127.0.0.1:9696/some/per-indexer/torznab/api"


def test_custom_indexers_default_empty_for_old_config(tmp_path, monkeypatch):
    # Backward compatibility: an existing config with no custom-indexer field
    # loads with an empty collection and no migration path is required.
    _write(tmp_path, monkeypatch, {"download_dir": "/srv/dl", "theme": "light"})

    loaded = config.Settings.load()

    assert loaded.custom_indexers == []
    assert loaded.download_dir == "/srv/dl"
    assert loaded.theme == "light"
    # A normal save/reload of an old config remains a no-op for indexers.
    loaded.save()
    assert config.Settings.load().custom_indexers == []


def test_custom_indexer_roundtrips_through_save_and_reload(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {})

    loaded = config.Settings.load()
    loaded.custom_indexers = [
        CustomTorznabIndexer(
            id=ID_ONE,
            enabled=True,
            name="My Torznab",
            url=ENDPOINT,
            api_key="super-secret-test-key",
        )
    ]
    loaded.save()

    reloaded = config.Settings.load()
    assert len(reloaded.custom_indexers) == 1
    (record,) = reloaded.custom_indexers
    assert record.id == ID_ONE
    assert record.enabled is True
    assert record.name == "My Torznab"
    assert record.url == ENDPOINT
    assert record.api_key == "super-secret-test-key"


def test_custom_indexer_endpoint_path_is_not_normalized(tmp_path, monkeypatch):
    # A multi-component per-indexer path must round-trip byte-for-byte; S2 must
    # not collapse it to a generic /api.
    _write(tmp_path, monkeypatch, {})

    loaded = config.Settings.load()
    loaded.custom_indexers = [
        CustomTorznabIndexer(id=ID_ONE, name="n", url=ENDPOINT)
    ]
    loaded.save()

    (record,) = config.Settings.load().custom_indexers
    assert record.url == ENDPOINT


def test_custom_indexer_order_is_preserved(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {})

    ids = [
        "custom:00000000-0000-0000-0000-000000000003",
        "custom:00000000-0000-0000-0000-000000000001",
        "custom:00000000-0000-0000-0000-000000000002",
    ]
    loaded = config.Settings.load()
    loaded.custom_indexers = [
        CustomTorznabIndexer(id=i, name=f"n{i[-1]}", url="http://x/api")
        for i in ids
    ]
    loaded.save()

    assert [r.id for r in config.Settings.load().custom_indexers] == ids


def test_custom_indexer_id_survives_field_edits(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {})

    loaded = config.Settings.load()
    loaded.custom_indexers = [
        CustomTorznabIndexer(
            id=ID_ONE, name="old", url="http://old/api", api_key="old-key"
        )
    ]
    loaded.save()

    reloaded = config.Settings.load()
    (record,) = reloaded.custom_indexers
    record.name = "new name"
    record.url = "http://new/per-indexer/api"
    record.api_key = "new-key"
    record.enabled = False
    reloaded.save()

    (final,) = config.Settings.load().custom_indexers
    assert final.id == ID_ONE
    assert final.name == "new name"
    assert final.url == "http://new/per-indexer/api"
    assert final.api_key == "new-key"
    assert final.enabled is False


def test_duplicate_persisted_ids_are_dropped(tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        {
            "custom_indexers": [
                {"id": ID_ONE, "name": "first", "url": "http://a/api"},
                {"id": ID_ONE, "name": "dup", "url": "http://b/api"},
                {"id": ID_TWO, "name": "second", "url": "http://c/api"},
            ]
        },
    )

    loaded = config.Settings.load()

    assert [r.name for r in loaded.custom_indexers] == ["first", "second"]


def test_existing_settings_preserved_alongside_custom_indexer(tmp_path, monkeypatch):
    # Representative existing fields: a network setting (proxy_host), an
    # ordinary setting (max_concurrent), and a secret-bearing setting
    # (rpc_secret). None may drift when a custom indexer is added.
    secret = "x" * 24
    _write(
        tmp_path,
        monkeypatch,
        {
            "download_dir": "/srv/dl",
            "proxy_host": "127.0.0.1",
            "max_concurrent": 3,
            "rpc_secret": secret,
        },
    )

    loaded = config.Settings.load()
    loaded.custom_indexers = [
        CustomTorznabIndexer(id=ID_ONE, name="n", url=ENDPOINT, api_key="sek")
    ]
    loaded.save()

    reloaded = config.Settings.load()
    assert reloaded.download_dir == "/srv/dl"
    assert reloaded.proxy_host == "127.0.0.1"
    assert reloaded.max_concurrent == 3
    assert reloaded.rpc_secret == secret
    assert [r.id for r in reloaded.custom_indexers] == [ID_ONE]


def test_removed_custom_indexer_secret_absent_from_saved_file(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {})

    loaded = config.Settings.load()
    loaded.custom_indexers = [
        CustomTorznabIndexer(
            id=ID_ONE, name="n", url="http://x/api", api_key="super-secret-test-key"
        ),
        CustomTorznabIndexer(
            id=ID_TWO, name="n2", url="http://y/api", api_key="other-key"
        ),
    ]
    loaded.save()

    loaded.custom_indexers = loaded.custom_indexers[1:]
    loaded.save()

    raw = (tmp_path / "settings.json").read_text()
    assert "super-secret-test-key" not in raw
    assert "other-key" in raw


@pytest.mark.skipif(os.name == "nt", reason="0600 is a POSIX-only promise")
def test_settings_file_saved_with_custom_indexers_is_0600(tmp_path, monkeypatch):
    import stat

    _write(tmp_path, monkeypatch, {})

    loaded = config.Settings.load()
    loaded.custom_indexers = [
        CustomTorznabIndexer(id=ID_ONE, name="n", url="http://x/api", api_key="sek")
    ]
    loaded.save()

    mode = stat.S_IMODE(os.stat(tmp_path / "settings.json").st_mode)
    assert mode == 0o600
