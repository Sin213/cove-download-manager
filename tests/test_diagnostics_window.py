"""Tests for the Qt bridge and the Diagnostics window.

The window is a support tool: it may only ever show records that the logger
already sanitized, and it must never become a second, unsynchronized logging
system of its own.
"""
import json

import pytest

from cove import diagnostics, diagnostics_bridge, diagnostics_window

from tests.test_diagnostics import (
    RD_SHARE_URL,
    WIN_WORK_PATH,
    assert_clean,
)


@pytest.fixture
def logger(tmp_path):
    log = diagnostics.DiagLogger(log_dir=tmp_path / "logs", filename="cove.jsonl")
    yield log
    log.close()


@pytest.fixture
def window(logger, qt_pump):
    win = diagnostics_window.DiagnosticsWindow(logger, host_log_dir=logger.log_dir)
    yield win
    win.close()
    diagnostics_window.close_diagnostics()


@pytest.fixture
def qt_pump():
    from PySide6.QtWidgets import QApplication

    def pump():
        app = QApplication.instance()
        for _ in range(3):
            app.sendPostedEvents(None, 0)
            app.processEvents()

    return pump


# --------------------------------------------------------------------------
# Bridge
# --------------------------------------------------------------------------


def test_bridge_forwards_sanitized_records_into_qt(logger, qt_pump):
    bridge = diagnostics_bridge.DiagnosticsBridge(logger)
    seen = []
    bridge.record_added.connect(seen.append)
    try:
        logger.emit("debrid", "share_link_rejected", "WARNING", url=RD_SHARE_URL)
        qt_pump()
        assert [r["event"] for r in seen] == ["share_link_rejected"]
        assert_clean(json.dumps(seen))
    finally:
        bridge.close()


def test_bridge_does_not_persist_a_second_copy(logger, tmp_path, qt_pump):
    bridge = diagnostics_bridge.DiagnosticsBridge(logger)
    try:
        logger.emit("app", "app_start", "INFO")
        qt_pump()
        lines = (tmp_path / "logs" / "cove.jsonl").read_text(encoding="utf-8")
        assert lines.count("app_start") == 1
    finally:
        bridge.close()


def test_bridge_close_detaches_the_observer(logger, qt_pump):
    bridge = diagnostics_bridge.DiagnosticsBridge(logger)
    seen = []
    bridge.record_added.connect(seen.append)
    bridge.close()
    logger.emit("app", "app_start", "INFO")
    qt_pump()
    assert seen == []


# --------------------------------------------------------------------------
# Window basics
# --------------------------------------------------------------------------


def test_window_is_not_modal(window):
    assert window.isModal() is False


def test_show_diagnostics_reuses_one_window(logger, qt_pump):
    first = diagnostics_window.show_diagnostics(logger=logger)
    second = diagnostics_window.show_diagnostics(logger=logger)
    try:
        assert first is second
    finally:
        diagnostics_window.close_diagnostics()


def test_window_header_reports_environment_and_session(window, logger):
    text = window.header_label.text()
    assert logger.session in text
    for key in ("app version", "os", "arch", "install mode"):
        assert key in text.lower()


def test_window_shows_the_sanitization_notice(window):
    assert diagnostics.SANITIZATION_NOTICE in window.notice_label.text()


def test_window_shows_debug_status(window, logger):
    assert "off" in window.debug_status_label.text().lower()
    window.debug_check.setChecked(True)
    assert logger.debug is True
    assert "on" in window.debug_status_label.text().lower()


def test_debug_checkbox_starts_unchecked(window):
    assert window.debug_check.isChecked() is False


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


def _seed(logger):
    logger.emit("app", "app_start", "INFO")
    logger.emit("queue", "task_added", "INFO", task_id=7)
    logger.emit("queue", "task_failed", "ERROR", task_id=7)
    logger.emit("native_host", "ipc_result", "WARNING", request_id="51c2a711")


def test_level_filter_hides_lower_levels(window, logger):
    _seed(logger)
    window.set_level_filter("ERROR")
    events = [r["event"] for r in window.visible_records()]
    assert events == ["task_failed"]


def test_component_filter_selects_one_family(window, logger):
    _seed(logger)
    window.set_component_filter("queue")
    events = [r["event"] for r in window.visible_records()]
    assert events == ["task_added", "task_failed"]


def test_id_filter_matches_task_id(window, logger):
    _seed(logger)
    window.set_id_filter("7")
    assert {r["event"] for r in window.visible_records()} == {
        "task_added",
        "task_failed",
    }


