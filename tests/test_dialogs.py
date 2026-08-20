"""Layout regressions for Cove dialogs."""

import json
import os
from pathlib import Path
import subprocess
import sys


# ---------------------------------------------------------------------------
# Download File Info preflight setting
# ---------------------------------------------------------------------------


def test_settings_dialog_show_download_options_checkbox(tmp_path):
    """Toggle + Save persists; toggle + Cancel does not.

    The checkbox is a draft control like every other Settings row: it starts
    from the settings value, and only _on_accept copies it onto the shared
    Settings object and saves.
    """
    script = r'''
import json, sys
from pathlib import Path
from PySide6.QtWidgets import QApplication

import cove.config as config
tmp = Path(sys.argv[1])
config.CONFIG_DIR = tmp
config.DATA_DIR = tmp
config.CONFIG_FILE = tmp / "settings.json"

from cove.config import Settings
from cove.dialogs import SettingsDialog

app = QApplication([])
out = {}

# Loads on by default.
settings = Settings()
dialog = SettingsDialog(settings)
out["label"] = dialog.show_download_options_check.text()
out["default_checked"] = dialog.show_download_options_check.isChecked()
# Toggle off then Cancel: nothing persists.
dialog.show_download_options_check.setChecked(False)
dialog.reject()
out["after_cancel"] = settings.show_download_options
out["file_after_cancel"] = config.CONFIG_FILE.exists()

# Toggle off then Save: persists.
settings2 = Settings()
dialog2 = SettingsDialog(settings2)
dialog2.show_download_options_check.setChecked(False)
dialog2._on_accept()
out["after_save"] = settings2.show_download_options
raw = json.loads(config.CONFIG_FILE.read_text())
out["persisted_false"] = raw.get("show_download_options")

# Toggle back on from a dismissed state and Save: restores.
settings3 = Settings(show_download_options=False)
dialog3 = SettingsDialog(settings3)
out["restored_from_false"] = dialog3.show_download_options_check.isChecked()
dialog3.show_download_options_check.setChecked(True)
dialog3._on_accept()
out["re_enabled"] = settings3.show_download_options
raw3 = json.loads(config.CONFIG_FILE.read_text())
out["persisted_true"] = raw3.get("show_download_options")

print(json.dumps(out))
'''
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )
    out = json.loads(result.stdout)

    assert out["label"] == "Show download options before starting downloads"
    assert out["default_checked"] is True
    # Cancel never persists, and never even writes the settings file.
    assert out["after_cancel"] is True
    assert out["file_after_cancel"] is False
    # Save persists the explicit False through the existing transaction.
    assert out["after_save"] is False
    assert out["persisted_false"] is False
    # A previously dismissed preference restores through Settings.
    assert out["restored_from_false"] is False
    assert out["re_enabled"] is True
    assert out["persisted_true"] is True


def test_settings_dialog_can_shrink_vertically_and_scroll():
    # Other test modules use QCoreApplication. Qt cannot upgrade that singleton
    # to QApplication later, so exercise the real widget in an isolated process.
    script = r'''
import json
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from cove.config import Settings
from cove.dialogs import SettingsDialog

app = QApplication([])
dialog = SettingsDialog(Settings(
    overall_speed_limit_kbps=1536,
    speed_limiter_enabled=True,
    speed_limit_unit="MB/s",
))
dialog.resize(dialog.width(), 360)
dialog.show()
app.processEvents()
initial_speed_value = dialog.speed_limit.value()
dialog.speed_limit.setValue(2.5)
dialog.speed_unit.setCurrentText("KB/s")
app.processEvents()
print(json.dumps({
    "height": dialog.height(),
    "minimum_height": dialog.minimumSizeHint().height(),
    "scroll_policy": dialog.settings_scroll.verticalScrollBarPolicy().value,
    "expected_policy": Qt.ScrollBarAsNeeded.value,
    "scroll_maximum": dialog.settings_scroll.verticalScrollBar().maximum(),
    "outer_layout_count": dialog.layout().count(),
    "scroll_in_outer_layout": dialog.layout().indexOf(dialog.settings_scroll) >= 0,
    "initial_speed_value": initial_speed_value,
    "converted_speed_value": dialog.speed_limit.value(),
    "speed_units": [dialog.speed_unit.itemText(i) for i in range(dialog.speed_unit.count())],
    "speed_enabled_text": dialog.speed_enabled.text(),
    "speed_enabled": dialog.speed_enabled.isChecked(),
}))
dialog.close()
'''
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=True,
    )
    metrics = json.loads(result.stdout)

    assert metrics["height"] == 360
    assert metrics["minimum_height"] < 360
    assert metrics["scroll_policy"] == metrics["expected_policy"]
    assert metrics["scroll_maximum"] > 0
    # Save/Cancel lives outside the scrolling viewport and therefore stays
    # reachable even when the form is scrolled on a short display.
    assert metrics["scroll_in_outer_layout"] is True
    assert metrics["outer_layout_count"] == 4
    assert metrics["initial_speed_value"] == 1.5
    assert metrics["converted_speed_value"] == 2560
    assert metrics["speed_units"] == ["KB/s", "MB/s"]
    assert metrics["speed_enabled_text"] == "Enable speed limiter"
    assert metrics["speed_enabled"] is True


DEBRID_SCRIPT = r'''
import json, sys
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QApplication

import cove.config as config
tmp = sys.argv[1]
config.CONFIG_DIR = __import__("pathlib").Path(tmp)
config.DATA_DIR = __import__("pathlib").Path(tmp)
config.CONFIG_FILE = __import__("pathlib").Path(tmp) / "settings.json"

from cove import debrid
from cove.config import Settings
from cove.debrid import ALL_DEBRID, REAL_DEBRID, DebridError
from cove.dialogs import SettingsDialog

# This script only exercises AD/RD controls, so force TorBox off regardless
# of the shipped module default.
debrid.TORBOX_FEATURE_AVAILABLE = False

# Any real network call is a test failure, not a slow test.
def _no_network(*a, **k):
    raise AssertionError("live provider request attempted")
for name in ("get", "post", "head", "put", "request"):
    setattr(debrid.requests, name, _no_network)

AD_KEY = "ad-key-SECRETVALUE"
RD_TOKEN = "rd-token-SECRETVALUE"

app = QApplication([])
out = {}

# ---- load ---------------------------------------------------------------
loaded = SettingsDialog(Settings(
    all_debrid_enabled=True,
    all_debrid_api_key=AD_KEY,
    real_debrid_enabled=False,
    real_debrid_api_token=RD_TOKEN,
    debrid_preferred_provider="real_debrid",
))
out["ad_enabled"] = loaded.ad_enabled.isChecked()
out["ad_key"] = loaded.ad_key.text()
out["ad_masked"] = loaded.ad_key.echoMode() == loaded.ad_key.EchoMode.Password
out["rd_enabled"] = loaded.rd_enabled.isChecked()
out["rd_token"] = loaded.rd_token.text()
out["rd_masked"] = loaded.rd_token.echoMode() == loaded.rd_token.EchoMode.Password
out["preferred"] = loaded.debrid_preferred.currentData()
out["preferred_options"] = [
    loaded.debrid_preferred.itemData(i) for i in range(loaded.debrid_preferred.count())
]
loaded.close()

# ---- save ---------------------------------------------------------------
settings = Settings()
dialog = SettingsDialog(settings)
dialog.ad_enabled.setChecked(True)
dialog.ad_key.setText("  " + AD_KEY + "  ")
dialog.rd_enabled.setChecked(True)
dialog.rd_token.setText(RD_TOKEN)
dialog.debrid_preferred.setCurrentIndex(
    dialog.debrid_preferred.findData("real_debrid"))
dialog._on_accept()
out["saved"] = {
    "ad_enabled": settings.all_debrid_enabled,
    "ad_key": settings.all_debrid_api_key,
    "rd_enabled": settings.real_debrid_enabled,
    "rd_token": settings.real_debrid_api_token,
    "preferred": settings.debrid_preferred_provider,
}

# Providers are independent: enabling only Real-Debrid leaves AllDebrid off.
only_rd_settings = Settings()
only_rd = SettingsDialog(only_rd_settings)
only_rd.rd_enabled.setChecked(True)
only_rd.rd_token.setText(RD_TOKEN)
only_rd._on_accept()
out["only_rd"] = {
    "ad_enabled": only_rd_settings.all_debrid_enabled,
    "ad_key": only_rd_settings.all_debrid_api_key,
    "rd_enabled": only_rd_settings.real_debrid_enabled,
    "preferred": only_rd_settings.debrid_preferred_provider,
}

# ---- account test: success ----------------------------------------------
def _drain():
    for _ in range(100):
        app.processEvents()
        if QThreadPool.globalInstance().waitForDone(200):
            break
    app.processEvents()
    app.processEvents()

debrid.all_debrid_account = lambda key, **kw: {
    "username": "coveuser", "is_premium": True, "is_trial": False,
    "premium_until": 1800000000,
}
tester = SettingsDialog(Settings(all_debrid_enabled=True, all_debrid_api_key=AD_KEY))
tester.ad_test.click()
out["ad_button_disabled_during_test"] = not tester.ad_test.isEnabled()
_drain()
out["ad_success_text"] = tester.ad_result.text()
out["ad_button_restored"] = tester.ad_test.isEnabled()

# ---- account test: failure ----------------------------------------------
def _bad_key(key, **kw):
    raise DebridError(ALL_DEBRID, "AUTH_BAD_APIKEY",
                      "the API key was rejected. Check the key in Settings.")
debrid.all_debrid_account = _bad_key
tester.ad_test.click()
_drain()
out["ad_failure_text"] = tester.ad_result.text()
out["ad_button_restored_after_failure"] = tester.ad_test.isEnabled()

# ---- Real-Debrid test is independent ------------------------------------
debrid.real_debrid_account = lambda token, **kw: {
    "username": "rduser", "type": "premium", "expiration": "2027-01-01T00:00:00.000Z",
}
rd_tester = SettingsDialog(Settings(real_debrid_enabled=True, real_debrid_api_token=RD_TOKEN))
rd_tester.rd_test.click()
_drain()
out["rd_success_text"] = rd_tester.rd_result.text()
out["ad_result_untouched"] = rd_tester.ad_result.text()

# ---- an unexpected provider crash is still sanitized --------------------
def _explode(token, **kw):
    raise RuntimeError("boom with token " + RD_TOKEN)
debrid.real_debrid_account = _explode
rd_tester.rd_test.click()
_drain()
out["rd_crash_text"] = rd_tester.rd_result.text()

# ---- provider-controlled account text is never rendered as markup -------
debrid.all_debrid_account = lambda key, **kw: {
    "username": "<b>admin</b><img src=x>", "is_premium": True, "is_trial": False,
}
spoof = SettingsDialog(Settings(all_debrid_enabled=True, all_debrid_api_key=AD_KEY))
spoof.ad_test.click()
_drain()
out["spoof_text"] = spoof.ad_result.text()
out["ad_result_plain"] = spoof.ad_result.textFormat() == Qt.PlainText
out["rd_result_plain"] = spoof.rd_result.textFormat() == Qt.PlainText

tester.close()
rd_tester.close()
spoof.close()
print(json.dumps(out))
'''


