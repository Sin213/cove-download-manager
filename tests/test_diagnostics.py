"""Tests for the sanitized diagnostics subsystem (cove/diagnostics.py).

The whole point of this module is that nothing sensitive survives into a
retained record, so every test here works from the same fixture bank of
representative secrets and asserts their absence from whatever surface is
under test.
"""
import json
import sys

import pytest

from cove import diagnostics


WIN_USER = "jsmith"
LINUX_USER = "sinuser"
RD_TOKEN = "A7QK3ZP9WVN2XLMDR4TJ6YB8C5FGH1SE"
BEARER = "eyJhbGciOiJIUzI1NiJ9.QWxhZGRpbjpvcGVuIHNlc2FtZQ.dBjftJeZ4CVPmB92K27u"
PASSKEY = "d41d8cd98f00b204e9800998ecf8427e"

RD_SHARE_URL = "https://real-debrid.com/d/{}".format(RD_TOKEN)
RD_DELIVERY_URL = (
    "https://sg5.download.real-debrid.com/d/{}/video.mp4".format(RD_TOKEN)
)
MAGNET_URI = (
    "magnet:?xt=urn:btih:c12fe1c06bba254a9dc9f519b335aa7c1367a88a"
    "&tr=https://tracker.example.org/{}/announce".format(PASSKEY)
)
COOKIE_HEADER = "Cookie: session_id={}; csrf={}".format(BEARER, PASSKEY)
QUERY_URL = "https://cdn.example.com/file.mp4?token={}&Expires=99".format(RD_TOKEN)
WIN_WORK_PATH = (
    "C:\\Users\\{}\\Music\\cdm\\.cove-work-8f2a91cd\\video.mp4".format(WIN_USER)
)
LINUX_WORK_PATH = "/home/{}/Downloads/.cove-work-8f2a91cd/video.mp4".format(LINUX_USER)
NATIVE_BODY = json.dumps({"action": "download", "url": QUERY_URL, "cookies": COOKIE_HEADER})

# Every literal that must never appear in any retained or exported surface.
SECRET_FIXTURES = [
    RD_TOKEN,
    BEARER,
    PASSKEY,
    "real-debrid.com/d/{}".format(RD_TOKEN),
    "sg5.download",
    "c12fe1c06bba254a9dc9f519b335aa7c1367a88a",
    WIN_USER,
    LINUX_USER,
    "8f2a91cd",
    "session_id=",
    "Expires=99",
]


def assert_clean(text):
    """Fail if any fixture secret survived into ``text``."""
    for secret in SECRET_FIXTURES:
        assert secret not in text, "leaked {!r} in: {}".format(secret, text)


# --------------------------------------------------------------------------
# sanitize_url
# --------------------------------------------------------------------------


def test_real_debrid_share_url_keeps_route_shape_without_token():
    out = diagnostics.sanitize_url(RD_SHARE_URL)
    assert out == "https://real-debrid.com/d/<redacted>"


def test_real_debrid_delivery_url_redacts_server_label_and_token():
    out = diagnostics.sanitize_url(RD_DELIVERY_URL)
    assert out == "https://<redacted>.download.real-debrid.com/d/<redacted>"


def test_magnet_uri_is_fully_redacted():
    assert diagnostics.sanitize_url(MAGNET_URI) == "magnet:<redacted>"


def test_query_string_and_fragment_are_dropped():
    out = diagnostics.sanitize_url(QUERY_URL + "#frag")
    assert "?" not in out
    assert "#" not in out
    assert_clean(out)


def test_userinfo_is_never_retained():
    out = diagnostics.sanitize_url("https://user:pw@files.example.com/a/b")
    assert "user" not in out
    assert "pw" not in out
    assert "example.com" in out


def test_common_subdomains_are_not_redacted():
    out = diagnostics.sanitize_url("https://www.example.com/")
    assert out == "https://www.example.com/"


def test_sanitize_url_of_non_string_returns_placeholder():
    assert diagnostics.sanitize_url(None) == "<redacted>"
    assert diagnostics.sanitize_url(object()) == "<redacted>"


def test_sanitize_url_route_returns_path_only():
    assert diagnostics.sanitize_url_route(RD_SHARE_URL) == "/d/<redacted>"


