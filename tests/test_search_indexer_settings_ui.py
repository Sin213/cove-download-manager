"""Settings UI for custom Torznab indexers (Search v2 slice S6).

Covers the SettingsDialog custom-indexer section and the IndexerEditorDialog
Add/Edit flow: existing config rendering, empty state, add/edit/remove/enable
through a draft that only lands on the live Settings object on accept, secret
masking and clearing, non-blocking caps-only Test Connection, and the S2/S3/S4
delegation boundary. No test here touches the network: Test Connection probes a
monkeypatched TorznabSource, and settings round-trips use an isolated temp
config file.
"""
import threading
import uuid

import pytest
import shiboken6
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit

import cove.config as config
import cove.dialogs as dialogs
from cove.dialogs import SettingsDialog
from cove.indexer_editor import IndexerEditorDialog
from cove.search.indexers import CustomTorznabIndexer, new_custom_indexer_id
from cove.search.models import SourceError, SourceErrorKind

QApplication.instance() or QApplication([])

ID_A = "custom:00000000-0000-0000-0000-000000000001"
ID_B = "custom:00000000-0000-0000-0000-000000000002"
ID_C = "custom:00000000-0000-0000-0000-000000000003"
ID_D = "custom:00000000-0000-0000-0000-000000000004"
ENDPOINT_A = "http://127.0.0.1:9696/torznab/api"
ENDPOINT_B = "http://192.168.1.20:9117/api"
ENDPOINT_C = "https://example.invalid/torznab/api"
SECRET = "super-secret-s6-key"

_live_hosts = []


@pytest.fixture(autouse=True)
def _destroy_hosts():
    """Tear every dialog down inside the test to avoid Qt shutdown segfaults."""
    yield
    while _live_hosts:
        host = _live_hosts.pop()
        host.close()
        shiboken6.delete(host)
    QApplication.processEvents()


@pytest.fixture(autouse=True)
def _quiet_settings_init(monkeypatch):
    """No magnet shell-out and no real interface enumeration during __init__."""
    monkeypatch.setattr(dialogs, "_run_magnet_probe", lambda fn, on_done: None)
    monkeypatch.setattr(dialogs, "list_interfaces", lambda: [])


def _rec(id=ID_A, enabled=True, name="A", url=ENDPOINT_A, api_key=""):
    return CustomTorznabIndexer(
        id=id, enabled=enabled, name=name, url=url, api_key=api_key
    )


def _settings_with(*records):
    settings = config.Settings()
    settings.custom_indexers = list(records)
    return settings


def _dialog(records=(), monkeypatch=None):
    """A SettingsDialog on an isolated Settings object with save neutralized."""
    settings = _settings_with(*records)
    if monkeypatch is not None:
        monkeypatch.setattr(type(settings), "save", lambda self: None)
    dlg = SettingsDialog(settings, None)
    _live_hosts.append(dlg)
    return settings, dlg


def _table_texts(dlg):
    texts = []
    for r in range(dlg.indexer_table.rowCount()):
        for c in range(dlg.indexer_table.columnCount()):
            item = dlg.indexer_table.item(r, c)
            if item is not None:
                texts.append(item.text())
    return texts


def _settle():
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()
    QApplication.processEvents()


# --- deterministic fake editor for SettingsDialog add/edit wiring -----------


class _FakeEditor:
    """Stands in for IndexerEditorDialog inside SettingsDialog tests.

    Records the record/interface it was handed and returns a configurable
    result, so SettingsDialog add/edit/remove/order wiring is tested without a
    modal exec() loop. The real editor is covered directly below.
    """

    outcome = "accept"
    result_factory = None
    created = []

    def __init__(self, indexer, *, interface="", is_new=False, parent=None):
        self.indexer = indexer
        self.interface = interface
        self.is_new = is_new
        _FakeEditor.created.append(self)

    def exec(self):
        return QDialog.Accepted if _FakeEditor.outcome == "accept" else QDialog.Rejected

    def result(self):
        if _FakeEditor.outcome != "accept":
            return None
        if _FakeEditor.result_factory is not None:
            return _FakeEditor.result_factory(self.indexer)
        return self.indexer