def _run_debrid_dialog_script(tmp_path):
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", DEBRID_SCRIPT, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr[-4000:])
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_debrid_settings_controls_load_save_and_test(tmp_path):
    m = _run_debrid_dialog_script(tmp_path)

    # Controls reflect the settings they were built from.
    assert m["ad_enabled"] is True
    assert m["ad_key"] == "ad-key-SECRETVALUE"
    assert m["ad_masked"] is True
    assert m["rd_enabled"] is False
    assert m["rd_token"] == "rd-token-SECRETVALUE"
    assert m["rd_masked"] is True
    assert m["preferred"] == "real_debrid"
    assert m["preferred_options"] == ["alldebrid", "real_debrid"]

    # Save writes every field back, trimming stray whitespace on the key.
    assert m["saved"] == {
        "ad_enabled": True,
        "ad_key": "ad-key-SECRETVALUE",
        "rd_enabled": True,
        "rd_token": "rd-token-SECRETVALUE",
        "preferred": "real_debrid",
    }

    # Each provider is configurable on its own.
    assert m["only_rd"]["rd_enabled"] is True
    assert m["only_rd"]["ad_enabled"] is False
    assert m["only_rd"]["ad_key"] == ""
    assert m["only_rd"]["preferred"] == "alldebrid"


def test_debrid_account_test_reports_sanitized_results(tmp_path):
    m = _run_debrid_dialog_script(tmp_path)

    assert m["ad_button_disabled_during_test"] is True
    assert m["ad_button_restored"] is True
    assert "coveuser" in m["ad_success_text"]
    assert "Premium" in m["ad_success_text"]

    assert "AllDebrid" in m["ad_failure_text"]
    assert "API key was rejected" in m["ad_failure_text"]
    assert m["ad_button_restored_after_failure"] is True

    # Real-Debrid runs independently and leaves the AllDebrid row alone.
    assert "rduser" in m["rd_success_text"]
    assert "premium" in m["rd_success_text"].lower()
    assert m["ad_result_untouched"] == ""

    # An unexpected exception must not leak the credential into the UI.
    assert "SECRETVALUE" not in m["rd_crash_text"]
    assert "RuntimeError" not in m["rd_crash_text"]
    assert m["rd_crash_text"]

    # No credential text appears in any result label.
    for key in ("ad_success_text", "ad_failure_text", "rd_success_text", "rd_crash_text"):
        assert "SECRETVALUE" not in m[key], key


def test_debrid_account_labels_never_render_provider_markup(tmp_path):
    """A provider-controlled username must not be able to style or spoof the
    settings dialog. QLabel defaults to auto-detecting rich text."""
    m = _run_debrid_dialog_script(tmp_path)

    assert m["ad_result_plain"] is True
    assert m["rd_result_plain"] is True
    # The markup survives verbatim as literal text rather than being rendered.
    assert "<b>admin</b><img src=x>" in m["spoof_text"]


# ---------------------------------------------------------------------------
# TorBox (T1: gated behind cove.debrid.TORBOX_FEATURE_AVAILABLE)
# ---------------------------------------------------------------------------

