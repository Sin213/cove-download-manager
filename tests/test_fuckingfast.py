"""Strict parsing and one-shot, filesystem-only browser consent."""
import json
import os
import re
from pathlib import Path
import pytest
from cove import fuckingfast as ff, config, native_messaging as nm


@pytest.mark.parametrize("url,file_id,hint", [
    ("https://fuckingfast.co/6oavo2x52dq0", "6oavo2x52dq0", ""),
    ("http://WWW.FUCKINGFAST.CO/f/Ab_4/?q=ignored#two%20words.zip", "Ab_4", "two words.zip"),
    ("HTTPS://fuckingfast.co:443/Ab_4#name.zip", "Ab_4", "name.zip"),
    *[("https://fuckingfast.co/Ab_4#" + hint, "Ab_4", "") for hint in
      ("../x", "..%2Fx", "CON.zip", "%00file", "x" * 256, "%C3%A9" * 128)],
])
def test_parser(url, file_id, hint):
    link = ff.parse_share_url(url)
    assert link and link.file_id == file_id and link.filename_hint == hint
    host = "www.fuckingfast.co" if "WWW." in url else "fuckingfast.co"
    assert link.page_url == f"https://{host}/{file_id}"


@pytest.mark.parametrize("url", [
    "https://fuckingfast.co.evil.com/Ab_4", "https://evilfuckingfast.co/Ab_4",
    "https://user@fuckingfast.co/Ab_4", "https://fuckingfast.co:80/Ab_4",
    "https://fuckingfast.co./Ab_4", "https://127.0.0.1/Ab_4",
    "https://fuckingfast.co\\@evil.com/Ab_4", "https://fuck\ningfast.co/Ab_4",
    "https://fuckingfast.co/dl/Ab_4", "https://fuckingfast.co/f/Ab_4/go",
    "https://fuckingfast.co/abc", "https://fuckingfast.co/" + "x" * 65,
    *["https://fuckingfast.co/" + prefix + word for prefix in ("", "f/") for word in
      ("help", "LOGIN", "terms", "privacy", "contact", "speed", "speedtest", "upload", "account")],
])
def test_not_a_share(url):
    assert ff.parse_share_url(url) is None


@pytest.mark.parametrize("url", [
    "https://[fuckingfast.co/Ab_4", "https://fuckingfast.co]/Ab_4",
    "https:\\fuckingfast.co\\Ab_4", "https://user@fuckingfast.co:8443/Ab_4",
    "https://fuck\tingfast.co./Ab_4", "https://fuckingfast.co/help",
])
def test_guard_classifier_is_separate_from_browser_eligibility(url):
    assert ff.parse_share_url(url) is None
    assert ff.is_landing_page_url(url)
    assert ff.share_link_reason(url) == ff.BROWSER_REQUIRED


@pytest.mark.parametrize("url", [None, [], "", "file://fuckingfast.co/Ab_4",
                                    "https://evil.com/[fuckingfast.co",
                                    "https://[evil.com/fuckingfast.co/Ab_4"])
def test_guard_classifier_does_not_scan_paths_or_non_http_urls(url):
    assert not ff.is_landing_page_url(url)
    assert ff.share_link_reason(url) == ""


def test_ticket_one_shot_expiry_and_binding(tmp_path):
    link = ff.parse_share_url("https://fuckingfast.co/Ab_4#name.zip")
    nonce = ff.issue_ticket(tmp_path, link, now=1000)
    assert re.fullmatch("[0-9a-f]{32}", nonce)
    assert ff.fetch_url(link, nonce) == "https://fuckingfast.co/Ab_4#cove-fetch=" + nonce
    assert json.loads((tmp_path / "browser-fetch" / (nonce + ".json")).read_text())["exp"] == 1100
    assert ff.ticket_exists(tmp_path, nonce)
    assert ff.consume_ticket(tmp_path, nonce, "Other_1", now=1001) is None
    assert ff.consume_ticket(tmp_path, nonce, link.file_id, now=1001) == {"hint": "name.zip", "dir": ""}
    assert ff.consume_ticket(tmp_path, nonce, link.file_id, now=1001) is None
    expired = ff.issue_ticket(tmp_path, link, now=1000)
    assert ff.consume_ticket(tmp_path, expired, link.file_id, now=1101) is None
    nonce = ff.issue_ticket(tmp_path, link)
    folder = tmp_path / "browser-fetch"
    if os.name == "posix":
        assert folder.stat().st_mode & 0o777 == 0o700
        assert (folder / (nonce + ".json")).stat().st_mode & 0o777 == 0o600
    ff.revoke_ticket(tmp_path, nonce)
    assert not ff.ticket_exists(tmp_path, nonce)


def test_revoke_reports_whether_it_removed_the_ticket(tmp_path):
    link = ff.parse_share_url("https://fuckingfast.co/Ab_4")
    nonce = ff.issue_ticket(tmp_path, link)
    assert ff.revoke_ticket(tmp_path, nonce) is True
    assert not ff.ticket_exists(tmp_path, nonce)
    assert ff.revoke_ticket(tmp_path, nonce) is False
    for invalid in ("../outside", "", None, [], "A" * 32):
        assert ff.revoke_ticket(tmp_path, invalid) is False


@pytest.mark.parametrize("nonce,file_id", [("../outside", "Ab_4"), ("F" * 32, "Ab_4"),
                                         (None, {}), ([], []), ("a" * 32, "../x")])
def test_garbage_does_not_raise_or_build_paths(tmp_path, nonce, file_id):
    assert ff.consume_ticket(tmp_path, nonce, file_id) is None
    assert ff.ticket_exists(tmp_path, nonce) is False
    ff.revoke_ticket(tmp_path, nonce)