def test_sanitize_host_returns_host_only():
    assert diagnostics.sanitize_host(RD_SHARE_URL) == "real-debrid.com"
    assert (
        diagnostics.sanitize_host(RD_DELIVERY_URL)
        == "<redacted>.download.real-debrid.com"
    )


# --------------------------------------------------------------------------
# sanitize_path
# --------------------------------------------------------------------------


def test_windows_profile_root_is_replaced_and_work_id_masked():
    out = diagnostics.sanitize_path(WIN_WORK_PATH)
    assert out.startswith("%USERPROFILE%")
    assert ".cove-work-<work-id>" in out
    assert out.endswith("video.mp4")
    assert_clean(out)


def test_linux_home_is_replaced():
    out = diagnostics.sanitize_path(LINUX_WORK_PATH, home="/home/{}".format(LINUX_USER))
    assert out.startswith("~")
    assert ".cove-work-<work-id>" in out
    assert_clean(out)


def test_deep_path_middle_segments_are_elided():
    deep = "C:\\Users\\{}\\a\\b\\c\\d\\e\\video.mp4".format(WIN_USER)
    out = diagnostics.sanitize_path(deep)
    assert "..." in out
    assert out.endswith("video.mp4")
    assert_clean(out)


def test_localappdata_and_appdata_roots_are_replaced():
    out = diagnostics.sanitize_path(
        "C:\\Users\\{}\\AppData\\Local\\Cove\\logs\\cove.jsonl".format(WIN_USER)
    )
    assert "%LOCALAPPDATA%" in out
    assert_clean(out)
    out = diagnostics.sanitize_path(
        "C:\\Users\\{}\\AppData\\Roaming\\Cove\\x.txt".format(WIN_USER)
    )
    assert "%APPDATA%" in out
    assert_clean(out)


def test_sanitize_path_of_non_string_returns_placeholder():
    assert diagnostics.sanitize_path(None) == "<redacted>"


def test_path_facts_expose_safe_structure_only():
    facts = diagnostics.path_facts(WIN_WORK_PATH)
    assert facts["absolute"] is True
    assert facts["drive"] == "C:"
    assert facts["ext"] == ".mp4"
    assert facts["depth"] >= 4
    assert_clean(json.dumps(facts))


# --------------------------------------------------------------------------
# scrub_text / free-form secrets
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Bearer {}".format(BEARER),
        COOKIE_HEADER,
        "apikey={}".format(RD_TOKEN),
        "passkey={}".format(PASSKEY),
        MAGNET_URI,
        RD_SHARE_URL,
        RD_DELIVERY_URL,
        QUERY_URL,
        WIN_WORK_PATH,
        NATIVE_BODY,
        "failed to open {}".format(LINUX_WORK_PATH),
    ],
)
def test_scrub_text_removes_every_fixture_secret(raw):
    assert_clean(diagnostics.scrub_text(raw))


def test_scrub_text_redacts_custom_indexer_api_key_forms():
    # S2 persists a user-configured Torznab API key under the field name
    # `api_key`. The sanitizer must recognise the conventional keyed-secret
    # spelling so the value can never reach a retained record, even if a
    # future caller logs the record; `apikey` shares the same pattern.
    for raw in (
        "api_key=super-secret-test-key",
        "api_key: super-secret-test-key",
        "apikey=super-secret-test-key",
    ):
        out = diagnostics.scrub_text(raw)
        assert "super-secret-test-key" not in out
        assert "<redacted>" in out


def test_scrub_text_keeps_ordinary_message_readable():
    assert diagnostics.scrub_text("Invalid engine output path") == (
        "Invalid engine output path"
    )


def test_scrub_text_of_non_string_returns_placeholder():
    assert diagnostics.scrub_text(object()) == "<redacted>"


def test_scrub_text_caps_length():
    out = diagnostics.scrub_text("x" * 100000)
    assert len(out) <= diagnostics.MAX_TEXT_LEN + 16


# --------------------------------------------------------------------------
# field sanitization
# --------------------------------------------------------------------------