TORBOX_SCRIPT = r'''
import json, sys
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QApplication

import cove.config as config
tmp = sys.argv[1]
config.CONFIG_DIR = __import__("pathlib").Path(tmp)
config.DATA_DIR = __import__("pathlib").Path(tmp)
config.CONFIG_FILE = __import__("pathlib").Path(tmp) / "settings.json"

from cove import debrid
from cove.config import Settings
from cove.debrid import DEBRID_TORBOX, DebridError
from cove.dialogs import SettingsDialog

def _no_network(*a, **k):
    raise AssertionError("live provider request attempted")
for name in ("get", "post", "head", "put", "request"):
    setattr(debrid.requests, name, _no_network)

TB_TOKEN = "torbox-token-SECRETVALUE"

app = QApplication([])
out = {}

def _drain():
    for _ in range(100):
        app.processEvents()
        if QThreadPool.globalInstance().waitForDone(200):
            break
    app.processEvents()
    app.processEvents()

# ---- gate off: controls hidden, no combo entry ----------------------------
debrid.TORBOX_FEATURE_AVAILABLE = False
gated_off = SettingsDialog(Settings())
out["hidden_when_gate_off"] = gated_off.torbox_container.isHidden()
out["combo_options_gate_off"] = [
    gated_off.debrid_preferred.itemData(i)
    for i in range(gated_off.debrid_preferred.count())
]
gated_off.close()

# A stored "torbox" preference survives an unrelated Save while the gate
# is off, instead of being silently reset to AllDebrid.
preexisting = Settings(debrid_preferred_provider="torbox")
gated_off_save = SettingsDialog(preexisting)
gated_off_save.ad_enabled.setChecked(True)
gated_off_save.ad_key.setText("somekey")
gated_off_save._on_accept()
out["preserved_torbox_preference_when_gate_off"] = preexisting.debrid_preferred_provider
gated_off_save.close()

# ---- gate on: everything below runs with TorBox available ----------------
debrid.TORBOX_FEATURE_AVAILABLE = True

loaded = SettingsDialog(Settings(
    torbox_enabled=True, torbox_api_token=TB_TOKEN,
    debrid_preferred_provider="torbox",
))
out["visible_when_gate_on"] = not loaded.torbox_container.isHidden()
out["tb_enabled"] = loaded.torbox_enabled_cb.isChecked()
out["tb_token"] = loaded.torbox_token.text()
out["tb_masked"] = loaded.torbox_token.echoMode() == loaded.torbox_token.EchoMode.Password
out["preferred"] = loaded.debrid_preferred.currentData()
out["combo_options_gate_on"] = [
    loaded.debrid_preferred.itemData(i) for i in range(loaded.debrid_preferred.count())
]
# Existing AD/RD controls are unaffected by TorBox being present.
out["ad_enabled_untouched"] = loaded.ad_enabled.isChecked()
loaded.close()

settings = Settings()
dialog = SettingsDialog(settings)
dialog.torbox_enabled_cb.setChecked(True)
dialog.torbox_token.setText("  " + TB_TOKEN + "  ")
dialog.debrid_preferred.setCurrentIndex(dialog.debrid_preferred.findData(DEBRID_TORBOX))
dialog._on_accept()
out["saved"] = {
    "torbox_enabled": settings.torbox_enabled,
    "torbox_api_token": settings.torbox_api_token,
    "preferred": settings.debrid_preferred_provider,
}

# ---- account test: success ------------------------------------------------
debrid.torbox_account = lambda token, **kw: {
    "email": "user@example.com", "is_subscribed": True,
    "expiration": "2027-01-01T00:00:00.000Z",
}
tester = SettingsDialog(Settings(torbox_enabled=True, torbox_api_token=TB_TOKEN))
tester.torbox_test.click()
out["tb_button_disabled_during_test"] = not tester.torbox_test.isEnabled()
_drain()
out["tb_success_text"] = tester.torbox_result.text()
out["tb_button_restored"] = tester.torbox_test.isEnabled()
out["tb_result_plain"] = tester.torbox_result.textFormat() == Qt.PlainText

# ---- account test: failure -------------------------------------------------
def _bad_token(token, **kw):
    raise DebridError(DEBRID_TORBOX, "auth",
                      "the API token was rejected. Check the token in Settings.")
debrid.torbox_account = _bad_token
tester.torbox_test.click()
_drain()
out["tb_failure_text"] = tester.torbox_result.text()

# ---- an unexpected provider crash is still sanitized -----------------------
def _explode(token, **kw):
    raise RuntimeError("boom with token " + TB_TOKEN)
debrid.torbox_account = _explode
tester.torbox_test.click()
_drain()
out["tb_crash_text"] = tester.torbox_result.text()

tester.close()
print(json.dumps(out))
'''


def _run_torbox_dialog_script(tmp_path):
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", TORBOX_SCRIPT, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr[-4000:])
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_torbox_controls_hidden_and_excluded_from_combo_while_gate_is_off(tmp_path):
    m = _run_torbox_dialog_script(tmp_path)
    assert m["hidden_when_gate_off"] is True
    assert m["combo_options_gate_off"] == ["alldebrid", "real_debrid"]
    assert m["preserved_torbox_preference_when_gate_off"] == "torbox"


def test_torbox_controls_available_when_gate_is_on(tmp_path):
    m = _run_torbox_dialog_script(tmp_path)
    assert m["visible_when_gate_on"] is True
    assert m["tb_enabled"] is True
    assert m["tb_token"] == "torbox-token-SECRETVALUE"
    assert m["tb_masked"] is True
    assert m["preferred"] == "torbox"
    assert m["combo_options_gate_on"] == ["alldebrid", "real_debrid", "torbox"]
    assert m["ad_enabled_untouched"] is False


def test_torbox_checkbox_and_masked_token_load_and_save(tmp_path):
    m = _run_torbox_dialog_script(tmp_path)
    assert m["saved"] == {
        "torbox_enabled": True,
        "torbox_api_token": "torbox-token-SECRETVALUE",
        "preferred": "torbox",
    }


def test_torbox_account_test_reports_sanitized_results(tmp_path):
    m = _run_torbox_dialog_script(tmp_path)
    assert m["tb_button_disabled_during_test"] is True
    assert m["tb_button_restored"] is True
    assert m["tb_result_plain"] is True
    assert "user@example.com" in m["tb_success_text"]
    assert "Subscribed" in m["tb_success_text"]

    assert "TorBox" in m["tb_failure_text"]
    assert "API token was rejected" in m["tb_failure_text"]

    assert "SECRETVALUE" not in m["tb_crash_text"]
    assert "RuntimeError" not in m["tb_crash_text"]
    assert m["tb_crash_text"]

    for key in ("tb_success_text", "tb_failure_text", "tb_crash_text"):
        assert "SECRETVALUE" not in m[key], key


# ---------------------------------------------------------------------------
# Torrent input (hidden unless torrent_support_enabled)
# ---------------------------------------------------------------------------


def _torrent_bytes() -> bytes:
    return (
        b"d4:infod6:lengthi7e4:name9:movie.mkv12:piece lengthi16384e"
        b"6:pieces20:" + b"\x01" * 20 + b"ee"
    )


def test_torrent_file_problem_accepts_a_real_torrent(tmp_path):
    from cove.dialogs import torrent_file_problem

    path = tmp_path / "ok.torrent"
    path.write_bytes(_torrent_bytes())
    assert torrent_file_problem(str(path)) == ""


def test_torrent_file_problem_rejects_other_local_files(tmp_path):
    from cove.dialogs import torrent_file_problem

    other = tmp_path / "notes.txt"
    other.write_text("hello")
    assert torrent_file_problem(str(other)) != ""
    assert torrent_file_problem(str(tmp_path)) != ""
    assert torrent_file_problem(str(tmp_path / "missing.torrent")) != ""
    assert torrent_file_problem(None) != ""


def test_torrent_file_problem_rejects_an_oversized_file(tmp_path):
    from cove.dialogs import torrent_file_problem
    from cove.torrent import MAX_TORRENT_BYTES

    big = tmp_path / "big.torrent"
    big.write_bytes(b"d" * (MAX_TORRENT_BYTES + 1))
    problem = torrent_file_problem(str(big))
    assert "10 MiB" in problem


def test_dropped_torrent_is_accepted_only_while_enabled(tmp_path):
    from cove.main_window import torrent_drop_paths

    good = tmp_path / "ok.torrent"
    good.write_bytes(_torrent_bytes())
    other = tmp_path / "notes.txt"
    other.write_text("hello")
    drops = [str(good), str(other), str(tmp_path), "", str(tmp_path / "gone.torrent")]

    assert torrent_drop_paths(drops, True) == [str(good)]
    # Flag off: a dropped .torrent is ignored exactly like any other local
    # file, so the drop handler behaves as it does at HEAD.
    assert torrent_drop_paths(drops, False) == []


def test_add_download_dialog_torrent_button_follows_the_flag(tmp_path):
    script = r'''
import json, sys
from PySide6.QtWidgets import QApplication

import cove.config as config
from pathlib import Path
tmp = Path(sys.argv[1])
config.CONFIG_DIR = tmp
config.DATA_DIR = tmp
config.CONFIG_FILE = tmp / "settings.json"

from cove.config import Settings
from cove.dialogs import AddDownloadDialog

app = QApplication([])
out = {}
for flag in (False, True):
    dialog = AddDownloadDialog(Settings(
        download_dir=str(tmp), torrent_support_enabled=flag
    ))
    dialog.show()
    app.processEvents()
    out[str(flag)] = {
        "visible": dialog.torrent_button.isVisible(),
        "enabled": dialog.torrent_button.isEnabled(),
        "text": dialog.torrent_button.text(),
        "path": dialog.torrent_path,
    }
    if not flag:
        # A picker the user cannot reach must also refuse to act: no file
        # dialog is opened at all, so this returns without blocking.
        dialog._pick_torrent()
        out[str(flag)]["path_after_direct_call"] = dialog.torrent_path
    dialog.close()
print(json.dumps(out))
'''
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    out = json.loads(result.stdout)

    assert out["False"]["visible"] is False
    assert out["False"]["enabled"] is False
    assert out["True"]["visible"] is True
    assert out["True"]["enabled"] is True
    assert out["True"]["text"] == "Add torrent file..."
    # Nothing is selected until the user picks a file.
    assert out["False"]["path"] == ""
    assert out["True"]["path"] == ""
    assert out["False"]["path_after_direct_call"] == ""