@pytest.fixture(autouse=True)
def _reset_fake_editor():
    _FakeEditor.outcome = "accept"
    _FakeEditor.result_factory = None
    _FakeEditor.created.clear()
    yield


# --- GROUP 1: existing config appears in UI -----------------------------


def test_existing_records_render_in_persisted_order():
    settings, dlg = _dialog(
        [
            _rec(ID_A, True, "Alpha", ENDPOINT_A, api_key=SECRET),
            _rec(ID_B, False, "Beta", ENDPOINT_B),
            _rec(ID_C, True, "Gamma", ENDPOINT_C),
        ]
    )
    assert dlg.indexer_table.rowCount() == 3
    assert dlg.indexer_table.item(0, 1).text() == "Alpha"
    assert dlg.indexer_table.item(1, 1).text() == "Beta"
    assert dlg.indexer_table.item(2, 1).text() == "Gamma"
    assert dlg.indexer_table.item(0, 2).text() == ENDPOINT_A
    assert dlg.indexer_table.item(1, 2).text() == ENDPOINT_B
    assert dlg.indexer_table.item(2, 2).text() == ENDPOINT_C
    assert dlg.indexer_table.cellWidget(0, 0).isChecked() is True
    assert dlg.indexer_table.cellWidget(1, 0).isChecked() is False
    assert dlg.indexer_table.cellWidget(2, 0).isChecked() is True
    # The secret never appears in the list.
    assert SECRET not in _table_texts(dlg)


def test_endpoint_column_redacts_embedded_credentials():
    query_url = "http://127.0.0.1:9696/torznab/api?apikey=embedded-secret"
    userinfo_url = "http://user:pass@127.0.0.1:9696/torznab/api"
    settings, dlg = _dialog(
        [
            _rec(ID_A, True, "Query", query_url),
            _rec(ID_B, True, "Userinfo", userinfo_url),
        ]
    )
    assert (
        dlg.indexer_table.item(0, 2).text()
        == "http://127.0.0.1:9696/torznab/api?apikey=[redacted]"
    )
    assert (
        dlg.indexer_table.item(1, 2).text()
        == "http://[redacted]@127.0.0.1:9696/torznab/api"
    )
    assert "embedded-secret" not in _table_texts(dlg)
    assert "user:pass" not in _table_texts(dlg)
    # The model retains the unmodified URL for persistence and requests.
    assert dlg._indexer_draft[0].url == query_url
    assert dlg._indexer_draft[1].url == userinfo_url


# --- GROUP 2: empty state ------------------------------------------------


def test_empty_state_is_usable():
    settings, dlg = _dialog([])
    assert dlg.indexer_table.rowCount() == 0
    assert dlg.indexer_add_btn.isEnabled() is True
    assert dlg.indexer_edit_btn.isEnabled() is False
    assert dlg.indexer_remove_btn.isEnabled() is False


# --- GROUP 3 / GROUP 5: add + add cancel (wiring) ------------------------


def test_add_appends_a_new_record_with_a_minted_id(monkeypatch):
    monkeypatch.setattr(dialogs, "IndexerEditorDialog", _FakeEditor)
    settings, dlg = _dialog([], monkeypatch)
    dlg._add_indexer()
    assert len(dlg._indexer_draft) == 1
    record = dlg._indexer_draft[0]
    assert record.id.startswith("custom:")
    uuid.UUID(record.id[len("custom:"):])
    assert record.enabled is True
    assert _FakeEditor.created[-1].is_new is True


def test_add_cancel_leaves_the_draft_unchanged(monkeypatch):
    monkeypatch.setattr(dialogs, "IndexerEditorDialog", _FakeEditor)
    _FakeEditor.outcome = "reject"
    settings, dlg = _dialog([_rec(ID_A, name="A")], monkeypatch)
    dlg._add_indexer()
    assert [r.id for r in dlg._indexer_draft] == [ID_A]
    assert settings.custom_indexers[0].name == "A"