def test_id_filter_matches_request_id(window, logger):
    _seed(logger)
    window.set_id_filter("51c2a711")
    assert [r["event"] for r in window.visible_records()] == ["ipc_result"]


def test_search_matches_rendered_text(window, logger):
    _seed(logger)
    window.set_search("ipc")
    assert [r["event"] for r in window.visible_records()] == ["ipc_result"]


def test_filters_combine(window, logger):
    _seed(logger)
    window.set_level_filter("WARNING")
    window.set_component_filter("queue")
    assert [r["event"] for r in window.visible_records()] == ["task_failed"]


def test_visible_records_are_bounded(window, logger):
    for i in range(diagnostics_window.MAX_VISIBLE + 200):
        logger.emit("app", "tick", "INFO", i=i)
    assert len(window.visible_records()) <= diagnostics_window.MAX_VISIBLE


# --------------------------------------------------------------------------
# Support actions
# --------------------------------------------------------------------------


def test_copy_text_has_header_events_and_notice(window, logger):
    _seed(logger)
    logger.emit("debrid", "share_link_rejected", "WARNING", url=RD_SHARE_URL,
                path=WIN_WORK_PATH)
    text = window.report_text()
    assert "Cove diagnostics report" in text
    assert "task_failed" in text
    assert diagnostics.SANITIZATION_NOTICE in text
    assert_clean(text)


def test_copy_text_reflects_active_filters(window, logger):
    _seed(logger)
    window.set_component_filter("queue")
    text = window.report_text()
    assert "task_failed" in text
    assert "ipc_result" not in text
    assert "component=queue" in text


def test_copy_diagnostics_puts_the_report_on_the_clipboard(window, logger, qt_pump):
    from PySide6.QtWidgets import QApplication

    _seed(logger)
    window.copy_diagnostics()
    qt_pump()
    assert "task_failed" in QApplication.clipboard().text()


def test_save_writes_one_sanitized_text_file(window, logger, tmp_path):
    _seed(logger)
    logger.emit("debrid", "share_link_rejected", "WARNING", url=RD_SHARE_URL)
    target = tmp_path / "report.txt"
    assert window.save_to(target) is True
    body = target.read_text(encoding="utf-8")
    assert "Cove diagnostics report" in body
    assert_clean(body)


def test_save_failure_is_reported_not_raised(window, tmp_path):
    assert window.save_to(tmp_path / "missing-dir" / "x" / "report.txt") is False


def test_open_log_folder_uses_the_local_open_helper(window, logger, monkeypatch):
    opened = []
    monkeypatch.setattr(diagnostics_window, "_open_folder", lambda p: opened.append(p))
    window.open_log_folder()
    assert opened == [logger.log_dir]


def test_clear_empties_the_view_without_deleting_log_files(window, logger, tmp_path):
    _seed(logger)
    path = tmp_path / "logs" / "cove.jsonl"
    before = path.read_text(encoding="utf-8")
    window.clear_view()
    assert window.visible_records() == []
    assert path.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------
# Native-host merge
# --------------------------------------------------------------------------


def _write_host_log(log_dir, lines):
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / diagnostics.NATIVE_LOG_NAME).write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def test_host_records_are_merged_and_labelled(logger, tmp_path, qt_pump):
    _write_host_log(
        logger.log_dir,
        [
            json.dumps(
                {
                    "ts": "2026-08-06T12:00:00.000Z",
                    "level": "WARNING",
                    "component": "native_host",
                    "event": "ipc_result",
                    "session": "aaaabbbb",
                }
            )
        ],
    )
    win = diagnostics_window.DiagnosticsWindow(logger, host_log_dir=logger.log_dir)
    try:
        logger.emit("app", "app_start", "INFO")
        win.reload()
        text = win.view.toPlainText()
        assert "[host]" in text
        assert "[app]" in text
    finally:
        win.close()


def test_malformed_host_records_are_skipped(logger, tmp_path):
    _write_host_log(logger.log_dir, ["{ not json", "[1,2]"])
    win = diagnostics_window.DiagnosticsWindow(logger, host_log_dir=logger.log_dir)
    try:
        win.reload()
        assert win.skipped_host_records == 2
    finally:
        win.close()