def test_torrent_picker_uses_a_torrent_filter_and_rejects_bad_files(tmp_path):
    script = r'''
import json, sys
from pathlib import Path
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

import cove.config as config
tmp = Path(sys.argv[1])
config.CONFIG_DIR = tmp
config.DATA_DIR = tmp
config.CONFIG_FILE = tmp / "settings.json"

from cove.config import Settings
from cove import dialogs
from cove.dialogs import AddDownloadDialog

app = QApplication([])
out = {"filters": [], "warnings": []}

chosen = {"path": ""}
def fake_open(parent, title, start, filt):
    out["filters"].append(filt)
    return chosen["path"], filt
QFileDialog.getOpenFileName = staticmethod(fake_open)
QMessageBox.warning = staticmethod(
    lambda *a, **k: out["warnings"].append(a[2]) or QMessageBox.Ok
)

dialog = AddDownloadDialog(Settings(download_dir=str(tmp), torrent_support_enabled=True))

# An oversized file is refused before anything is read.
big = tmp / "big.torrent"
big.write_bytes(b"d" * (10 * 1024 * 1024 + 1))
chosen["path"] = str(big)
dialog._pick_torrent()
out["after_big"] = dialog.torrent_path

# A plain file with the wrong extension is refused too.
chosen["path"] = str(tmp / "notes.txt")
(tmp / "notes.txt").write_text("hi")
dialog._pick_torrent()
out["after_txt"] = dialog.torrent_path

# A real one is accepted and closes the dialog.
good = tmp / "ok.torrent"
good.write_bytes(b"d4:infod6:lengthi7e4:name9:movie.mkv12:piece lengthi16384e6:pieces20:" + b"\x01" * 20 + b"ee")
chosen["path"] = str(good)
dialog._pick_torrent()
out["after_good"] = dialog.torrent_path
out["accepted"] = dialog.result() == AddDownloadDialog.Accepted
print(json.dumps(out))
'''
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    out = json.loads(result.stdout)

    assert out["filters"] and all(f == "Torrent files (*.torrent)" for f in out["filters"])
    assert out["after_big"] == ""
    assert out["after_txt"] == ""
    assert out["after_good"].endswith("ok.torrent")
    assert out["accepted"] is True
    assert len(out["warnings"]) == 2
    assert "10 MiB" in out["warnings"][0]


# ---------------------------------------------------------------------------
# BitTorrent settings (Slice B)
# ---------------------------------------------------------------------------


def _load_settings_from(tmp_path, raw: dict):
    """Settings.load() against a throwaway config directory."""
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
print(json.dumps({
    "mode": s.torrent_fallback_mode,
    "allow_with_proxy": s.torrent_allow_with_proxy,
    "disclosure": s.torrent_ip_disclosure_shown,
    "support": s.torrent_support_enabled,
}))
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
    return json.loads(result.stdout)


def test_bittorrent_settings_defaults():
    from cove.config import Settings

    s = Settings()
    assert s.torrent_fallback_mode == "automatic"
    assert s.torrent_allow_with_proxy is False
    assert s.torrent_ip_disclosure_shown is False


def test_bittorrent_settings_round_trip(tmp_path):
    out = _load_settings_from(tmp_path, {
        "torrent_fallback_mode": "never",
        "torrent_allow_with_proxy": True,
        "torrent_ip_disclosure_shown": True,
    })
    assert out == {
        "mode": "never", "allow_with_proxy": True,
        "disclosure": True, "support": False,
    }


def test_invalid_fallback_mode_resets_to_automatic(tmp_path):
    for bad in ("ask", "", "AUTOMATIC ", 7, None, True):
        out = _load_settings_from(tmp_path, {"torrent_fallback_mode": bad})
        assert out["mode"] == "automatic"


def test_invalid_torrent_booleans_reset_safely(tmp_path):
    out = _load_settings_from(tmp_path, {
        "torrent_allow_with_proxy": "yes",
        "torrent_ip_disclosure_shown": 1,
        "torrent_support_enabled": "true",
    })
    assert out["allow_with_proxy"] is False
    assert out["disclosure"] is False
    assert out["support"] is False


def test_settings_dialog_bittorrent_group(tmp_path):
    script = r'''
import json, sys
from pathlib import Path
from PySide6.QtWidgets import QApplication, QGroupBox

import cove.config as config
tmp = Path(sys.argv[1])
config.CONFIG_DIR = tmp
config.DATA_DIR = tmp
config.CONFIG_FILE = tmp / "settings.json"

from cove.config import Settings
from cove.dialogs import SettingsDialog

app = QApplication([])
settings = Settings(
    download_dir=str(tmp),
    torrent_support_enabled=True,
    torrent_fallback_mode="never",
    torrent_allow_with_proxy=True,
    torrent_ip_disclosure_shown=True,
)
dialog = SettingsDialog(settings)
groups = [g.title() for g in dialog.findChildren(QGroupBox)]
notes = " ".join(
    lbl.text() for lbl in dialog.torrent_group.findChildren(type(dialog.proxy_note))
)
out = {
    "groups": groups,
    "loaded_support": dialog.torrent_enabled.isChecked(),
    "loaded_mode": dialog.torrent_fallback.currentData(),
    "loaded_proxy_override": dialog.torrent_proxy_override.isChecked(),
    "modes": [dialog.torrent_fallback.itemData(i)
              for i in range(dialog.torrent_fallback.count())],
    "notes": notes,
    "has_disclosure_checkbox": any(
        "disclosure" in cb.objectName().lower() or "notice" in cb.text().lower()
        for cb in dialog.torrent_group.findChildren(type(dialog.torrent_enabled))
    ),
}

# Saving writes the group back without disturbing the consent flag.
dialog.torrent_enabled.setChecked(False)
dialog.torrent_fallback.setCurrentIndex(dialog.torrent_fallback.findData("automatic"))
dialog.torrent_proxy_override.setChecked(False)
dialog._on_accept()
out["saved_support"] = settings.torrent_support_enabled
out["saved_mode"] = settings.torrent_fallback_mode
out["saved_proxy_override"] = settings.torrent_allow_with_proxy
out["saved_disclosure"] = settings.torrent_ip_disclosure_shown
print(json.dumps(out))
'''
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    out = json.loads(result.stdout)

    assert "BitTorrent" in out["groups"]
    assert "Debrid services" in out["groups"]
    assert out["loaded_support"] is True
    assert out["loaded_mode"] == "never"
    assert out["loaded_proxy_override"] is True
    assert out["modes"] == ["automatic", "never"]
    assert out["saved_support"] is False
    assert out["saved_mode"] == "automatic"
    assert out["saved_proxy_override"] is False
    # Consent is not a normal checkbox and must survive a Save untouched.
    assert out["has_disclosure_checkbox"] is False
    assert out["saved_disclosure"] is True

    notes = out["notes"].lower()
    assert "https" in notes and "swarm" in notes
    assert "ip address" in notes
    assert "stops seeding" in notes or "no seeding" in notes
    assert "proxy" in notes and ("dht" in notes or "peer" in notes)


# ---------------------------------------------------------------------------
# One-time local BitTorrent privacy disclosure
# ---------------------------------------------------------------------------


def test_disclosure_wording_is_honest_about_what_cove_can_promise():
    from cove.main_window import P2P_DISCLOSURE_TEXT, P2P_DISCLOSURE_TITLE

    text = P2P_DISCLOSURE_TEXT.lower()
    assert "not cached" in P2P_DISCLOSURE_TITLE.lower()
    assert "ip address" in text
    assert "peers and trackers" in text
    assert "vpn" in text and "cannot verify" in text
    # Both escape routes are named, so the notice is not a dead end.
    assert "network interface" in text and "cancel" in text
    assert "settings" in text
    # No promise Cove cannot keep.
    for claim in ("anonymous", "anonymity", "kill switch", "fully protected",
                  "you are protected", "encrypted end-to-end"):
        assert claim not in text