# --- GROUP 6 / GROUP 7: edit id + order, edit cancel ---------------------


def test_edit_passes_the_selected_record_and_preserves_order(monkeypatch):
    monkeypatch.setattr(dialogs, "IndexerEditorDialog", _FakeEditor)
    settings, dlg = _dialog(
        [_rec(ID_A, name="A"), _rec(ID_B, name="B"), _rec(ID_C, name="C")],
        monkeypatch,
    )
    dlg.indexer_table.setCurrentCell(1, 0)
    dlg._edit_indexer()
    assert _FakeEditor.created[-1].indexer.id == ID_B
    assert _FakeEditor.created[-1].is_new is False
    assert [r.id for r in dlg._indexer_draft] == [ID_A, ID_B, ID_C]


def test_edit_replacements_stay_in_position(monkeypatch):
    monkeypatch.setattr(dialogs, "IndexerEditorDialog", _FakeEditor)
    settings, dlg = _dialog(
        [_rec(ID_A, name="A"), _rec(ID_B, name="B"), _rec(ID_C, name="C")],
        monkeypatch,
    )
    _FakeEditor.result_factory = lambda idx: _rec(idx.id, name="B-edited", url=idx.url)
    dlg.indexer_table.setCurrentCell(1, 0)
    dlg._edit_indexer()
    assert [r.name for r in dlg._indexer_draft] == ["A", "B-edited", "C"]
    assert [r.id for r in dlg._indexer_draft] == [ID_A, ID_B, ID_C]


def test_edit_cancel_leaves_the_draft_unchanged(monkeypatch):
    monkeypatch.setattr(dialogs, "IndexerEditorDialog", _FakeEditor)
    _FakeEditor.outcome = "reject"
    settings, dlg = _dialog(
        [_rec(ID_A, name="A"), _rec(ID_B, name="B", api_key=SECRET)], monkeypatch
    )
    dlg.indexer_table.setCurrentCell(1, 0)
    dlg._edit_indexer()
    assert dlg._indexer_draft[1].name == "B"
    assert dlg._indexer_draft[1].api_key == SECRET


# --- GROUP 10: enable toggle ---------------------------------------------


def test_enable_toggle_preserves_identity_and_position():
    settings, dlg = _dialog(
        [
            _rec(ID_A, name="A"),
            _rec(ID_B, name="B", url=ENDPOINT_B, enabled=False, api_key=SECRET),
            _rec(ID_C, name="C", url=ENDPOINT_C),
        ]
    )
    dlg.indexer_table.cellWidget(1, 0).setChecked(True)
    record = dlg._indexer_draft[1]
    assert record.enabled is True
    assert record.id == ID_B
    assert record.name == "B"
    assert record.url == ENDPOINT_B
    assert record.api_key == SECRET
    assert [r.id for r in dlg._indexer_draft] == [ID_A, ID_B, ID_C]


# --- GROUP 11: remove ----------------------------------------------------


def test_remove_deletes_only_the_selected_record(monkeypatch):
    settings, dlg = _dialog(
        [_rec(ID_A, name="A"), _rec(ID_B, name="B"), _rec(ID_C, name="C")],
        monkeypatch,
    )
    dlg.indexer_table.setCurrentCell(1, 0)
    dlg._remove_indexer()
    assert [r.id for r in dlg._indexer_draft] == [ID_A, ID_C]
    assert [r.name for r in dlg._indexer_draft] == ["A", "C"]


# --- GROUP 12: settings cancel transaction -------------------------------


