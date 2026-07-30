"""Privacy rules for the per-task "View source" action."""

import json
import os
from pathlib import Path
import subprocess
import sys

from cove.queue import DownloadTask
from cove.source_info import REDACTED, redact_url, source_details


def _task(**kw) -> DownloadTask:
    base = dict(id=1, url="https://example.com/a.bin", out_dir="/tmp/dl")
    base.update(kw)
    return DownloadTask(**base)


def test_redact_url_passes_through_a_plain_url():
    assert redact_url("https://example.com/a.bin?page=2") == (
        "https://example.com/a.bin?page=2"
    )


def test_redact_url_strips_embedded_credentials():
    out = redact_url("https://joe:hunter2@example.com/a.bin")
    assert "hunter2" not in out
    assert "joe" not in out
    assert out == f"https://{REDACTED}@example.com/a.bin"


def test_redact_url_masks_secret_query_parameters():
    out = redact_url(
        "https://cdn.example.com/a.bin"
        "?token=abc123&X-Amz-Signature=deadbeef&api_key=k1&page=2"
    )
    assert "abc123" not in out
    assert "deadbeef" not in out
    assert "k1" not in out
    # Harmless parameters survive, so the user can still recognise the link.
    assert "page=2" in out
    assert out.count(REDACTED) == 3


def test_redact_url_masks_tracker_passkey_parameters():
    # The private-tracker HTTP form of the same secret already masked in
    # magnet tr= values.
    out = redact_url(
        "https://tracker.example/download/1?passkey=SECRET123&authkey=SECRET456"
    )
    assert "SECRET123" not in out
    assert "SECRET456" not in out


def test_redact_url_masks_camelcase_secret_parameters():
    out = redact_url("https://x.example/a.bin?accessToken=SECRET1&apiKey=SECRET2")
    assert "SECRET1" not in out
    assert "SECRET2" not in out


def test_redact_url_masks_a_credential_shaped_path_segment():
    # A private tracker's passkey usually sits in the path, not the query.
    out = redact_url(
        "https://t.example/announce/3f9a2b7c1d4e5f60718293a4b5c6d7e8/a.bin"
    )
    assert "3f9a2b7c1d4e5f60718293a4b5c6d7e8" not in out
    # The parts that identify the download still read normally.
    assert "t.example" in out
    assert "announce" in out
    assert "a.bin" in out


def test_redact_url_keeps_ordinary_path_segments_readable():
    out = redact_url("https://example.com/videos/2026/season-01/episode-12.mkv")
    assert out == "https://example.com/videos/2026/season-01/episode-12.mkv"


def test_redact_url_masks_secret_fragment_parameters():
    # The OAuth implicit flow delivers tokens in the fragment.
    out = redact_url("https://x.example/a.bin#access_token=SECRET1&page=2")
    assert "SECRET1" not in out
    assert "page=2" in out


def test_redact_url_keeps_a_plain_fragment_readable():
    out = redact_url("https://example.com/doc.pdf#section-2")
    assert out == "https://example.com/doc.pdf#section-2"


def test_redact_url_masks_secrets_in_the_fragment():
    # OAuth implicit-flow callbacks put the token after the '#'.
    out = redact_url(
        "https://app.example/done#access_token=SECRET1&token_type=bearer"
    )
    assert "SECRET1" not in out
    # Parameter names survive, so the user can still see what the link is.
    assert "access_token=" in out
    assert "token_type=" in out


def test_redact_url_keeps_an_ordinary_fragment_readable():
    assert redact_url("https://example.com/doc.pdf#page=4") == (
        "https://example.com/doc.pdf#page=4"
    )


def test_redact_url_keeps_non_secret_lookalikes_readable():
    out = redact_url("https://example.com/a.bin?monkey=1&keyboard=2")
    assert out == "https://example.com/a.bin?monkey=1&keyboard=2"


def test_redact_url_masks_magnet_tracker_passkeys():
    out = redact_url(
        "magnet:?xt=urn:btih:abcdef&dn=Some+Name"
        "&tr=https://tracker.example/PASSKEY123/announce"
        "&ws=https://seed.example/PASSKEY123/a.bin"
    )
    assert "PASSKEY123" not in out
    # The parts that let the user recognise the torrent survive intact.
    assert "xt=urn:btih:abcdef" in out
    assert "dn=Some+Name" in out


def test_redact_url_preserves_the_byte_form_of_kept_parameters():
    # Masking one parameter must not re-encode the others.
    out = redact_url("https://example.com/a.bin?token=x&dn=Some+Name&p=a%2Fb")
    assert out == (
        f"https://example.com/a.bin?token={REDACTED}&dn=Some+Name&p=a%2Fb"
    )