def test_bittorrent_tab_exposes_interface_binding_and_cancel_wording(tmp_path):
    script = r'''
import json, sys
from pathlib import Path
from PySide6.QtWidgets import QApplication

import cove.config as config
tmp = Path(sys.argv[1])
config.CONFIG_DIR = tmp
config.DATA_DIR = tmp
config.CONFIG_FILE = tmp / "settings.json"

from cove.config import Settings
import cove.dialogs as dialogs
from cove.dialogs import SettingsDialog

dialogs.list_interfaces = lambda: ["eno1", "wg0-mullvad"]

app = QApplication([])
out = {}

dialog = SettingsDialog(Settings())
out["fallback_labels"] = [
    dialog.torrent_fallback.itemText(i)
    for i in range(dialog.torrent_fallback.count())
]
out["fallback_values"] = [
    dialog.torrent_fallback.itemData(i)
    for i in range(dialog.torrent_fallback.count())
]
out["interface_labels"] = [
    dialog.torrent_interface.itemText(i)
    for i in range(dialog.torrent_interface.count())
]
out["interface_default"] = dialog.torrent_interface.currentData()
# Interface binding covers every aria2 transfer, so it must not be gated
# behind the torrent-support switch.
out["interface_enabled_with_torrents_off"] = dialog.torrent_interface.isEnabled()
dialog.close()

# A saved interface that no longer exists stays selected and is flagged.
stale = SettingsDialog(Settings(torrent_network_interface="tun9"))
out["stale_current"] = stale.torrent_interface.currentData()
out["stale_label"] = stale.torrent_interface.currentText()
stale.close()

settings = Settings()
saver = SettingsDialog(settings)
saver.torrent_interface.setCurrentIndex(
    saver.torrent_interface.findData("wg0-mullvad")
)
saver.torrent_fallback.setCurrentIndex(saver.torrent_fallback.findData("never"))
saver._on_accept()
out["saved_interface"] = settings.torrent_network_interface
out["saved_fallback"] = settings.torrent_fallback_mode

print(json.dumps(out))
'''
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )
    out = json.loads(result.stdout)

    assert out["fallback_labels"] == [
        "Download locally with BitTorrent", "Cancel the download",
    ]
    # Relabelling must not change what is stored.
    assert out["fallback_values"] == ["automatic", "never"]
    assert out["saved_fallback"] == "never"

    assert out["interface_labels"] == ["Any interface", "eno1", "wg0-mullvad"]
    assert out["interface_default"] == ""
    assert out["interface_enabled_with_torrents_off"] is True
    assert out["saved_interface"] == "wg0-mullvad"
    # A vanished interface is never silently downgraded to "Any interface".
    assert out["stale_current"] == "tun9"
    assert "not available" in out["stale_label"]


def test_interface_note_states_that_binding_covers_every_download(tmp_path):
    """One shared aria2 daemon means this is not a torrent-only setting."""
    script = r'''
import json, sys
from pathlib import Path
from PySide6.QtWidgets import QApplication, QLabel

import cove.config as config
tmp = Path(sys.argv[1])
config.CONFIG_DIR = tmp
config.DATA_DIR = tmp
config.CONFIG_FILE = tmp / "settings.json"

from cove.config import Settings
from cove.dialogs import SettingsDialog

app = QApplication([])
dialog = SettingsDialog(Settings())
notes = " ".join(
    lbl.text() for lbl in dialog.torrent_group.findChildren(QLabel)
)
print(json.dumps({"notes": notes.lower()}))
'''
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )
    notes = json.loads(result.stdout)["notes"]

    assert "all downloads" in notes
    assert "restart cove" in notes
    # It must not imply the binding is limited to torrent traffic.
    assert "only torrent" not in notes


def test_metadata_phase_has_its_own_status_label():
    from types import SimpleNamespace

    from cove.main_window import task_status_label

    fetching = SimpleNamespace(status="active", phase="metadata")
    assert task_status_label(fetching) == "Fetching metadata"
    downloading = SimpleNamespace(status="active", phase="")
    assert task_status_label(downloading) == "Downloading"
    # A completed torrent never reads as still fetching.
    done = SimpleNamespace(status="completed", phase="metadata")
    assert task_status_label(done) == "Done"
    # Rows without the attribute (plain HTTP tasks) behave as before.
    assert task_status_label(SimpleNamespace(status="paused")) == "Paused"


def test_consent_modal_runs_on_the_gui_thread_and_answers_the_queue(tmp_path):
    script = r'''
import json, sys
from pathlib import Path
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

import cove.config as config
tmp = Path(sys.argv[1])
config.CONFIG_DIR = tmp
config.DATA_DIR = tmp
config.CONFIG_FILE = tmp / "settings.json"

import cove.main_window as mw

app = QApplication([])
out = {"on_gui_thread": [], "texts": [], "titles": [], "buttons": []}

class FakeQueue:
    def __init__(self):
        self.answers = []
        self.reevaluated = []
    def torrent_consent(self, tid, accepted, remember=False):
        self.answers.append((tid, accepted, remember))
    def torrent_consent_reevaluate(self, tid):
        self.reevaluated.append(tid)

class Host(QWidget):
    def _open_settings(self):
        out["settings_opened"].append(True)

out["settings_opened"] = []
host = Host()
host.queue = FakeQueue()

real_build = mw.build_p2p_consent_box
# "download" | "cancel" | "settings", plus the checkbox state.
choice = {"button": "download", "remember": True}

def build(parent):
    box, download, settings, remember = real_build(parent)
    out["on_gui_thread"].append(QThread.currentThread() is app.thread())
    out["texts"].append(box.text())
    out["titles"].append(box.windowTitle())
    out["buttons"].append([
        (b.text(), int(box.buttonRole(b).value)) for b in box.buttons()
    ])
    out["default_is_cancel"] = box.defaultButton().text() == "Cancel download"
    out["checkbox_text"] = box.checkBox().text()
    remember.setChecked(choice["remember"])
    clicked = {
        "download": download,
        "settings": settings,
    }.get(choice["button"])
    if clicked is None:
        clicked = [b for b in box.buttons() if b.text() == "Cancel download"][0]
    box.exec = lambda: 0
    box.clickedButton = lambda: clicked
    return box, download, settings, remember

mw.build_p2p_consent_box = build

mw.MainWindow._on_torrent_consent_needed(host, 7)
choice["button"] = "cancel"
mw.MainWindow._on_torrent_consent_needed(host, 9)
# Open Settings must park the task and never record consent, even with the
# checkbox ticked.
choice["button"] = "settings"
mw.MainWindow._on_torrent_consent_needed(host, 11)

out["answers"] = host.queue.answers
out["reevaluated"] = host.queue.reevaluated
out["text_matches"] = [t == mw.P2P_DISCLOSURE_TEXT for t in out["texts"]]
out["title_matches"] = [t == mw.P2P_DISCLOSURE_TITLE for t in out["titles"]]
print(json.dumps(out))
'''
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    out = json.loads(result.stdout)

    assert all(out["on_gui_thread"])
    assert all(out["text_matches"])
    assert all(out["title_matches"])
    assert sorted(b[0] for b in out["buttons"][0]) == [
        "Cancel download", "Download locally", "Open Settings",
    ]
    assert out["default_is_cancel"] is True
    assert out["checkbox_text"] == "Don't show this notice again"
    # Cancel carries the ticked checkbox through as remember=True, but the
    # queue only honours it on an accepted download.
    assert out["answers"] == [[7, True, True], [9, False, True]]
    # Open Settings answers nothing and re-evaluates instead.
    assert out["settings_opened"] == [True]
    assert out["reevaluated"] == [11]