def test_settings_cancel_discards_all_draft_operations(monkeypatch):
    monkeypatch.setattr(dialogs, "IndexerEditorDialog", _FakeEditor)
    settings, dlg = _dialog(
        [_rec(ID_A, name="A", api_key="key-a"), _rec(ID_B, name="B", api_key="key-b")],
        monkeypatch,
    )
    # add C, toggle A off, remove B
    _FakeEditor.result_factory = lambda idx: _rec(ID_C, name="C", url=ENDPOINT_C)
    dlg._add_indexer()
    dlg.indexer_table.cellWidget(0, 0).setChecked(False)
    dlg.indexer_table.setCurrentCell(1, 0)
    dlg._remove_indexer()
    # Cancel means no _on_accept: the live Settings object is untouched.
    assert [r.id for r in settings.custom_indexers] == [ID_A, ID_B]
    assert settings.custom_indexers[0].name == "A"
    assert settings.custom_indexers[0].enabled is True
    assert settings.custom_indexers[0].api_key == "key-a"
    assert settings.custom_indexers[1].api_key == "key-b"


# --- GROUP 13 / GROUP 24: accept + roundtrip, wiring boundary ------------


def _isolated_config(tmp_path, monkeypatch):
    config_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return config_file


def test_accept_updates_canonical_settings(monkeypatch):
    monkeypatch.setattr(dialogs, "IndexerEditorDialog", _FakeEditor)
    settings, dlg = _dialog([_rec(ID_A, name="A")], monkeypatch)
    _FakeEditor.result_factory = lambda idx: _rec(ID_B, name="B")
    dlg._add_indexer()
    dlg._on_accept()
    assert [r.id for r in settings.custom_indexers] == [ID_A, ID_B]


def test_accept_roundtrips_through_the_real_save_path(tmp_path, monkeypatch):
    config_file = _isolated_config(tmp_path, monkeypatch)
    settings = _settings_with(
        _rec(ID_A, name="A", api_key="key-a"),
        _rec(ID_B, name="B", enabled=False, api_key="key-b"),
    )
    dlg = SettingsDialog(settings, None)
    _live_hosts.append(dlg)
    dlg._on_accept()
    reloaded = config.Settings.load()
    assert [r.id for r in reloaded.custom_indexers] == [ID_A, ID_B]
    assert [r.name for r in reloaded.custom_indexers] == ["A", "B"]
    assert reloaded.custom_indexers[0].api_key == "key-a"
    assert reloaded.custom_indexers[1].api_key == "key-b"
    assert reloaded.custom_indexers[1].enabled is False


def test_removed_record_secret_is_absent_from_canonical_config(tmp_path, monkeypatch):
    config_file = _isolated_config(tmp_path, monkeypatch)
    settings = _settings_with(
        _rec(ID_A, name="A", api_key="key-a"),
        _rec(ID_B, name="B", api_key=SECRET),
    )
    dlg = SettingsDialog(settings, None)
    _live_hosts.append(dlg)
    dlg.indexer_table.setCurrentCell(1, 0)
    dlg._remove_indexer()
    dlg._on_accept()
    raw = config_file.read_text()
    assert SECRET not in raw
    reloaded = config.Settings.load()
    assert [r.id for r in reloaded.custom_indexers] == [ID_A]


# --- GROUP 23: order through the UI --------------------------------------


def test_record_order_through_ui_operations(monkeypatch):
    monkeypatch.setattr(dialogs, "IndexerEditorDialog", _FakeEditor)
    settings, dlg = _dialog(
        [_rec(ID_A, name="A"), _rec(ID_B, name="B"), _rec(ID_C, name="C")],
        monkeypatch,
    )
    # edit B (identity) -> A,B,C
    dlg.indexer_table.setCurrentCell(1, 0)
    dlg._edit_indexer()
    # toggle A off -> still A,B,C
    dlg.indexer_table.cellWidget(0, 0).setChecked(False)
    # remove C -> A,B
    dlg.indexer_table.setCurrentCell(2, 0)
    dlg._remove_indexer()
    # add D -> A,B,D
    _FakeEditor.result_factory = lambda idx: _rec(ID_D, name="D", url=ENDPOINT_C)
    dlg._add_indexer()
    assert [r.id for r in dlg._indexer_draft] == [ID_A, ID_B, ID_D]