def test_host_records_are_sanitized_on_the_way_in(logger, tmp_path):
    _write_host_log(
        logger.log_dir,
        [
            json.dumps(
                {
                    "ts": "2026-08-06T12:00:00.000Z",
                    "level": "ERROR",
                    "component": "native_host",
                    "event": "ipc_result",
                    "session": "aaaabbbb",
                    "fields": {"url": RD_SHARE_URL, "path": WIN_WORK_PATH},
                }
            )
        ],
    )
    win = diagnostics_window.DiagnosticsWindow(logger, host_log_dir=logger.log_dir)
    try:
        win.reload()
        assert_clean(win.view.toPlainText())
        assert_clean(win.report_text())
    finally:
        win.close()


# --------------------------------------------------------------------------
# Scrolling and pre-filtering
# --------------------------------------------------------------------------


def test_new_records_do_not_yank_a_user_reading_older_entries(window, logger, qt_pump):
    for i in range(200):
        logger.emit("app", "tick", "INFO", i=i)
    window.reload()
    bar = window.view.verticalScrollBar()
    bar.setValue(0)
    logger.emit("app", "tick", "INFO", i=999)
    window.reload()
    assert bar.value() == 0


def test_view_follows_when_already_at_the_bottom(window, logger, qt_pump):
    for i in range(200):
        logger.emit("app", "tick", "INFO", i=i)
    window.reload()
    bar = window.view.verticalScrollBar()
    bar.setValue(bar.maximum())
    logger.emit("app", "tick", "INFO", i=999)
    window.reload()
    assert bar.value() == bar.maximum()


def test_show_diagnostics_can_prefilter_to_a_task(logger):
    win = diagnostics_window.show_diagnostics(logger=logger, task_id=7)
    try:
        assert win.id_edit.text() == "7"
    finally:
        diagnostics_window.close_diagnostics()


def test_prefiltering_an_open_window_reuses_it(logger):
    first = diagnostics_window.show_diagnostics(logger=logger)
    second = diagnostics_window.show_diagnostics(logger=logger, task_id=42)
    try:
        assert first is second
        assert first.id_edit.text() == "42"
    finally:
        diagnostics_window.close_diagnostics()


# --------------------------------------------------------------------------
# Main-window entry points
#
# MainWindow's constructor is heavy, so the real unbound methods are driven
# on a light host, the same pattern tests/test_extension_banner.py uses.
# --------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from PySide6.QtWidgets import QMainWindow, QMenu  # noqa: E402

import cove.main_window as mw  # noqa: E402


class _Host(QMainWindow):
    _build_actionbar = mw.MainWindow._build_actionbar
    _open_diagnostics = mw.MainWindow._open_diagnostics
    _open_context_menu = mw.MainWindow._open_context_menu

    def __init__(self):
        super().__init__()
        self._add_download = lambda: None
        self._add_from_clipboard = lambda: None
        self._open_downloads_folder = lambda: None
        self._open_settings = lambda: None
        self._clear_completed = lambda _v: None
        self._toggle_queue = lambda: None


def test_action_bar_has_a_logs_button(logger):
    host = _Host()
    try:
        host._build_actionbar()
        assert host.logs_btn.text() == "Logs"
    finally:
        host.close()


def test_logs_button_opens_the_diagnostics_window(logger, monkeypatch):
    opened = []
    monkeypatch.setattr(
        diagnostics_window, "show_diagnostics",
        lambda **kw: opened.append(kw) or "window",
    )
    host = _Host()
    try:
        host._build_actionbar()
        host.logs_btn.click()
        assert opened == [{"parent": None, "task_id": None}]
    finally:
        host.close()


def test_open_diagnostics_can_prefilter_to_a_failed_task(logger, monkeypatch):
    opened = []
    monkeypatch.setattr(
        diagnostics_window, "show_diagnostics",
        lambda **kw: opened.append(kw) or "window",
    )
    host = _Host()
    try:
        host._open_diagnostics(task_id=7)
        assert opened[0]["task_id"] == 7
    finally:
        host.close()


def test_open_diagnostics_failure_is_swallowed(logger, monkeypatch):
    monkeypatch.setattr(
        diagnostics_window, "show_diagnostics",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("no window")),
    )
    host = _Host()
    try:
        assert host._open_diagnostics() is None
    finally:
        host.close()