DUPLICATE_SCRIPT = r'''
import json, sys
from PySide6.QtWidgets import QApplication, QMainWindow

from cove import dedup
import cove.main_window as mw

app = QApplication([])
out = {}

HEX = "0123456789abcdef0123456789abcdef01234567"
SIGNED = "https://cdn.example.com/dir/f.zip?token=dummy-token"

# Which button the next dialog should report as clicked, by label. "escape"
# means "the user hit Escape", which Qt reports as the escape button.
choice = {"button": "Cancel"}
boxes = []

real_box = mw.QMessageBox


class RecordingBox(real_box):
    def exec(self):
        default = self.defaultButton()
        escape = self.escapeButton()
        boxes.append({
            "title": self.windowTitle(),
            "text": self.text(),
            "informative": self.informativeText(),
            "buttons": [b.text() for b in self.buttons()],
            "default": default.text() if default is not None else None,
            "escape": escape.text() if escape is not None else None,
        })
        return 0

    def clickedButton(self):
        if choice["button"] == "escape":
            return self.escapeButton()
        for b in self.buttons():
            if b.text() == choice["button"]:
                return b
        return None


mw.QMessageBox = RecordingBox


class FakeTree:
    def __init__(self):
        self.current = []
        self.scrolled = []
    def setCurrentItem(self, item):
        self.current.append(item)
    def scrollToItem(self, item):
        self.scrolled.append(item)
    def setFocus(self):
        pass


class FakeQueue:
    def __init__(self, matches=None):
        self.matches = matches or {}
        self.added = []
        self.batches = []
    def find_duplicate(self, url, **kw):
        return self.matches.get(dedup.canonical_url(url) or url)
    def add_url(self, url, out_dir=None, **kw):
        self.added.append(url)
        return len(self.added)
    def add_urls(self, urls, out_dir=None, **kw):
        self.batches.append(list(urls))
        return [self.add_url(u) for u in urls]


class Host(mw.MainWindow):
    """The real MainWindow methods, without its heavy constructor."""
    def __init__(self):
        QMainWindow.__init__(self)


def host_with(matches=None):
    h = Host()
    h.queue = FakeQueue(matches)
    h.tree = FakeTree()
    h._items = {}
    return h


def run(label, host, urls):
    boxes.clear()
    ids = mw.MainWindow.add_urls_checked(host, urls)
    return {
        "boxes": list(boxes),
        "added": list(host.queue.added),
        "batches": list(host.queue.batches),
        "ids": ids,
    }


# ---- live, non-torrent -------------------------------------------------
live = dedup.DuplicateMatch(
    category=dedup.LIVE, identity=dedup.ID_URL, task_id=7, status="queued",
    name="f.zip", can_duplicate=True,
)
matches = {dedup.canonical_url(SIGNED): live}

choice["button"] = "Cancel"
out["live_cancel"] = run("live_cancel", host_with(matches), [SIGNED])

choice["button"] = "escape"
out["live_escape"] = run("live_escape", host_with(matches), [SIGNED])

choice["button"] = "Download Anyway"
out["live_anyway"] = run("live_anyway", host_with(matches), [SIGNED])

choice["button"] = "Focus Existing"
h = host_with(matches)
h._items = {7: "row-7", 9: "row-9"}
out["live_focus"] = run("live_focus", h, [SIGNED])
out["focused"] = list(h.tree.current)
out["scrolled_to"] = list(h.tree.scrolled)

# ---- live torrent ------------------------------------------------------
magnet = "magnet:?xt=urn:btih:%s&tr=https%%3A%%2F%%2Ft%%2Fdummy-passkey" % HEX
live_torrent = dedup.DuplicateMatch(
    category=dedup.LIVE, identity=dedup.ID_INFO_HASH, task_id=3, status="active",
    name="Alpha", can_duplicate=False,
)
h = host_with()
h.queue.matches = {}
h.queue.find_duplicate = lambda url, **kw: live_torrent
choice["button"] = "Cancel"
out["live_torrent"] = run("live_torrent", h, [magnet])

# ---- completed ---------------------------------------------------------
tmp = sys.argv[1]
completed_with_path = dedup.DuplicateMatch(
    category=dedup.COMPLETED, identity=dedup.ID_URL, task_id=None,
    status="completed", name="f.zip", out_dir=tmp, filename="f.zip",
)
completed_no_path = dedup.DuplicateMatch(
    category=dedup.COMPLETED, identity=dedup.ID_URL, task_id=None,
    status="completed", name="f.zip", out_dir="", filename="",
)
h = host_with()
h.queue.find_duplicate = lambda url, **kw: completed_with_path
choice["button"] = "Cancel"
out["completed_with_path"] = run("cwp", h, [SIGNED])

h = host_with()
h.queue.find_duplicate = lambda url, **kw: completed_no_path
choice["button"] = "Download Again"
out["completed_again"] = run("ca", h, [SIGNED])

# ---- batch -------------------------------------------------------------
u1 = "https://example.com/a.bin"
u2 = "https://example.com/b.bin"
batch = [u1, SIGNED, u2]
def batch_host():
    h = host_with()
    h.queue.find_duplicate = lambda url, **kw: (
        live if dedup.canonical_url(url) == dedup.canonical_url(SIGNED) else None
    )
    return h

choice["button"] = "Skip Duplicates"
out["batch_skip"] = run("bs", batch_host(), batch)
choice["button"] = "Add All Anyway"
out["batch_all"] = run("ba", batch_host(), batch)
choice["button"] = "Cancel"
out["batch_cancel"] = run("bc", batch_host(), batch)
choice["button"] = "escape"
out["batch_escape"] = run("be", batch_host(), batch)

# A repeat inside the same submission, with nothing pre-existing.
choice["button"] = "Skip Duplicates"
out["batch_intra"] = run("bi", host_with(), [u1, u2, u1])

# A live torrent stays skipped even under "Add All Anyway".
h = host_with()
h.queue.find_duplicate = lambda url, **kw: (
    live_torrent if url.startswith("magnet:") else None
)
choice["button"] = "Add All Anyway"
out["batch_all_torrent"] = run("bat", h, [u1, magnet])

# ---- startup / IPC entry point ----------------------------------------
h = host_with(matches)
choice["button"] = "Cancel"
boxes.clear()
mw.MainWindow.add_url_interactive(h, SIGNED)
out["interactive"] = {"boxes": list(boxes), "added": list(h.queue.added)}

print(json.dumps(out))
'''


def _run_duplicate_script(tmp_path):
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", DUPLICATE_SCRIPT, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


def test_duplicate_warning_dialogs(tmp_path):
    out = _run_duplicate_script(tmp_path)

    live = out["live_cancel"]
    assert len(live["boxes"]) == 1
    box = live["boxes"][0]
    assert box["text"] == "This download is already in your queue."
    assert sorted(box["buttons"]) == [
        "Cancel", "Download Anyway", "Focus Existing",
    ]
    # Cancel is both the default and the Escape button, so a dismissed
    # dialog can never start a download.
    assert box["default"] == "Cancel"
    assert box["escape"] == "Cancel"
    assert live["added"] == []
    assert out["live_escape"]["added"] == []

    # "Download Anyway" adds exactly once, through the single-URL path.
    assert out["live_anyway"]["added"] == [
        "https://cdn.example.com/dir/f.zip?token=dummy-token"
    ]
    assert out["live_anyway"]["batches"] == []

    # "Focus Existing" selects and scrolls to the matched row, adds nothing.
    assert out["live_focus"]["added"] == []
    assert out["focused"] == ["row-7"]
    assert out["scrolled_to"] == ["row-7"]


def test_live_torrent_duplicate_offers_no_download_anyway(tmp_path):
    out = _run_duplicate_script(tmp_path)
    box = out["live_torrent"]["boxes"][0]
    assert box["text"] == "This torrent is already in your queue."
    assert sorted(box["buttons"]) == ["Cancel", "Focus Existing"]
    assert "Download Anyway" not in box["buttons"]
    # The magnet's tracker passkey must not reach the dialog.
    assert "dummy-passkey" not in json.dumps(box)


def test_completed_duplicate_dialog(tmp_path):
    out = _run_duplicate_script(tmp_path)
    box = out["completed_with_path"]["boxes"][0]
    assert box["text"] == "This download appears to have already been completed."
    assert sorted(box["buttons"]) == ["Cancel", "Download Again", "Open Folder"]
    assert box["default"] == "Cancel"
    assert box["escape"] == "Cancel"
    assert out["completed_with_path"]["added"] == []

    # No usable path on disk means no "Open Folder" that could not work.
    no_path = out["completed_again"]["boxes"][0]
    assert "Open Folder" not in no_path["buttons"]
    assert out["completed_again"]["added"] == [
        "https://cdn.example.com/dir/f.zip?token=dummy-token"
    ]


def test_batch_duplicates_show_one_summary(tmp_path):
    out = _run_duplicate_script(tmp_path)
    skip = out["batch_skip"]
    assert len(skip["boxes"]) == 1, "one summary, never one modal per item"
    box = skip["boxes"][0]
    assert box["text"] == (
        "1 of 3 downloads already exist in your queue or completed history."
    )
    assert sorted(box["buttons"]) == [
        "Add All Anyway", "Cancel", "Skip Duplicates",
    ]
    assert box["default"] == "Skip Duplicates"
    assert box["escape"] == "Cancel"
    # Only a short label is shown; the signed URL and its token are not.
    assert "dummy-token" not in json.dumps(box)
    assert "cdn.example.com/f.zip" in box["informative"]

    assert skip["batches"] == [
        ["https://example.com/a.bin", "https://example.com/b.bin"]
    ]

    # Add All Anyway keeps the submitted order.
    assert out["batch_all"]["batches"] == [[
        "https://example.com/a.bin",
        "https://cdn.example.com/dir/f.zip?token=dummy-token",
        "https://example.com/b.bin",
    ]]

    assert out["batch_cancel"]["batches"] == []
    assert out["batch_cancel"]["added"] == []
    assert out["batch_escape"]["added"] == []