# --- editor: GROUP 3 add, GROUP 4 validation -----------------------------


def _make_editor(indexer=None, *, is_new=False):
    if indexer is None:
        indexer = CustomTorznabIndexer(
            id=new_custom_indexer_id() if is_new else ID_B,
            enabled=True,
            name="" if is_new else "B",
            url="" if is_new else ENDPOINT_B,
            api_key="",
        )
    editor = IndexerEditorDialog(indexer, interface="", is_new=is_new, parent=None)
    _live_hosts.append(editor)
    return editor


def test_editor_add_builds_a_record_with_the_minted_id():
    editor = _make_editor(is_new=True)
    editor.name_edit.setText("Local Torznab")
    editor.url_edit.setText(ENDPOINT_A)
    editor.api_key_edit.setText("secret-add-test")
    editor._on_accept()
    record = editor.result()
    assert record is not None
    assert record.id.startswith("custom:")
    assert record.enabled is True
    assert record.name == "Local Torznab"
    assert record.url == ENDPOINT_A
    assert record.api_key == "secret-add-test"


def test_editor_blank_name_blocks_save():
    editor = _make_editor(is_new=True)
    editor.url_edit.setText(ENDPOINT_A)
    editor._on_accept()
    assert editor.result() is None
    assert not editor.validation_label.isHidden()
    assert editor.validation_label.text() == "Name is required."


def test_editor_blank_url_blocks_save():
    editor = _make_editor(is_new=True)
    editor.name_edit.setText("Name")
    editor._on_accept()
    assert editor.result() is None
    assert editor.validation_label.text() == "Endpoint URL is required."


def test_editor_overlong_values_delegate_to_s2(monkeypatch):
    from cove.search.indexers import MAX_NAME_LENGTH

    editor = _make_editor(is_new=True)
    editor.name_edit.setText("x" * (MAX_NAME_LENGTH + 1))
    editor.url_edit.setText(ENDPOINT_A)
    editor._on_accept()
    assert editor.result() is None
    assert "valid" in editor.validation_label.text().lower()


def test_editor_overlong_api_key_delegates_to_s2(monkeypatch):
    from cove.search.indexers import MAX_API_KEY_LENGTH

    editor = _make_editor(is_new=True)
    editor.name_edit.setText("Name")
    editor.url_edit.setText(ENDPOINT_A)
    editor.api_key_edit.setText("k" * (MAX_API_KEY_LENGTH + 1))
    editor._on_accept()
    assert editor.result() is None


# --- editor: GROUP 6/7 edit id + cancel ----------------------------------


def test_editor_edit_preserves_the_stable_id():
    editor = _make_editor(_rec(ID_B, name="B", url=ENDPOINT_B, api_key="old"))
    editor.name_edit.setText("Renamed")
    editor.url_edit.setText(ENDPOINT_C)
    editor.api_key_edit.setText("new-key")
    editor.enabled_check.setChecked(False)
    editor._on_accept()
    assert editor.result().id == ID_B
    assert editor.result().name == "Renamed"
    assert editor.result().url == ENDPOINT_C
    assert editor.result().api_key == "new-key"
    assert editor.result().enabled is False


def test_editor_edit_cancel_preserves_the_original_secret():
    original = _rec(ID_B, name="B", url=ENDPOINT_B, api_key=SECRET)
    editor = _make_editor(original)
    editor.name_edit.setText("Changed")
    editor.url_edit.setText(ENDPOINT_C)
    editor.api_key_edit.setText("other-secret")
    editor.reject()
    assert editor.result() is None
    assert original.api_key == SECRET
    assert original.name == "B"


# --- editor: GROUP 8/9 secret masking + clearing -------------------------


def test_api_key_field_is_masked():
    editor = _make_editor(_rec(ID_B, name="B", url=ENDPOINT_B, api_key=SECRET))
    assert editor.api_key_edit.echoMode() == QLineEdit.Password