def test_ticket_symlinks_malformed_and_sweep(tmp_path):
    link = ff.parse_share_url("https://fuckingfast.co/Ab_4")
    other = tmp_path / "other"
    other.mkdir()
    folder = tmp_path / "browser-fetch"
    folder.symlink_to(other, target_is_directory=True)
    assert not ff.issue_ticket(tmp_path, link)
    folder.unlink()
    nonce = ff.issue_ticket(tmp_path, link, now=1000)
    assert nonce
    path = folder / (nonce + ".json")
    path.unlink()
    target = other / "secret"
    target.write_text('{"id":"Ab_4","hint":"","exp":1050,"dir":""}')
    path.symlink_to(target)
    assert ff.consume_ticket(tmp_path, nonce, "Ab_4", now=1001) is None
    path.unlink()
    for value in ([], {"id": "Ab_4", "hint": "", "exp": True},
                  {"id": "Ab_4", "hint": "", "exp": 9999},
                  {"id": "Ab_4", "hint": "../unsafe", "exp": 1050},
                  {"id": "Ab_4", "hint": "", "exp": 1050, "extra": 1}):
        if isinstance(value, dict):
            value["dir"] = ""
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        assert ff.consume_ticket(tmp_path, nonce, "Ab_4", now=1001) is None
    path.write_text('{"id":"Ab_4","hint":"","exp":999}')
    stale = folder / "foreign"
    stale.write_text("garbage")
    os.utime(stale, (1, 1))
    assert ff.issue_ticket(tmp_path, link, now=1000)
    assert not path.exists() and not stale.exists() and target.exists()


def test_native_ticket_action_has_fixed_private_replies(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    logs = []
    monkeypatch.setattr(nm, "_log", lambda *a, **k: logs.append((a, k)))
    link = ff.parse_share_url("https://fuckingfast.co/Ab_4#name.zip")
    nonce = ff.issue_ticket(tmp_path, link)
    assert nonce
    msg = {"action": "fetch_ticket", "nonce": nonce, "id": link.file_id, "requestId": nonce}
    assert nm.handle_message(msg, None, None) == {"status": "ok", "hint": "name.zip", "directory": ""}
    assert nm.handle_message(msg, None, None) == {
        "status": "error", "message": "Cove did not authorize this page."}
    for nonce_value, id_value in (("../outside", "Ab_4"), ([], {}), (None, None)):
        assert nm.handle_message({"action": "fetch_ticket", "nonce": nonce_value, "id": id_value},
                                 None, None) == {"status": "error", "message": "Cove did not authorize this page."}
    assert nonce not in repr(logs) and "https://" not in repr(logs)


def test_ticket_rejects_duplicate_keys_oversize_and_failed_unlink(tmp_path, monkeypatch):
    link = ff.parse_share_url("https://fuckingfast.co/Ab_4")
    nonce = ff.issue_ticket(tmp_path, link, now=1000)
    path = tmp_path / "browser-fetch" / (nonce + ".json")
    valid = '{"id":"Ab_4","hint":"","exp":1050,"dir":""}'
    for raw in ('{"id":"Ab_4","hint":"","exp":1050,"exp":1051,"dir":""}',
                valid + " " * ff._MAX_TICKET_BYTES):
        path.write_text(raw)
        assert ff.consume_ticket(tmp_path, nonce, "Ab_4", now=1001) is None
    path.write_text('{"id":"Ab_4","hint":"","exp":1050,"dir":""}')
    original = Path.unlink
    def lost_race(self, *a, **k):
        if self == path:
            raise FileNotFoundError()
        return original(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", lost_race)
    assert ff.consume_ticket(tmp_path, nonce, "Ab_4", now=1001) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership contract")
def test_ticket_directory_owned_by_another_user_is_refused(tmp_path, monkeypatch):
    link = ff.parse_share_url("https://fuckingfast.co/Ab_4")
    nonce = ff.issue_ticket(tmp_path, link)
    uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    assert not ff.issue_ticket(tmp_path, link)
    assert ff.consume_ticket(tmp_path, nonce, link.file_id) is None


@pytest.mark.parametrize("directory,expected", [
    (None, ""), ("", ""), ("/chosen/folder", "/chosen/folder"),
    ("x" * 4096, "x" * 4096), ("\U0001f4c1" * 4096, "\U0001f4c1" * 4096),
    ("x" * 4097, ""), ("bad\nfolder", ""), ("bad\x00folder", ""),
    ("bad\x7ffolder", ""), ("bad\x85folder", ""), ([], ""), (Path("/folder"), ""),
], ids=["none", "empty", "folder", "max_ascii", "max_unicode", "too_long", "newline", "nul", "del", "c1", "list", "path"])
def test_ticket_directory_round_trip(tmp_path, directory, expected):
    link = ff.parse_share_url("https://fuckingfast.co/Ab_4#name.zip")
    nonce = ff.issue_ticket(tmp_path, link, directory=directory)
    assert nonce
    assert ff.consume_ticket(tmp_path, nonce, link.file_id) == {"hint": "name.zip", "dir": expected}


def test_native_ticket_reply_carries_directory_without_logging_it(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    logs = []
    monkeypatch.setattr(nm, "_log", lambda *a, **k: logs.append((a, k)))
    link = ff.parse_share_url("https://fuckingfast.co/Ab_4")
    nonce = ff.issue_ticket(tmp_path, link, directory="/PRIVATE_FOLDER")
    result = nm.handle_message({"action": "fetch_ticket", "nonce": nonce, "id": link.file_id}, None, None)
    assert result == {"status": "ok", "hint": "", "directory": "/PRIVATE_FOLDER"}
    assert "PRIVATE_FOLDER" not in repr(logs)
