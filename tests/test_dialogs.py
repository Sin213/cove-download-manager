"""Layout regressions for Cove dialogs."""

import json
import os
from pathlib import Path
import subprocess
import sys


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