def test_api_key_prepopulates_the_real_secret_not_a_mask_literal():
    editor = _make_editor(_rec(ID_B, name="B", url=ENDPOINT_B, api_key=SECRET))
    assert editor.api_key_edit.text() == SECRET
    assert editor.api_key_edit.text() != "********"


def test_api_key_untouched_roundtrips_the_real_secret():
    editor = _make_editor(_rec(ID_B, name="B", url=ENDPOINT_B, api_key=SECRET))
    editor.name_edit.setText("Renamed")
    editor._on_accept()
    assert editor.result().api_key == SECRET


def test_api_key_can_be_cleared():
    editor = _make_editor(_rec(ID_B, name="B", url=ENDPOINT_B, api_key=SECRET))
    editor.api_key_edit.setText("")
    editor._on_accept()
    assert editor.result().api_key == ""
    assert editor.result().id == ID_B


# --- editor: Test Connection ---------------------------------------------


def _patch_source(monkeypatch, factory):
    monkeypatch.setattr("cove.indexer_editor.TorznabSource", factory)


def test_test_connection_uses_unsaved_add_values(monkeypatch):
    received = {}

    class FakeSource:
        def __init__(self, indexer):
            received["indexer"] = indexer

        def probe_caps(self, interface):
            received["interface"] = interface
            return object()

    _patch_source(monkeypatch, FakeSource)
    editor = _make_editor(is_new=True)
    editor.name_edit.setText("Draft name")
    editor.url_edit.setText(ENDPOINT_A)
    editor.api_key_edit.setText("draft-key")
    editor._on_test()
    _settle()
    assert received["indexer"].name == "Draft name"
    assert received["indexer"].url == ENDPOINT_A
    assert received["indexer"].api_key == "draft-key"
    assert editor.result_label.text() == "Connection successful."


def test_test_connection_uses_unsaved_edit_values_and_persists_nothing(monkeypatch):
    received = {}

    class FakeSource:
        def __init__(self, indexer):
            received["indexer"] = indexer

        def probe_caps(self, interface):
            return object()

    _patch_source(monkeypatch, FakeSource)
    original = _rec(ID_B, name="B", url=ENDPOINT_B, api_key="old-key")
    editor = _make_editor(original)
    editor.url_edit.setText(ENDPOINT_C)
    editor.api_key_edit.setText("edited-key")
    editor._on_test()
    _settle()
    assert received["indexer"].url == ENDPOINT_C
    assert received["indexer"].api_key == "edited-key"
    assert received["indexer"].id == ID_B
    # The stored record is untouched: no persistence happened.
    assert original.url == ENDPOINT_B
    assert original.api_key == "old-key"


def test_test_connection_does_not_set_the_draft_result(monkeypatch):
    # Test Connection must not persist the draft: only an explicit Save sets
    # result(), so a later Cancel never leaks a Test-triggered record.
    class FakeSource:
        def __init__(self, indexer):
            pass

        def probe_caps(self, interface):
            return object()

    _patch_source(monkeypatch, FakeSource)
    editor = _make_editor(_rec(ID_B, name="B", url=ENDPOINT_B, api_key="old-key"))
    editor._on_test()
    _settle()
    assert editor.result() is None


def test_test_connection_blocks_duplicate_probes(monkeypatch):
    release = threading.Event()
    started = threading.Event()
    launches = []
    original_launch = IndexerEditorDialog._launch_probe

    def counting_launch(self, candidate):
        launches.append(1)
        return original_launch(self, candidate)

    monkeypatch.setattr(IndexerEditorDialog, "_launch_probe", counting_launch)

    class FakeSource:
        def __init__(self, indexer):
            pass

        def probe_caps(self, interface):
            started.set()
            release.wait(timeout=5)
            return object()

    _patch_source(monkeypatch, FakeSource)
    editor = _make_editor(_rec(ID_B, name="B", url=ENDPOINT_B))
    editor._on_test()
    started.wait(timeout=5)
    editor._on_test()  # second click while in flight must be ignored
    assert len(launches) == 1
    release.set()
    _settle()