def _menu_actions_for_status(status, tid=7):
    """Collect the context-menu labels MainWindow builds for one row."""
    labels = []

    class _Menu(QMenu):
        def addAction(self, text, *a):
            labels.append(text)
            return super().addAction(text, *a)

        def exec(self, *a):
            return None

    task = SimpleNamespace(id=tid, status=status, backend="aria2", url="",
                           filename=None, out_dir="")
    host = _Host()
    host.queue = SimpleNamespace(tasks={tid: task})
    host.tree = SimpleNamespace(
        itemAt=lambda _p: SimpleNamespace(data=lambda *_a: tid),
        setCurrentItem=lambda _i: None,
        viewport=lambda: SimpleNamespace(mapToGlobal=lambda p: p),
    )
    host._selected_tids = lambda: [tid]
    host._task_path = lambda _t: None
    host._add_source_action = lambda _m, _t: None
    host._remove_selected = lambda **kw: None
    host._clear_all = lambda: None
    try:
        with_menu = mw.QMenu
        mw.QMenu = _Menu
        try:
            host._open_context_menu(None)
        finally:
            mw.QMenu = with_menu
    finally:
        host.close()
    return labels


def test_failed_rows_offer_view_logs(logger):
    assert "View logs" in _menu_actions_for_status("error")


def test_healthy_rows_do_not_offer_view_logs(logger):
    assert "View logs" not in _menu_actions_for_status("queued")


def test_an_open_window_updates_live_from_the_logger(logger, qt_pump):
    win = diagnostics_window.show_diagnostics(logger=logger)
    try:
        logger.emit("queue", "task_failed", "ERROR", task_id=7)
        qt_pump()
        assert "task_failed" in win.view.toPlainText()
    finally:
        diagnostics_window.close_diagnostics()
        qt_pump()


def test_closing_the_window_detaches_its_bridge(logger, qt_pump):
    win = diagnostics_window.show_diagnostics(logger=logger)
    before = len(logger._observers)
    diagnostics_window.close_diagnostics()
    qt_pump()
    assert len(logger._observers) < before
    assert win._bridge is None


# --------------------------------------------------------------------------
# Phase 8: one sweep across every output surface
# --------------------------------------------------------------------------


def test_no_fixture_secret_survives_any_surface(logger, tmp_path, qt_pump):
    """Push the whole secret fixture bank through every retained surface.

    The individual sanitizer tests prove each rule; this proves the wiring,
    which is where a leak actually happens - a field that skipped
    sanitize_fields, a host record merged raw, a report built from the ring
    instead of the filtered view.
    """
    from tests.test_diagnostics import (
        BEARER, COOKIE_HEADER, LINUX_WORK_PATH, MAGNET_URI, NATIVE_BODY,
        QUERY_URL, RD_DELIVERY_URL, WIN_WORK_PATH,
    )

    fixtures = {
        "share": RD_SHARE_URL,
        "delivery": RD_DELIVERY_URL,
        "magnet": MAGNET_URI,
        "query": QUERY_URL,
        "bearer": "Authorization: Bearer {}".format(BEARER),
        "cookie": COOKIE_HEADER,
        "winpath": WIN_WORK_PATH,
        "linuxpath": LINUX_WORK_PATH,
        "native": NATIVE_BODY,
    }
    for name, value in fixtures.items():
        logger.emit("debrid", "share_link_rejected", "WARNING", task_id=1, **{name: value})
    try:
        raise ValueError("failed on {} with {}".format(WIN_WORK_PATH, RD_SHARE_URL))
    except ValueError as exc:
        logger.emit("extractor.publish", "engine_output_rejected", "ERROR", exc=exc)

    # A native-host record written by another process, merged into the view.
    _write_host_log(
        logger.log_dir,
        [
            json.dumps({
                "ts": "2026-08-06T12:00:00.000Z", "level": "ERROR",
                "component": "native_host", "event": "ipc_result",
                "session": "aaaabbbb",
                "fields": dict(fixtures),
            })
        ],
    )

    win = diagnostics_window.DiagnosticsWindow(logger, host_log_dir=logger.log_dir)
    try:
        win.reload()
        saved = tmp_path / "report.txt"
        assert win.save_to(saved) is True

        surfaces = {
            "ring": json.dumps(logger.records()),
            "jsonl": (logger.log_dir / "cove.jsonl").read_text(encoding="utf-8"),
            "view": win.view.toPlainText(),
            "copy": win.report_text(),
            "saved": saved.read_text(encoding="utf-8"),
            "host_merge": json.dumps(win._host_records),
        }
        for label, text in surfaces.items():
            try:
                assert_clean(text)
            except AssertionError as failure:
                raise AssertionError("{}: {}".format(label, failure)) from failure
    finally:
        win.close()