def test_batch_detects_repeats_inside_the_same_submission(tmp_path):
    out = _run_duplicate_script(tmp_path)
    intra = out["batch_intra"]
    assert len(intra["boxes"]) == 1
    assert intra["boxes"][0]["text"] == (
        "1 of 3 downloads already exist in your queue or completed history."
    )
    assert intra["batches"] == [
        ["https://example.com/a.bin", "https://example.com/b.bin"]
    ]


def test_batch_add_all_still_skips_a_live_torrent(tmp_path):
    out = _run_duplicate_script(tmp_path)
    # The engine cannot run one info hash twice, whatever the user picks.
    assert out["batch_all_torrent"]["batches"] == [["https://example.com/a.bin"]]


def test_startup_and_ipc_magnets_reach_the_duplicate_check(tmp_path):
    out = _run_duplicate_script(tmp_path)
    assert len(out["interactive"]["boxes"]) == 1
    assert out["interactive"]["added"] == []

    # ...and app.py routes both the startup inbox and second-instance IPC
    # through the window helper rather than straight at the queue.
    source = (Path(__file__).resolve().parents[1] / "cove" / "app.py").read_text()
    assert "window.add_url_interactive(url)" in source
    assert source.count("_add_interactive(url)") == 2
    assert "queue.add_url(url)" in source  # the pre-window fallback


def test_torrent_file_adds_pass_an_interactive_duplicate_check():
    source = (Path(__file__).resolve().parents[1] / "cove" / "main_window.py").read_text()
    # Both interactive .torrent paths (Add dialog, drag-and-drop) opt in.
    assert source.count("duplicate_check=self._confirm_duplicate") == 2


# Every other SettingsDialog test in this file runs the dialog in a
# subprocess, because constructing one inside the pytest process leaves Qt
# state that crashes the interpreter at shutdown. _magnet_status_text never
# touches self, so these call it unbound and need no dialog at all.
def _status_text(state):
    from cove.dialogs import SettingsDialog

    return SettingsDialog._magnet_status_text(None, state)


def test_magnet_status_text_never_claims_default_without_confirmation():
    from cove.magnet_handler import HandlerStatus
    import cove.magnet_identity as mi

    registered_only = HandlerStatus(
        supported=True, identity=mi.WINDOWS_PORTABLE, registered=True,
        owned_by_cove=True, is_default=False,
    )
    text = _status_text(registered_only)
    assert "not currently selected as default" in text
    assert "Cove is the current default" not in text

    confirmed = HandlerStatus(
        supported=True, identity=mi.WINDOWS_PORTABLE, registered=True,
        owned_by_cove=True, is_default=True,
    )
    assert "Cove is the current default" in _status_text(confirmed)


def test_magnet_row_explains_an_unsupported_build():
    from cove.magnet_handler import HandlerStatus

    assert "installed or portable build" in _status_text(HandlerStatus(supported=False))


def test_settings_dialog_does_not_probe_the_magnet_handler_on_the_gui_thread():
    """BUG-014: xdg-mime ran inline, freezing Settings for its whole timeout.

    Runs in an isolated process for the same reason as the layout test above:
    this needs a real QApplication.
    """
    script = r'''
import json, threading, time
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from cove import dialogs, magnet_handler
from cove.config import Settings

probe_threads = []
real_status = magnet_handler.status

def slow_status():
    probe_threads.append(threading.current_thread().name)
    time.sleep(0.4)          # stands in for a stuck xdg-mime
    return real_status()

magnet_handler.status = slow_status

app = QApplication([])
began = time.monotonic()
dialog = dialogs.SettingsDialog(Settings())
construction = time.monotonic() - began

# Let the pooled probe finish and its queued result land on the GUI thread.
deadline = time.monotonic() + 5
while not probe_threads and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.01)

print(json.dumps({
    "construction_secs": construction,
    "probe_thread": probe_threads[0] if probe_threads else None,
    "main_thread": threading.current_thread().name,
}))
'''
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120,
        cwd=str(Path(__file__).resolve().parent.parent), env=env,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])

    assert result["probe_thread"] is not None, "the status probe never ran"
    assert result["probe_thread"] != result["main_thread"], (
        "the magnet status was probed on the GUI thread"
    )
    # Construction must not have waited for the 0.4s probe.
    assert result["construction_secs"] < 0.3, result["construction_secs"]


def test_magnet_controls_serialize_and_always_come_back():
    """Registration and removal rewrite the same association.

    They must not overlap, a superseded callback must not touch the controls,
    and a failed probe must not strand the user with disabled buttons.
    """
    script = r'''
import json
from PySide6.QtWidgets import QApplication
from cove import dialogs, magnet_handler
from cove.config import Settings

app = QApplication([])
dialog = dialogs.SettingsDialog(Settings())

# Claim the controls, then deliver a result from a superseded operation.
first = dialog._begin_magnet_op()
second = dialog._begin_magnet_op()
dialog._on_magnet_status(first, None)
stale_ignored = not dialog.magnet_action_btn.isEnabled()

# A probe that could not determine the state must re-enable the controls,
# or there is no way to retry.
dialog._on_magnet_status(second, None)
recovered = (
    dialog.magnet_action_btn.isEnabled()
    and dialog.magnet_remove_btn.isEnabled()
    and dialog.magnet_repair_check.isEnabled()
)

# Starting an operation disables every magnet control, not just its own
# button, so enable and disable cannot run concurrently.
dialog._on_magnet_enable()
locked = (
    not dialog.magnet_action_btn.isEnabled()
    and not dialog.magnet_remove_btn.isEnabled()
    and not dialog.magnet_repair_check.isEnabled()
)

print(json.dumps({
    "stale_ignored": stale_ignored,
    "recovered": recovered,
    "locked": locked,
}))
'''
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120,
        cwd=str(Path(__file__).resolve().parent.parent), env=env,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])

    assert result["stale_ignored"], "a superseded callback re-enabled the controls"
    assert result["recovered"], "a failed probe left the controls stuck disabled"
    assert result["locked"], "one operation did not lock the others out"


# ---------------------------------------------------------------------------
# Download File Info dialog
# ---------------------------------------------------------------------------


FILE_INFO_SCRIPT = r'''
import json, sys
from pathlib import Path
from PySide6.QtWidgets import QApplication, QFileDialog

import cove.config as config
tmp = Path(sys.argv[1])
config.CONFIG_DIR = tmp
config.DATA_DIR = tmp
config.CONFIG_FILE = tmp / "settings.json"

from cove.config import Settings
from cove.dialogs import DownloadFileInfoDialog

app = QApplication([])
out = {}
url = "https://example.com/files/example.zip"

# ---- defaults ------------------------------------------------------------
dlg = DownloadFileInfoDialog(url, default_dir=str(tmp), parent=None)
out["title"] = dlg.windowTitle()
out["url_text"] = dlg.url_edit.text()
out["url_readonly"] = dlg.url_edit.isReadOnly()
out["filename_blank"] = dlg.filename_edit.text() == ""
out["save_to"] = dlg.dir_edit.text()
out["dont_show_again_default"] = dlg.dont_show_again.isChecked()
out["filename_placeholder"] = dlg.filename_edit.placeholderText()
out["buttons"] = [b.text() for b in dlg.buttons()]

# ---- filename result mapping --------------------------------------------
dlg.filename_edit.setText("  custom-name.zip  ")
dlg.dir_edit.setText("/srv/alt")
dlg.dont_show_again.setChecked(True)
dlg.accept()
out["result_after_accept"] = {
    "accepted": dlg.result() == dlg.DialogCode.Accepted,
    "filename": dlg.result_filename(),
    "dir": dlg.result_dir(),
    "dont_show_again": dlg.result_dont_show_again(),
}
dlg.deleteLater()

# Whitespace-only filename is blank -> None.
dlg2 = DownloadFileInfoDialog(url, default_dir=str(tmp), parent=None)
dlg2.filename_edit.setText("   ")
out["whitespace_filename_none"] = dlg2.result_filename() is None
dlg2.deleteLater()

# URL cannot be edited by the user; the widget is pinned read-only and the
# dialog never rewrites the URL it was given (setText on a read-only widget
# is a programmatic no-op for the user path, and the source value is the
# request's own URL).
dlg3 = DownloadFileInfoDialog(url, default_dir=str(tmp), parent=None)
out["url_readonly_widget"] = dlg3.url_edit.isReadOnly()
out["url_field_shows_request"] = dlg3.url_edit.text() == url
dlg3.deleteLater()

# Browse cancel retains the previous directory and keeps the dialog open.
picked = {"value": ""}
real_get = QFileDialog.getExistingDirectory
def fake_get(parent, title, start):
    out["browse_start"] = start
    return picked["value"]
QFileDialog.getExistingDirectory = staticmethod(fake_get)
dlg4 = DownloadFileInfoDialog(url, default_dir=str(tmp), parent=None)
dlg4.dir_edit.setText("/srv/prior")
dlg4._browse()
out["browse_cancel_retains"] = dlg4.dir_edit.text()
picked["value"] = "/srv/new"
dlg4._browse()
out["browse_pick_updates"] = dlg4.dir_edit.text()
out["browse_start_after_edit"] = out["browse_start"]
QFileDialog.getExistingDirectory = staticmethod(real_get)
dlg4.deleteLater()

print(json.dumps(out))
'''