def test_test_connection_is_non_blocking(monkeypatch):
    release = threading.Event()
    started = threading.Event()

    class FakeSource:
        def __init__(self, indexer):
            pass

        def probe_caps(self, interface):
            started.set()
            release.wait(timeout=5)
            return object()

    _patch_source(monkeypatch, FakeSource)
    editor = _make_editor(_rec(ID_B, name="B", url=ENDPOINT_B))
    editor._on_test()
    started.wait(timeout=5)
    assert editor.test_button.isEnabled() is False
    assert editor._test_inflight is True
    release.set()
    _settle()
    assert editor.test_button.isEnabled() is True
    assert editor._test_inflight is False
    assert editor.result_label.text() == "Connection successful."


def test_test_connection_validation_blocks_the_probe(monkeypatch):
    launches = []

    class FakeSource:
        def __init__(self, indexer):
            pass

        def probe_caps(self, interface):
            launches.append(1)
            return object()

    _patch_source(monkeypatch, FakeSource)
    editor = _make_editor(is_new=True)
    editor.url_edit.setText(ENDPOINT_A)  # no name
    editor._on_test()
    assert launches == []
    assert editor.validation_label.text() == "Name is required."


def test_test_connection_failure_shows_a_safe_message(monkeypatch):
    class FakeSource:
        def __init__(self, indexer):
            pass

        def probe_caps(self, interface):
            raise SourceError(SourceErrorKind.HTTP, "Torznab authentication failed")

    _patch_source(monkeypatch, FakeSource)
    editor = _make_editor(_rec(ID_B, name="B", url=ENDPOINT_B, api_key=SECRET))
    editor._on_test()
    _settle()
    assert editor.result_label.text() == "Torznab authentication failed"
    assert editor.test_button.isEnabled() is True


def test_test_connection_raw_exception_is_sanitized(monkeypatch):
    class FakeSource:
        def __init__(self, indexer):
            pass

        def probe_caps(self, interface):
            raise RuntimeError(f"failed for http://x?apikey={SECRET}")

    _patch_source(monkeypatch, FakeSource)
    editor = _make_editor(_rec(ID_B, name="B", url=ENDPOINT_B, api_key=SECRET))
    editor._on_test()
    _settle()
    assert editor.result_label.text() == "The connection test could not be completed."
    assert SECRET not in editor.result_label.text()


def test_test_connection_secret_never_reaches_the_ui(monkeypatch):
    received = {}

    class FakeSource:
        def __init__(self, indexer):
            received["api_key"] = indexer.api_key

        def probe_caps(self, interface):
            raise SourceError(SourceErrorKind.HTTP, "Torznab authentication failed")

    _patch_source(monkeypatch, FakeSource)
    editor = _make_editor(_rec(ID_B, name="B", url=ENDPOINT_B, api_key=SECRET))
    editor._on_test()
    _settle()
    assert received["api_key"] == SECRET  # the backend actually saw the secret
    assert SECRET not in editor.result_label.text()
    assert SECRET not in editor.validation_label.text()
    assert SECRET not in editor.windowTitle()


def test_editor_closed_during_test_does_not_crash(monkeypatch):
    release = threading.Event()
    started = threading.Event()

    class FakeSource:
        def __init__(self, indexer):
            pass

        def probe_caps(self, interface):
            started.set()
            release.wait(timeout=5)
            return object()

    _patch_source(monkeypatch, FakeSource)
    editor = _make_editor(_rec(ID_B, name="B", url=ENDPOINT_B))
    editor._on_test()
    started.wait(timeout=5)
    # The editor is destroyed while the probe is still in flight.
    _live_hosts.remove(editor)
    editor.close()
    shiboken6.delete(editor)
    release.set()
    _settle()  # the completed probe must not touch the deleted editor