def test_sanitize_fields_scrubs_values_and_drops_unsupported_objects():
    fields = diagnostics.sanitize_fields(
        {
            "url": RD_SHARE_URL,
            "path": WIN_WORK_PATH,
            "ok": True,
            "count": 3,
            "nothing": None,
            "obj": object(),
            "nested": {"url": QUERY_URL},
            "seq": [MAGNET_URI, 1],
        }
    )
    dumped = json.dumps(fields)
    assert_clean(dumped)
    assert fields["ok"] is True
    assert fields["count"] == 3
    assert fields["nothing"] is None
    assert fields["obj"] == "<redacted>"


def test_sanitize_fields_rejects_settings_like_objects():
    class Settings:
        rd_token = RD_TOKEN

    fields = diagnostics.sanitize_fields({"settings": Settings()})
    assert_clean(json.dumps(fields))


def test_sanitize_fields_caps_key_count():
    fields = diagnostics.sanitize_fields({"k{}".format(i): i for i in range(500)})
    assert len(fields) <= diagnostics.MAX_FIELDS


# --------------------------------------------------------------------------
# sanitize_exception
# --------------------------------------------------------------------------


def _raise_chain():
    try:
        raise FileNotFoundError(2, "No such file", WIN_WORK_PATH)
    except OSError as cause:
        raise ValueError("Invalid engine output path: {}".format(WIN_WORK_PATH)) from cause


def test_sanitize_exception_captures_type_cause_and_errno():
    try:
        _raise_chain()
    except ValueError as exc:
        out = diagnostics.sanitize_exception(exc)
    assert out["type"] == "ValueError"
    assert out["cause"] == "FileNotFoundError"
    assert out["errno"] == 2
    assert "winerror" in out
    assert_clean(json.dumps(out))


def test_sanitize_exception_scrubs_traceback():
    try:
        _raise_chain()
    except ValueError as exc:
        out = diagnostics.sanitize_exception(exc)
    assert out["traceback"]
    assert_clean(out["traceback"])


def test_sanitize_exception_of_non_exception_is_safe():
    out = diagnostics.sanitize_exception("not an exception")
    assert out["type"] == "<redacted>"