def _run_file_info_script(tmp_path):
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", FILE_INFO_SCRIPT, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr[-4000:])
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_download_file_info_defaults_and_read_only_url(tmp_path):
    m = _run_file_info_script(tmp_path)

    assert m["title"] == "Download File Info"
    assert m["url_text"] == "https://example.com/files/example.zip"
    assert m["url_readonly"] is True
    assert m["filename_blank"] is True
    assert m["save_to"] == str(tmp_path)
    assert m["dont_show_again_default"] is False
    assert m["filename_placeholder"]
    assert sorted(m["buttons"]) == ["Cancel", "Start Download"]


def test_download_file_info_accept_result_and_validation(tmp_path):
    m = _run_file_info_script(tmp_path)

    assert m["result_after_accept"]["accepted"] is True
    assert m["result_after_accept"]["filename"] == "custom-name.zip"
    assert m["result_after_accept"]["dir"] == "/srv/alt"
    assert m["result_after_accept"]["dont_show_again"] is True


def test_download_file_info_whitespace_filename_is_none(tmp_path):
    m = _run_file_info_script(tmp_path)
    assert m["whitespace_filename_none"] is True


def test_download_file_info_url_cannot_be_edited(tmp_path):
    m = _run_file_info_script(tmp_path)
    assert m["url_readonly_widget"] is True
    assert m["url_field_shows_request"] is True


def test_download_file_info_browse_semantics(tmp_path):
    m = _run_file_info_script(tmp_path)

    # Browse starts from the current field value; cancel keeps it.
    assert m["browse_start"] == "/srv/prior"
    assert m["browse_cancel_retains"] == "/srv/prior"
    # A picked directory updates the field.
    assert m["browse_pick_updates"] == "/srv/new"


INVALID_FILENAME_SCRIPT = r'''
import json, sys
from pathlib import Path
from PySide6.QtWidgets import QApplication

import cove.config as config
tmp = Path(sys.argv[1])
config.CONFIG_DIR = tmp
config.DATA_DIR = tmp
config.CONFIG_FILE = tmp / "settings.json"

from cove.config import Settings
from cove.dialogs import DownloadFileInfoDialog

app = QApplication([])
out = {}

def _invalid(value, default_dir):
    dlg = DownloadFileInfoDialog(
        "https://example.com/f.zip", default_dir=default_dir, parent=None
    )
    dlg.filename_edit.setText(value)
    ok = dlg.validate()
    msg = dlg.error_label.text()
    dlg._on_start()
    rejected = dlg.result() != dlg.DialogCode.Accepted
    out.setdefault("cases", []).append({
        "value": value, "valid": ok, "message": msg, "rejected": rejected,
    })
    dlg.deleteLater()

# Path separators / absolute / dot names / reserved / controls / trailing.
for bad in ("../evil.zip", "a/b", "/abs.zip", "..", ".", "CON", "ctrl\x01x", "trail ", "trail.", "x" * 300):
    _invalid(bad, str(tmp))

# Directory validation: empty or relative is invalid.
dlg = DownloadFileInfoDialog("https://example.com/f.zip", default_dir=str(tmp), parent=None)
dlg.dir_edit.setText("")
out["empty_dir_valid"] = dlg.validate()
out["empty_dir_msg"] = dlg.error_label.text()
dlg.deleteLater()

dlg = DownloadFileInfoDialog("https://example.com/f.zip", default_dir=str(tmp), parent=None)
dlg.dir_edit.setText("relative/path")
out["relative_dir_valid"] = dlg.validate()
out["relative_dir_msg"] = dlg.error_label.text()
dlg.deleteLater()

print(json.dumps(out))
'''


def _run_invalid_filename_script(tmp_path):
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", INVALID_FILENAME_SCRIPT, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr[-4000:])
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_download_file_info_invalid_filenames_are_rejected(tmp_path):
    m = _run_invalid_filename_script(tmp_path)

    for case in m["cases"]:
        assert case["valid"] is False, case["value"]
        assert case["message"], case["value"]
        # The Start Download action must not accept an invalid value.
        assert case["rejected"] is True, case["value"]


def test_download_file_info_invalid_directories_are_rejected(tmp_path):
    m = _run_invalid_filename_script(tmp_path)

    assert m["empty_dir_valid"] is False
    assert m["empty_dir_msg"]
    assert m["relative_dir_valid"] is False
    assert m["relative_dir_msg"]


EXPAND_DIR_SCRIPT = r'''
import json, sys, os
from pathlib import Path
from PySide6.QtWidgets import QApplication

import cove.config as config
tmp = Path(sys.argv[1])
config.CONFIG_DIR = tmp
config.DATA_DIR = tmp
config.CONFIG_FILE = tmp / "settings.json"

from cove.config import Settings
from cove.dialogs import DownloadFileInfoDialog

app = QApplication([])
out = {}

os.environ["COVE_DLFI_TEST_DIR"] = "/srv/env-expanded"

# ~ expansion is validated AND committed expanded.
dlg = DownloadFileInfoDialog("https://example.com/f.zip", default_dir=str(tmp), parent=None)
dlg.dir_edit.setText("~/cove-expanded")
valid = dlg.validate()
out["tilde_valid"] = valid
out["tilde_result"] = dlg.result_dir()
dlg.deleteLater()

# $VAR expansion is validated AND committed expanded.
dlg = DownloadFileInfoDialog("https://example.com/f.zip", default_dir=str(tmp), parent=None)
dlg.dir_edit.setText("$COVE_DLFI_TEST_DIR/sub")
valid = dlg.validate()
out["env_valid"] = valid
out["env_result"] = dlg.result_dir()
dlg.deleteLater()

# A $VAR whose expanded value carries a control character must be rejected,
# because that expanded value is exactly what would be committed.
os.environ["COVE_DLFI_CTRL_DIR"] = "/srv/bad\x01dir"
dlg = DownloadFileInfoDialog("https://example.com/f.zip", default_dir=str(tmp), parent=None)
dlg.dir_edit.setText("$COVE_DLFI_CTRL_DIR")
valid = dlg.validate()
out["ctrl_expanded_valid"] = valid
out["ctrl_expanded_msg"] = dlg.error_label.text()
dlg.deleteLater()

print(json.dumps(out))
'''


def _run_expand_dir_script(tmp_path):
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", EXPAND_DIR_SCRIPT, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr[-4000:])
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_download_file_info_expanded_directories_validate_and_commit_expanded(
    tmp_path,
):
    """Codex #3: `~` and `$VAR` forms pass validation AND the committed path
    is the expanded absolute path — never the raw text."""
    m = _run_expand_dir_script(tmp_path)

    assert m["tilde_valid"] is True
    assert m["tilde_result"] == str(Path.home() / "cove-expanded")
    assert m["env_valid"] is True
    assert m["env_result"] == "/srv/env-expanded/sub"
    # Codex round 4 #2: control characters are checked on the EXPANDED value.
    assert m["ctrl_expanded_valid"] is False
    assert m["ctrl_expanded_msg"]