def test_source_details_identify_a_torrent_without_its_tracker_passkey():
    task = _task(
        url="magnet:?xt=urn:btih:abcdef&tr=https://tracker.example/PK9/announce",
        source_type="torrent",
        info_hash="abcdef",
        torrent_name="Some Name",
    )
    details = dict(source_details(task))
    assert details["Info hash"] == "abcdef"
    assert details["Torrent name"] == "Some Name"
    assert "PK9" not in "".join(details.values())


def test_redact_url_handles_empty_and_non_url_values():
    assert redact_url("") == ""
    assert redact_url(None) == ""
    assert redact_url("magnet:?xt=urn:btih:abc&dn=name") == (
        "magnet:?xt=urn:btih:abc&dn=name"
    )


def test_source_details_never_expose_cookies_or_resolved_url():
    task = _task(
        url="https://joe:hunter2@example.com/a.bin?token=abc123",
        cookies="session=supersecret",
        resolved_url="https://node7.provider.example/secret-delivery",
        referrer="https://joe:hunter2@example.com/page",
        user_agent="Mozilla/5.0",
    )
    blob = "\n".join(f"{k}\t{v}" for k, v in source_details(task))
    for secret in (
        "supersecret",
        "node7.provider.example",
        "secret-delivery",
        "hunter2",
        "abc123",
    ):
        assert secret not in blob
    labels = [k for k, _ in source_details(task)]
    assert "Source URL" in labels
    assert "Referrer" in labels
    assert "User agent" in labels
    # The presence of cookies is disclosed, the value never is.
    assert ("Browser cookies", "Stored, not shown") in source_details(task)


def test_source_details_omit_empty_fields():
    labels = [k for k, _ in source_details(_task())]
    assert "Referrer" not in labels
    assert "User agent" not in labels
    assert "Browser cookies" not in labels
    assert labels[0] == "Source URL"


def test_source_details_report_destination_and_backend():
    task = _task(filename="a.bin", backend="ffmpeg", out_dir="/tmp/dl")
    details = dict(source_details(task))
    assert details["File name"] == "a.bin"
    assert details["Save folder"] == "/tmp/dl"
    assert details["Backend"] == "ffmpeg"


def test_source_details_name_the_debrid_provider_without_the_locked_link():
    task = _task(
        debrid_provider="torbox",
        debrid_item_id="T-9911",
        url="https://locked.example/token/zzz9",
    )
    details = dict(source_details(task))
    assert details["Debrid provider"] == "torbox"
    assert "T-9911" not in "".join(details.values())


def test_source_details_dialog_shows_redacted_url_and_copies_on_request():
    script = r'''
import json
from PySide6.QtWidgets import QApplication
from cove.queue import DownloadTask
from cove.dialogs import SourceDetailsDialog

app = QApplication([])
task = DownloadTask(
    id=1,
    url="https://joe:hunter2@example.com/a.bin?token=abc123&page=2",
    out_dir="/tmp/dl",
    cookies="session=supersecret",
    referrer="https://example.com/page",
)
dialog = SourceDetailsDialog(task)
dialog.show()
app.processEvents()
shown = dialog.details_text()
dialog.copy_url()
copied_redacted = QApplication.clipboard().text()
dialog.copy_original_url()
copied_original = QApplication.clipboard().text()
print(json.dumps({
    "shown": shown,
    "copied_redacted": copied_redacted,
    "copied_original": copied_original,
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
        timeout=30,
        check=True,
    )
    data = json.loads(result.stdout)

    # Nothing sensitive is rendered, and nothing is copied without an
    # explicit user action.
    for secret in ("hunter2", "abc123", "supersecret"):
        assert secret not in data["shown"]
    assert "page=2" in data["shown"]
    assert "Stored, not shown" in data["shown"]

    assert data["copied_redacted"] == redact_url(
        "https://joe:hunter2@example.com/a.bin?token=abc123&page=2"
    )
    # The original is only ever reachable through its own deliberate action.
    assert data["copied_original"] == (
        "https://joe:hunter2@example.com/a.bin?token=abc123&page=2"
    )


def test_context_menu_view_source_action_opens_the_dialog_for_that_task():
    script = r'''
import json
from PySide6.QtWidgets import QApplication, QMenu
from cove.main_window import MainWindow
from cove.queue import DownloadTask

app = QApplication([])

class _Fake:
    def __init__(self):
        self.opened = []
    def _show_source_details(self, task):
        self.opened.append(task.id)

fake = _Fake()
menu = QMenu()
task = DownloadTask(id=7, url="https://example.com/a.bin", out_dir="/tmp/dl")
MainWindow._add_source_action(fake, menu, task)
labels = [a.text() for a in menu.actions()]
menu.actions()[0].trigger()
print(json.dumps({"labels": labels, "opened": fake.opened}))
'''
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    data = json.loads(result.stdout)

    assert data["labels"] == ["View source"]
    assert data["opened"] == [7]