def test_sanitizer_failure_returns_placeholder_not_original(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("sanitizer exploded")

    monkeypatch.setattr(diagnostics, "_scrub_once", boom)
    out = diagnostics.scrub_text(RD_SHARE_URL)
    assert out == "<redacted>"


# ==========================================================================
# Phase 2: DiagLogger
# ==========================================================================


import logging  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402
from pathlib import Path  # noqa: E402


@pytest.fixture
def logger(tmp_path):
    log = diagnostics.DiagLogger(log_dir=tmp_path / "logs", filename="cove.jsonl")
    yield log
    log.close()


def _lines(path):
    return [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_emit_writes_one_json_object_per_line(logger, tmp_path):
    logger.emit("app", "app_start", "INFO")
    logger.emit("queue", "task_added", "INFO", task_id=42)
    path = tmp_path / "logs" / "cove.jsonl"
    lines = _lines(path)
    assert len(lines) == 2
    rec = json.loads(lines[1])
    assert rec["component"] == "queue"
    assert rec["event"] == "task_added"
    assert rec["task"] == 42


def test_record_schema_has_required_fields(logger):
    logger.emit("app", "app_start", "INFO")
    rec = logger.records()[0]
    for key in ("ts", "level", "component", "event", "session"):
        assert key in rec


def test_timestamp_is_utc_iso8601_with_milliseconds(logger):
    logger.emit("app", "app_start", "INFO")
    ts = logger.records()[0]["ts"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", ts), ts


def test_session_id_is_eight_hex_and_stable(logger):
    assert re.match(r"^[0-9a-f]{8}$", logger.session)
    logger.emit("app", "a", "INFO")
    logger.emit("app", "b", "INFO")
    sessions = {r["session"] for r in logger.records()}
    assert sessions == {logger.session}


def test_two_loggers_get_different_sessions(tmp_path):
    a = diagnostics.DiagLogger(log_dir=tmp_path / "a")
    b = diagnostics.DiagLogger(log_dir=tmp_path / "b")
    try:
        assert a.session != b.session
    finally:
        a.close()
        b.close()


def test_request_id_is_validated_and_capped(logger):
    logger.emit("native_host", "request_received", "INFO", request_id="51c2a711")
    logger.emit("native_host", "request_received", "INFO", request_id="bad id!")
    logger.emit("native_host", "request_received", "INFO", request_id="f" * 200)
    recs = logger.records()
    assert recs[0]["request"] == "51c2a711"
    assert recs[1].get("request") is None
    assert recs[2].get("request") is None


def test_task_id_must_be_an_int(logger):
    logger.emit("queue", "task_added", "INFO", task_id="not-an-int")
    assert logger.records()[0].get("task") is None


def test_fields_are_sanitized_before_reaching_the_ring(logger):
    logger.emit("debrid", "share_link_rejected", "WARNING", url=RD_SHARE_URL)
    assert_clean(json.dumps(logger.records()))


def test_persisted_lines_are_sanitized(logger, tmp_path):
    logger.emit("debrid", "share_link_rejected", "WARNING", url=RD_SHARE_URL,
                path=WIN_WORK_PATH)
    assert_clean((tmp_path / "logs" / "cove.jsonl").read_text(encoding="utf-8"))


def test_exception_is_sanitized_and_attached(logger):
    try:
        _raise_chain()
    except ValueError as exc:
        logger.emit("extractor.publish", "engine_output_rejected", "ERROR", exc=exc)
    rec = logger.records()[0]
    assert rec["exc"]["type"] == "ValueError"
    assert rec["exc"]["cause"] == "FileNotFoundError"
    assert_clean(json.dumps(rec))


def test_ring_is_bounded(tmp_path):
    log = diagnostics.DiagLogger(log_dir=tmp_path / "logs", ring_size=10)
    try:
        for i in range(50):
            log.emit("app", "tick", "INFO", i=i)
        recs = log.records()
        assert len(recs) == 10
        assert recs[-1]["fields"]["i"] == 49
    finally:
        log.close()


def test_default_ring_size_is_500():
    assert diagnostics.RING_SIZE == 500


def test_rotation_creates_numbered_backups(tmp_path):
    log = diagnostics.DiagLogger(
        log_dir=tmp_path / "logs", max_bytes=800, backups=3
    )
    try:
        for i in range(200):
            log.emit("app", "tick", "INFO", i=i, pad="y" * 40)
    finally:
        log.close()
    d = tmp_path / "logs"
    assert (d / "cove.jsonl").exists()
    assert (d / "cove.jsonl.1").exists()
    assert not (d / "cove.jsonl.4").exists()


def test_active_log_stays_under_the_size_limit(tmp_path):
    log = diagnostics.DiagLogger(log_dir=tmp_path / "logs", max_bytes=1000, backups=2)
    try:
        for i in range(300):
            log.emit("app", "tick", "INFO", i=i, pad="y" * 40)
    finally:
        log.close()
    assert (tmp_path / "logs" / "cove.jsonl").stat().st_size <= 1000 * 2
def test_default_limits_match_the_spec():
    assert diagnostics.MAX_BYTES == 2 * 1024 * 1024
    assert diagnostics.BACKUPS == 3
    assert diagnostics.NATIVE_MAX_BYTES == 512 * 1024
    assert diagnostics.NATIVE_BACKUPS == 2


def test_read_jsonl_skips_and_counts_malformed_lines(tmp_path):
    path = tmp_path / "x.jsonl"
    path.write_text(
        '{"ts":"t","level":"INFO","component":"app","event":"a","session":"1"}\n'
        "not json at all\n"
        "[1,2,3]\n"
        '{"ts":"t","level":"INFO","component":"app","event":"b","session":"1"}\n',
        encoding="utf-8",
    )
    records, skipped = diagnostics.read_jsonl(path)
    assert [r["event"] for r in records] == ["a", "b"]
    assert skipped == 2


def test_read_jsonl_of_missing_file_is_empty(tmp_path):
    assert diagnostics.read_jsonl(tmp_path / "nope.jsonl") == ([], 0)


def test_unwritable_log_dir_falls_back_to_memory_only(tmp_path):
    blocker = tmp_path / "logs"
    blocker.write_text("not a directory", encoding="utf-8")
    log = diagnostics.DiagLogger(log_dir=blocker)
    try:
        log.emit("app", "app_start", "INFO")
        assert log.memory_only is True
        events = [r["event"] for r in log.records()]
        assert "log_sink_unavailable" in events
        assert "app_start" in events
    finally:
        log.close()


def test_write_failure_mid_run_does_not_raise(logger, monkeypatch):
    def boom(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(logger, "_write_line", boom)
    logger.emit("app", "app_start", "INFO")
    assert logger.records()


def test_rotation_failure_falls_back_to_truncation(tmp_path, monkeypatch):
    log = diagnostics.DiagLogger(log_dir=tmp_path / "logs", max_bytes=400, backups=2)
    try:
        monkeypatch.setattr(
            diagnostics.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("locked"))
        )
        for i in range(100):
            log.emit("app", "tick", "INFO", i=i, pad="y" * 40)
        assert (tmp_path / "logs" / "cove.jsonl").stat().st_size <= 400 * 3
    finally:
        log.close()


def test_emit_never_raises_on_hostile_input(logger):
    class Hostile:
        def __repr__(self):
            raise RuntimeError("nope")

    logger.emit("app", "weird", "INFO", value=Hostile(), other=object())
    logger.emit(None, None, None)
    logger.emit("app", "x", "NOPE")


def test_debug_records_are_dropped_unless_debug_is_on(logger):
    logger.emit("app", "detail", "DEBUG")
    assert logger.records() == []
    logger.set_debug(True)
    assert logger.debug is True
    logger.emit("app", "detail", "DEBUG")
    assert [r["event"] for r in logger.records()] == ["detail"]


def test_debug_mode_defaults_off_and_is_not_persisted(logger):
    assert logger.debug is False
    logger.set_debug(True)
    fresh = diagnostics.DiagLogger(log_dir=logger.log_dir)
    try:
        assert fresh.debug is False
    finally:
        fresh.close()


def test_render_is_human_readable_and_sanitized(logger):
    logger.emit("queue", "task_failed", "ERROR", task_id=7, url=RD_SHARE_URL)
    text = diagnostics.format_records(logger.records(), source="app")
    assert "task_failed" in text
    assert "[app]" in text
    assert "ERROR" in text
    assert_clean(text)


def test_format_records_tolerates_malformed_records():
    text = diagnostics.format_records([{"junk": 1}, "not a dict", None])
    assert isinstance(text, str)


def test_attaching_python_logging_touches_only_the_cove_logger(logger):
    root_before = list(logging.getLogger().handlers)
    handler = diagnostics.attach_python_logging(logger)
    try:
        assert logging.getLogger().handlers == root_before
        assert handler in logging.getLogger("cove").handlers
        logging.getLogger("cove.queue").warning("failed for %s", RD_SHARE_URL)
        assert_clean(json.dumps(logger.records()))
        assert any(r["component"] == "cove.queue" for r in logger.records())
    finally:
        diagnostics.detach_python_logging(handler)
        assert handler not in logging.getLogger("cove").handlers


def test_third_party_logger_output_is_not_captured(logger):
    handler = diagnostics.attach_python_logging(logger)
    try:
        logging.getLogger("urllib3.connectionpool").warning("secret %s", RD_SHARE_URL)
        assert logger.records() == []
    finally:
        diagnostics.detach_python_logging(handler)


def test_emit_is_thread_safe(logger):
    def worker():
        for i in range(100):
            logger.emit("app", "tick", "INFO", i=i)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(_lines(logger.log_dir / "cove.jsonl")) == 400


def test_module_level_emit_is_a_no_op_before_init():
    diagnostics.shutdown_logger()
    assert diagnostics.get_logger() is None
    diagnostics.emit("app", "app_start", "INFO")


def test_init_app_logger_uses_a_logs_subdirectory(tmp_path):
    log = diagnostics.init_app_logger(tmp_path)
    try:
        assert log.log_dir == tmp_path / "logs"
        assert diagnostics.get_logger() is log
        diagnostics.emit("app", "app_start", "INFO")
        assert log.records()
    finally:
        diagnostics.shutdown_logger()


def test_init_app_logger_survives_an_unusable_data_dir(tmp_path):
    blocker = tmp_path / "data"
    blocker.write_text("file where a directory should be", encoding="utf-8")
    log = diagnostics.init_app_logger(blocker)
    try:
        assert log is not None
        diagnostics.emit("app", "app_start", "INFO")
        assert log.memory_only is True
    finally:
        diagnostics.shutdown_logger()


def test_no_temp_files_are_left_in_the_repo(logger):
    logger.emit("app", "app_start", "INFO")
    assert not any(p.name.startswith("cove.jsonl") for p in Path(".").glob("*"))



# ==========================================================================
# Phase 3 support: observers and environment facts
# ==========================================================================


def test_observers_receive_accepted_records_only(logger):
    seen = []
    logger.add_observer(seen.append)
    logger.emit("app", "app_start", "INFO")
    logger.emit("app", "detail", "DEBUG")
    assert [r["event"] for r in seen] == ["app_start"]
    logger.remove_observer(seen.append)


def test_observer_receives_sanitized_records(logger):
    seen = []
    logger.add_observer(seen.append)
    logger.emit("debrid", "share_link_rejected", "WARNING", url=RD_SHARE_URL)
    assert_clean(json.dumps(seen))


def test_failing_observer_cannot_break_emit(logger):
    def boom(_record):
        raise RuntimeError("observer exploded")

    logger.add_observer(boom)
    logger.emit("app", "app_start", "INFO")
    assert logger.records()


def test_remove_observer_stops_delivery(logger):
    seen = []
    logger.add_observer(seen.append)
    logger.remove_observer(seen.append)
    logger.emit("app", "app_start", "INFO")
    assert seen == []


def test_environment_facts_are_safe_and_complete():
    facts = diagnostics.environment_facts()
    for key in ("app_version", "os", "os_version", "arch", "mode"):
        assert key in facts
    assert facts["mode"] in ("source", "appimage", "installed", "portable")
    assert_clean(json.dumps(facts))


def test_support_header_includes_environment_and_notice():
    header = diagnostics.support_header(session="abcd1234", filters="level=ERROR")
    assert "abcd1234" in header
    assert "level=ERROR" in header
    assert diagnostics.SANITIZATION_NOTICE in header


# ==========================================================================
# Phase 4: application integration
# ==========================================================================


@pytest.fixture
def app_diag(tmp_path):
    from cove import app as cove_app

    diagnostics.shutdown_logger()
    log = cove_app.init_diagnostics(tmp_path)
    yield cove_app, log
    cove_app.shutdown_diagnostics()
    diagnostics.shutdown_logger()


def test_init_diagnostics_installs_the_process_logger(app_diag, tmp_path):
    _, log = app_diag
    assert diagnostics.get_logger() is log
    assert log.log_dir == tmp_path / "logs"


def test_init_diagnostics_is_explicit_not_an_import_side_effect():
    diagnostics.shutdown_logger()
    import importlib

    from cove import app as cove_app

    importlib.reload(cove_app)
    assert diagnostics.get_logger() is None


def test_app_start_event_carries_environment_facts(app_diag):
    _, log = app_diag
    start = [r for r in log.records() if r["event"] == "app_start"]
    assert len(start) == 1
    fields = start[0]["fields"]
    for key in ("app_version", "os", "arch", "mode"):
        assert key in fields
    assert_clean(json.dumps(start))


def test_init_diagnostics_attaches_only_the_cove_logger(app_diag):
    cove_app, log = app_diag
    root_handlers = list(logging.getLogger().handlers)
    assert any(
        isinstance(h, diagnostics._DiagHandler)
        for h in logging.getLogger("cove").handlers
    )
    assert logging.getLogger().handlers == root_handlers
    logging.getLogger("cove").error("boom %s", RD_SHARE_URL)
    assert_clean(json.dumps(log.records()))


def test_shutdown_emits_app_stop_and_detaches(app_diag):
    cove_app, log = app_diag
    cove_app.shutdown_diagnostics()
    assert [r["event"] for r in log.records()][-1] == "app_stop"
    assert not any(
        isinstance(h, diagnostics._DiagHandler)
        for h in logging.getLogger("cove").handlers
    )
    assert diagnostics.get_logger() is None


def test_shutdown_is_safe_to_call_twice(app_diag):
    cove_app, _ = app_diag
    cove_app.shutdown_diagnostics()
    cove_app.shutdown_diagnostics()


def test_init_diagnostics_survives_an_unwritable_data_dir(tmp_path):
    from cove import app as cove_app

    diagnostics.shutdown_logger()
    blocker = tmp_path / "data"
    blocker.write_text("not a directory", encoding="utf-8")
    log = cove_app.init_diagnostics(blocker)
    try:
        assert log is not None
        assert log.memory_only is True
    finally:
        cove_app.shutdown_diagnostics()


def test_init_diagnostics_never_raises(tmp_path, monkeypatch):
    from cove import app as cove_app

    diagnostics.shutdown_logger()
    monkeypatch.setattr(
        diagnostics, "init_app_logger",
        lambda _d: (_ for _ in ()).throw(RuntimeError("no logger for you")),
    )
    assert cove_app.init_diagnostics(tmp_path) is None
    cove_app.shutdown_diagnostics()


def test_cove_logger_keeps_writing_warnings_to_stderr(capsys, app_diag):
    """Attaching a handler to the cove logger disables logging's lastResort
    fallback, so startup diagnostics that used to reach stderr would vanish."""
    logging.getLogger("cove").error("settings_unreadable: cannot read settings")
    assert "settings_unreadable" in capsys.readouterr().err


def test_stderr_fallback_is_removed_on_shutdown(app_diag):
    cove_app, _ = app_diag
    cove_app.shutdown_diagnostics()
    assert logging.getLogger("cove").handlers == []


# ==========================================================================
# Phase 5 support: URL classification and output-path rule names
# ==========================================================================


def test_url_facts_classify_a_real_debrid_share_link():
    facts = diagnostics.url_facts(RD_SHARE_URL)
    assert facts["scheme"] == "https"
    assert facts["host"] == "real-debrid.com"
    assert facts["classification"] == "real_debrid_generated_link"
    assert facts["provider"] == "real_debrid"
    assert facts["route"] == "/d/<redacted>"
    assert_clean(json.dumps(facts))


def test_url_facts_classify_a_delivery_link_separately():
    facts = diagnostics.url_facts(RD_DELIVERY_URL)
    assert facts["classification"] == "debrid_delivery_link"
    assert facts["host"] == "<redacted>.download.real-debrid.com"
    assert_clean(json.dumps(facts))


def test_url_facts_classify_alldebrid_share_links():
    facts = diagnostics.url_facts("https://www.alldebrid.com/f/XYZ789")
    assert facts["classification"] == "all_debrid_share_link"
    assert facts["provider"] == "all_debrid"


@pytest.mark.parametrize(
    "url,expected",
    [
        (MAGNET_URI, "magnet"),
        ("https://example.com/a/b.torrent", "torrent_file"),
        ("https://example.com/video.mp4", "http_direct"),
        ("ftp://example.com/video.mp4", "ftp_direct"),
        ("not a url", "other"),
    ],
)
def test_url_facts_classification_table(url, expected):
    assert diagnostics.url_facts(url)["classification"] == expected


def test_url_facts_never_raise():
    assert diagnostics.url_facts(None)["classification"] == "other"


@pytest.mark.parametrize(
    "message,rule",
    [
        ("Invalid engine output path: C:\\x", "invalid_engine_output_path"),
        ("Engine output is outside its private directory: /x", "outside_private_directory"),
        ("Engine output is the private directory: /x", "output_is_private_directory"),
        ("Engine output contains a symlink: /x", "symlink_in_output"),
        ("Engine output is not a regular file: /x", "not_a_regular_file"),
        ("Engine did not report a final output path", "no_reported_output_path"),
        ("yt-dlp did not report a final output path", "no_reported_output_path"),
        ("Private output directory is missing: /x", "work_directory_missing"),
        ("Private output directory ownership changed: /x", "work_directory_ownership_changed"),
        ("Destination directory is missing: /x", "destination_missing"),
        ("Destination directory ownership changed: /x", "destination_ownership_changed"),
        ("Destination is not a directory: /x", "destination_not_a_directory"),
        ("Could not create private output directory: /x", "work_directory_create_failed"),
        (
            "Private output directory is not on the destination filesystem: /x",
            "work_directory_wrong_filesystem",
        ),
        ("Validated Windows directory identity is unavailable", "windows_identity_unavailable"),
        ("something nobody predicted", "other"),
    ],
)
def test_output_path_rule_names_are_stable_and_safe(message, rule):
    assert diagnostics.classify_output_path_error(message) == rule


def test_output_path_rule_classification_never_raises():
    assert diagnostics.classify_output_path_error(None) == "other"


def test_stable_event_vocabulary_survives_scrubbing():
    """Long snake_case identifiers are the diagnostics vocabulary, not tokens."""
    for word in (
        "real_debrid_generated_link",
        "invalid_engine_output_path",
        "work_directory_ownership_changed",
        "unsupported_share_link",
    ):
        assert diagnostics.scrub_text(word) == word


def test_uuid_shaped_identifiers_are_still_redacted():
    out = diagnostics.scrub_text("id=550e8400-e29b-41d4-a716-446655440000")
    assert "550e8400" not in out


def test_scrubbing_a_url_leaves_its_route_shape_intact():
    """The path rule must not chew through a URL the URL rule already handled:
    the route class is what identifies a share-link incident."""
    assert diagnostics.scrub_text(RD_SHARE_URL) == "https://real-debrid.com/d/<redacted>"
    assert diagnostics.scrub_text(RD_DELIVERY_URL) == (
        "https://<redacted>.download.real-debrid.com/d/<redacted>"
    )


def test_scrubbing_still_redacts_a_bare_absolute_path():
    out = diagnostics.scrub_text("failed to open /srv/media/{}/movie.mp4".format(LINUX_USER))
    assert LINUX_USER not in out
    assert "..." in out


def test_scrubbing_a_url_inside_a_sentence_keeps_the_sentence():
    out = diagnostics.scrub_text("could not fetch {} for task 7".format(RD_SHARE_URL))
    assert out == "could not fetch https://real-debrid.com/d/<redacted> for task 7"


# ---- AppImage install mode -------------------------------------------------
#
# An AppImage runs Cove from source inside its mounted AppDir, so the support
# log called it a plain "source" run - the one shape most likely to be behind a
# Linux extension report. Labelling only; nothing about DATA_DIR, the IPC
# endpoint name or the native-host manifest depends on it.


def test_install_mode_reports_an_appimage_run(tmp_path, monkeypatch):
    image = tmp_path / "Cove-x86_64.AppImage"
    image.write_bytes(b"appimage")
    monkeypatch.setenv("APPIMAGE", str(image))

    assert diagnostics.install_mode() == "appimage"


def test_install_mode_reports_an_ordinary_source_run(monkeypatch):
    monkeypatch.delenv("APPIMAGE", raising=False)

    assert diagnostics.install_mode() == "source"


def test_install_mode_ignores_an_appimage_variable_pointing_nowhere(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "missing.AppImage"))

    assert diagnostics.install_mode() == "source"


def test_install_mode_still_reports_frozen_builds(monkeypatch):
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr("cove.portable.is_portable", lambda: True)
    assert diagnostics.install_mode() == "portable"
    monkeypatch.setattr("cove.portable.is_portable", lambda: False)
    assert diagnostics.install_mode() == "installed"


def test_rotation_counts_bytes_not_characters(tmp_path):
    """max_bytes is a byte budget, and the log is written as UTF-8.

    Counting str characters undercounts every multibyte one, so a log full of
    localised text, filenames or URLs blew straight past its configured cap.
    """
    log = diagnostics.DiagLogger(log_dir=tmp_path / "logs", max_bytes=1000, backups=2)
    try:
        for i in range(300):
            # Four bytes per character in UTF-8: the widest undercount there is.
            log.emit("app", "tick", "INFO", i=i, pad="𝄞" * 40)
    finally:
        log.close()

    # The cap is enforced before each write, so the active file never exceeds
    # it. Counting characters let it reach roughly 1.5x here, and worse for
    # logs that are mostly non-ASCII.
    assert (tmp_path / "logs" / "cove.jsonl").stat().st_size <= 1000
