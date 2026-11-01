"""Gofile shares: recognition, listing, selection, queue and false-success guards.

Every request is faked; a real Gofile endpoint or token is never contacted.
Real aria2 runs only against a loopback server (see the end-to-end section).
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from cove import db, gofile
from cove.config import Settings
from tests.test_queue import queue_env  # noqa: F401
from tests.test_queue_output_ownership import inline

SHARE = "https://gofile.io/d/ObvAFn9h"
TOKEN = "GOFILE_TOKEN_CANARY_0123456789abcdef"  # gitleaks:allow
LANDING = (b"<!doctype html><html><body>Gofile needs JavaScript to run.</body></html>")


def settings(**kwargs):
    return Settings(**dict(gofile_api_token=TOKEN, **kwargs))


# --- the observed bug ------------------------------------------------------


def test_share_page_is_not_an_ordinary_download(queue_env):
    """RED on the old tree: a share page became a plain HTTP task."""
    queue, rpc, _ = queue_env()
    errors = []
    queue.error.connect(errors.append)
    assert queue.prepare_url(SHARE) is None
    assert queue.add_url(SHARE) is None
    assert errors == [gofile.INTAKE_MESSAGE] * 2
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM downloads").fetchone()[0] == 0
    assert not rpc.added


def test_legacy_plain_share_row_is_never_launched(queue_env):
    """RED on the old tree: a persisted plain row handed the page to aria2."""
    queue, rpc, _ = queue_env()
    tid = queue.add_url("https://example.test/file.zip")
    task = queue.tasks[tid]
    task.url = SHARE
    inline(queue)
    queue._launch(task)
    assert task.status == "error" and task.error == gofile.INTAKE_MESSAGE
    assert not rpc.added


# --- shared fakes ----------------------------------------------------------

F1 = "11111111-aaaa-4aaa-8aaa-111111111111"
F2 = "22222222-bbbb-4bbb-8bbb-222222222222"
F3 = "33333333-cccc-4ccc-8ccc-333333333333"
SUB = "99999999-dddd-4ddd-8ddd-999999999999"


class Resp:
    """The slice of requests.Response that gofile.py uses."""

    def __init__(self, payload=None, status=200, headers=None, body=b""):
        self._payload, self.status_code = payload, status
        self.headers = headers or {}
        self._body, self.closed = body, False

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def iter_content(self, size):
        yield self._body[:size]

    def close(self):
        self.closed = True


def ok(data, metadata=None):
    return Resp({"status": "ok", "data": data, "metadata": metadata or {}})


def link_for(file_id, name):
    return f"https://store-1.gofile.io/download/web/{file_id}/{name}"


def file_node(file_id, name, size, **extra):
    return {"id": file_id, "type": "file", "name": name, "size": size,
            "link": link_for(file_id, name), **extra}


def folder_node(content_id, name, children, **extra):
    return {"id": content_id, "type": "folder", "name": name,
            "children": {child["id"]: child for child in children}, **extra}


class Net:
    """Routes API calls by (content id, page) and downloads by URL."""

    def __init__(self, api=None, downloads=None):
        self.api, self.downloads, self.calls = dict(api or {}), dict(downloads or {}), []

    def get(self, url, **kwargs):
        from urllib.parse import parse_qs, urlsplit
        self.calls.append((url, kwargs))
        parts = urlsplit(url)
        if parts.netloc == "api.gofile.io":
            page = parse_qs(parts.query).get("page")
            key = (parts.path.rsplit("/", 1)[-1], int(page[0]) if page else None)
            value = self.api[key]
        else:
            value = self.downloads[url]
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def api_calls(self):
        return [url for url, _ in self.calls if "api.gofile.io" in url]


def good_download(name="a.bin", body=b"\x00binary-bytes"):
    return Resp(status=206, headers={"Content-Type": "application/octet-stream"}, body=body)


def tree_net():
    """A share with two files, a subfolder, and a second page of children."""
    root_page1 = folder_node("ObvAFn9h", "Share", [file_node(F1, "A.zip", 1_200), SUB and
                             folder_node(SUB, "Sub", [])])
    root_page2 = folder_node("ObvAFn9h", "Share", [file_node(F2, "B.mp4", 2_400)])
    sub = folder_node(SUB, "Sub", [file_node(F3, "C.txt", 10)])
    return Net(api={
        ("ObvAFn9h", 1): ok(root_page1, {"page": 1, "hasNextPage": True}),
        ("ObvAFn9h", 2): ok(root_page2, {"page": 2, "hasNextPage": False}),
        (SUB, 1): ok(sub, {"page": 1, "hasNextPage": False}),
    })


# --- A. recognition --------------------------------------------------------


@pytest.mark.parametrize("url", [
    SHARE, SHARE + "/", "http://gofile.io/d/ObvAFn9h", "https://www.gofile.io/d/ObvAFn9h",
    "HTTPS://GoFile.io/d/abc123", " https://gofile.io/d/ObvAFn9h ",
])
def test_share_forms_are_recognised_and_parsed(url):
    assert gofile.is_share_url(url)
    assert gofile.parse_share_url(url) == url.strip().rstrip("/").rsplit("/", 1)[-1]


@pytest.mark.parametrize("url", [
    "https://gofile.io/d/", "https://gofile.io/d", "https://gofile.io/d/ab", "https://gofile.io/d/ObvAFn9hX",
    "https://gofile.io/d/ObvAFn9h?x=1", "https://gofile.io/d/ObvAFn9h#frag",
    "https://gofile.io/d/Obv_Fn9h", "https://gofile.io/d/ObvAFn9h/extra",
    "https://user:pw@gofile.io/d/ObvAFn9h", "https://gofile.io:8443/d/ObvAFn9h",
    "https://gofile.io/?c=ObvAFn9h", "https://gofile.io/d/11111111-aaaa-4aaa-8aaa-111111111111",
])
def test_broken_share_forms_are_claimed_but_never_parsed(url):
    assert gofile.is_share_url(url) and gofile.parse_share_url(url) is None


@pytest.mark.parametrize("url", [
    "https://gofile.io.evil.test/d/ObvAFn9h", "https://evilgofile.io/d/ObvAFn9h",
    "https://gofile.io@evil.test/d/ObvAFn9h", "https://evil.test/gofile.io/d/ObvAFn9h",
    "https://store-1.gofile.io/d/ObvAFn9h", "ftp://gofile.io/d/ObvAFn9h",
    "https://gofile.io/", "https://gofile.io/premium", "https://example.test/file.zip",
    "magnet:?xt=urn:btih:abc", "", None, 5,
])
def test_other_links_are_not_gofile_shares(url):
    assert not gofile.is_share_url(url)


def test_file_ref_round_trips_and_names_no_download_location():
    ref = gofile.file_ref("ObvAFn9h", F1)
    assert gofile.parse_file_ref(ref) == ("ObvAFn9h", F1)
    assert "store" not in ref and TOKEN not in ref
    for bad in (SHARE, ref + "?x=1", "https://gofile.io/d/ObvAFn9h#", "https://evil.test/d/ObvAFn9h#" + F1):
        assert gofile.parse_file_ref(bad) is None
    with pytest.raises(gofile.GofileError):
        gofile.file_ref("ObvAFn9h", "../x")


def test_paste_routes_to_resolver_not_to_a_plain_download(queue_env, monkeypatch):
    import cove.main_window as mw
    queue, rpc, _ = queue_env()
    owner = flow_owner(queue)
    seen = []
    owner._gofile_share_preflight = lambda *args: seen.append(args) or []
    owner._candidate = mw.MainWindow._candidate
    owner._add_single_interactive = lambda *args: None
    ids = owner.add_urls_checked([SHARE, "https://example.test/file.bin"])
    assert seen == [(SHARE, None)] and len(ids) == 1
    assert queue.tasks[ids[0]].url == "https://example.test/file.bin"
    monkeypatch.setattr(queue, "add_url", lambda *a, **k: pytest.fail("share became a plain URL"))
    assert owner.add_urls_checked([SHARE]) == [] and not rpc.added


# --- B. listing ------------------------------------------------------------


def test_one_file_share_lists_its_real_name_and_size():
    net = Net(api={("ObvAFn9h", 1): ok(file_node(F1, "Archive.zip", 13_314_398_617))})
    listing = gofile.list_share(SHARE, settings(), session=net)
    assert [(f.name, f.size, f.file_id, f.share) for f in listing.files] == [
        ("Archive.zip", 13_314_398_617, F1, "ObvAFn9h")]
    url, kwargs = net.calls[0]
    assert kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    assert kwargs["allow_redirects"] is False and kwargs["timeout"]


def test_folder_share_lists_pages_and_subfolders_with_safe_paths():
    net = tree_net()
    listing = gofile.list_share(SHARE, settings(), session=net)
    assert [(f.path, f.size) for f in listing.files] == [
        (("A.zip",), 1_200), (("B.mp4",), 2_400), (("Sub", "C.txt"), 10)]
    assert listing.unavailable == 0
    assert len(net.api_calls()) == 3 and "page=2" in net.api_calls()[1]


@pytest.mark.parametrize("name", ["..", "../x", "a/b", "a\\b", "", " ", "x" * 300, "CON", "a:b", "nul.txt", None, 5])
def test_unsafe_provider_names_are_dropped_not_trusted(name):
    net = Net(api={("ObvAFn9h", 1): ok(folder_node("ObvAFn9h", "S", [
        file_node(F1, "ok.bin", 1), file_node(F2, name, 2)]))})
    listing = gofile.list_share(SHARE, settings(), session=net)
    assert [f.name for f in listing.files] == ["ok.bin"] and listing.unavailable == 1


def test_frozen_files_and_bad_sizes_are_counted_unavailable():
    nodes = [file_node(F1, "ok.bin", 1), file_node(F2, "cold.bin", 5, isFrozen=True),
             file_node(F3, "huge.bin", 2**63)]
    net = Net(api={("ObvAFn9h", 1): ok(folder_node("ObvAFn9h", "S", nodes))})
    listing = gofile.list_share(SHARE, settings(), session=net)
    assert [f.name for f in listing.files] == ["ok.bin"] and listing.unavailable == 2


@pytest.mark.parametrize("size", [-1, True, "1", 1.5, None, float("nan")])
def test_malformed_sizes_never_become_files(size):
    net = Net(api={("ObvAFn9h", 1): ok(folder_node("ObvAFn9h", "S", [
        file_node(F1, "ok.bin", 1), file_node(F2, "bad.bin", size)]))})
    assert [f.name for f in gofile.list_share(SHARE, settings(), session=net).files] == ["ok.bin"]


def test_listing_is_bounded():
    many = [file_node(f"{i:08d}-aaaa", f"f{i}.bin", 1) for i in range(gofile.MAX_FILES + 1)]
    net = Net(api={("ObvAFn9h", 1): ok(folder_node("ObvAFn9h", "S", many))})
    with pytest.raises(gofile.GofileError) as info:
        gofile.list_share(SHARE, settings(), session=net)
    assert info.value.user_message == gofile.TOO_MANY
    endless = Net(api={("ObvAFn9h", p): ok(folder_node("ObvAFn9h", "S", [file_node(f"{p:08d}-aaaa", "x.bin", 1)]),
                                           {"hasNextPage": True}) for p in range(1, 200)})
    with pytest.raises(gofile.GofileError) as info:
        gofile.list_share(SHARE, settings(), session=endless)
    assert info.value.code == "too_large" and len(endless.api_calls()) <= gofile.MAX_REQUESTS


# --- F. failures are actionable and fixed ----------------------------------


def listing_error(net, **kwargs):
    with pytest.raises(gofile.GofileError) as info:
        gofile.list_share(SHARE, settings(**kwargs), session=net)
    return info.value


def api_net(resp):
    return Net(api={("ObvAFn9h", 1): resp})


@pytest.mark.parametrize("token", ["", "   ", None, 5])
def test_listing_needs_a_token_and_never_calls_out_without_one(token):
    net = api_net(ok({}))
    with pytest.raises(gofile.GofileError) as info:
        gofile.list_share(SHARE, Settings(gofile_api_token=token), session=net)
    assert info.value.user_message == gofile.NEEDS_TOKEN and not net.calls


@pytest.mark.parametrize("token", ["short", "has space 0123456789abcdef", "line\nbreak0123456789abcdef"])
def test_malformed_tokens_are_refused_before_any_request(token):
    net = api_net(ok({}))
    with pytest.raises(gofile.GofileError) as info:
        gofile.list_share(SHARE, Settings(gofile_api_token=token), session=net)
    assert info.value.user_message == gofile.BAD_TOKEN and not net.calls


@pytest.mark.parametrize("resp, message", [
    (Resp({"status": "error-notPremium", "data": {}}, 401), gofile.NOT_PREMIUM),
    (Resp({"status": "error-token", "data": {}}, 401), gofile.BAD_TOKEN),
    (Resp({"status": "error-wrongToken", "data": {}}, 401), gofile.BAD_TOKEN),
    (Resp({"status": "error-notFound", "data": {}}, 404), gofile.NOT_FOUND),
    (Resp({"status": "error-rateLimit", "data": {}}, 429), gofile.RATE_LIMITED),
    (Resp(None, 403), gofile.BAD_TOKEN),
    (Resp(None, 404), gofile.NOT_FOUND),
    (Resp(ValueError("not json"), 429), gofile.RATE_LIMITED),
    (Resp(ValueError("not json"), 502), gofile.UNAVAILABLE),
    (Resp({"status": "error-weird", "data": {}}, 200), gofile.BAD_LISTING),
    (Resp({"status": "error-field", "data": {}}, 400), gofile.BAD_LISTING),
    (ok({"type": "folder", "canAccess": False, "children": {}}), gofile.NO_ACCESS),
    (ok({"type": "folder", "children": []}), gofile.BAD_LISTING),
    (ok({"type": "weird"}), gofile.BAD_LISTING),
    (ok({"type": "folder", "children": {"x": file_node(F1, "a", 1)}}), gofile.BAD_LISTING),
    (ok({"type": "folder", "children": {}}), gofile.NO_FILES),
    (Resp("<!doctype html>", 200), gofile.BAD_LISTING),
    (Resp(ValueError("html page"), 200), gofile.BAD_LISTING),
    (Resp([], 200), gofile.BAD_LISTING),
    (Resp({"status": "ok", "data": []}, 200), gofile.BAD_LISTING),
])
def test_listing_failures_are_actionable_and_fixed(resp, message):
    error = listing_error(api_net(resp))
    assert error.user_message == message and str(error) == message


def test_transport_failure_is_fixed_and_leaks_nothing():
    net = api_net(None)
    net.api[("ObvAFn9h", 1)] = ConnectionError(f"{SHARE} {TOKEN}")
    error = listing_error(net)
    assert error.user_message == gofile.UNAVAILABLE
    assert TOKEN not in repr(error) and error.__cause__ is None


def test_broken_share_form_never_reaches_the_network():
    net = Net()
    with pytest.raises(gofile.GofileError) as info:
        gofile.list_share("https://gofile.io/d/ObvAFn9h?x=1", settings(), session=net)
    assert info.value.user_message == gofile.UNSUPPORTED_LINK and not net.calls


# --- C. selection dialog ---------------------------------------------------

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Nothing here may reach Gofile; only loopback HTTP is allowed."""
    import requests
    from urllib.parse import urlsplit
    original = requests.sessions.Session.request

    def refuse(session, method, url, **kwargs):
        if urlsplit(url).hostname == "127.0.0.1" and urlsplit(url).scheme == "http":
            return original(session, method, url, **kwargs)
        pytest.fail("Unexpected real network request")
    monkeypatch.setattr(requests.sessions.Session, "request", refuse)


def three_files():
    return gofile.list_share(SHARE, settings(), session=tree_net())


def test_dialog_shows_real_names_sizes_and_the_selection_contract():
    from cove.dialogs import GofileFilesDialog
    result = three_files()
    dlg = GofileFilesDialog(result, "/tmp/chosen")
    assert dlg.windowTitle() == "Gofile Files" and dlg.name_edit.text() == "Gofile share"
    assert dlg.download_button().text() == "Download Selected"
    assert dlg.selected_count() == 3 and "3 of 3" in dlg.summary_text()
    # All selected -> all files.
    dlg.confirm()
    assert dlg.result() == QDialog.Accepted and dlg.result_selection() == result.files

    dlg = GofileFilesDialog(result, "/tmp/chosen")
    dlg.select_none()
    assert not dlg.download_button().isEnabled() and "0 of 3" in dlg.summary_text()
    dlg.confirm()  # None selected -> no downloads.
    assert dlg.result_selection() == ()

    dlg = GofileFilesDialog(result, "/tmp/chosen")
    dlg.leaves()[0][0].setCheckState(0, Qt.Unchecked)  # Proper subset.
    dlg.confirm()
    assert dlg.result_selection() == result.files[1:]

    for close in (lambda d: d.reject(), lambda d: d.done(QDialog.Rejected)):
        dlg = GofileFilesDialog(result, "/tmp/chosen")
        close(dlg)  # Cancel / dialog closed -> zero downloads.
        assert dlg.result_selection() == ()


def test_single_file_gets_a_confirmation_step_with_its_real_name():
    from cove.dialogs import GofileFilesDialog
    net = Net(api={("ObvAFn9h", 1): ok(file_node(F1, "Archive.zip", 13_314_398_617))})
    dlg = GofileFilesDialog(gofile.list_share(SHARE, settings(), session=net), "/tmp")
    assert dlg.selected_count() == 1 and dlg.download_button().isEnabled()
    assert dlg.result_selection() == ()  # nothing happens until the user confirms
    labels = [dlg.tree.topLevelItem(i).text(0) for i in range(dlg.tree.topLevelItemCount())]
    assert labels == ["Archive.zip"]


# --- D. queue --------------------------------------------------------------


def test_selected_rows_carry_identity_name_size_and_destination(queue_env, tmp_path):
    from cove.queue import SOURCE_GOFILE_FILE
    queue, rpc, _ = queue_env()
    files = three_files().files
    assert queue.add_gofile_files([], str(tmp_path)) == []
    first = queue.add_gofile_files(files[:1], str(tmp_path))
    assert len(first) == 1
    ids = first + queue.add_gofile_files(files, str(tmp_path))
    assert len(ids) == 3 and queue.add_gofile_files(files, str(tmp_path)) == []  # no duplicates
    with db.connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM downloads ORDER BY id")]
    assert TOKEN not in repr(rows) and "store-1" not in repr(rows)
    assert {r["url"] for r in rows} == {gofile.file_ref("ObvAFn9h", f.file_id) for f in files}
    assert all(r["source_type"] == SOURCE_GOFILE_FILE and r["status"] == "queued"
               and r["backend"] == "aria2" for r in rows)
    by_name = {r["filename"]: r for r in rows}
    assert by_name["A.zip"]["total_bytes"] == 1_200 and by_name["B.mp4"]["total_bytes"] == 2_400
    assert by_name["C.txt"]["total_bytes"] == 10
    assert Path(by_name["C.txt"]["out_dir"]) == tmp_path / "Sub"
    assert Path(by_name["A.zip"]["out_dir"]) == tmp_path
    assert not rpc.added


# --- H. paths cannot escape ------------------------------------------------


def entry(path, **kw):
    name = kw.pop("name", path[-1] if path else "")
    return gofile.GofileFile(name, tuple(path), kw.pop("size", 1), kw.pop("file_id", F1), kw.pop("share", "ObvAFn9h"))


@pytest.mark.parametrize("bad", [
    entry(("..", "x")), entry(("/abs", "x")), entry(("a\\b",)), entry(("a", "..")), entry(("x",), name="y"),
    entry(("x",), file_id="../../etc"), entry(("x",), share="bad!"), entry(("x",), size=-1),
    entry(("x",), size=True), entry(()), entry(("a",) * 40),
])
def test_forged_entries_are_refused_atomically(queue_env, tmp_path, bad):
    queue, _, _ = queue_env()
    with pytest.raises(gofile.GofileError):
        queue.add_gofile_files([entry(("good.bin",), file_id=F2), bad], str(tmp_path))
    assert not queue.tasks
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM downloads").fetchone()[0] == 0


@pytest.mark.parametrize("paths", [
    [("A.bin",), ("a.BIN",)],            # case-insensitive collision
    [("Dir",), ("dir", "x.bin")],        # file shadows a directory
    [("dir", "x.bin"), ("DIR",)],        # directory shadows a file
])
def test_conflicting_selections_are_atomic(queue_env, tmp_path, paths):
    queue, _, _ = queue_env()
    entries = [entry(p, file_id=f"{i:08d}-aaaa") for i, p in enumerate(paths)]
    with pytest.raises(gofile.GofileError):
        queue.add_gofile_files(entries, str(tmp_path))
    assert not queue.tasks


def test_symlinked_subfolder_cannot_redirect_the_destination(queue_env, tmp_path):
    queue, _, _ = queue_env()
    outside, base = tmp_path / "outside", tmp_path / "chosen"
    outside.mkdir(), base.mkdir()
    (base / "Sub").symlink_to(outside, target_is_directory=True)
    with pytest.raises(gofile.GofileError):
        queue.add_gofile_files([entry(("Sub", "x.bin"))], str(base))
    assert not queue.tasks


# --- E. file bytes, not a landing page -------------------------------------


@pytest.mark.parametrize("name", ["movie.mp4", "Archive.zip", "doc.pdf", "noextension"])
def test_landing_page_is_never_the_named_file(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(LANDING)
    with pytest.raises(gofile.GofileError) as info:
        gofile.verify_download(path, len(LANDING), name)
    assert info.value.user_message == gofile.LANDING_PAGE
    # Even when no size was ever learned, the page is recognised from its bytes.
    with pytest.raises(gofile.GofileError):
        gofile.verify_download(path, None, name)


@pytest.mark.parametrize("body", [
    b'{\n  "status": "error-token",\n  "data": {}\n}', b'{"data": {}, "status": "error-notPremium"}',
    b'{ "status" :\t"error-rateLimit" }', b'{"metadata":{},"data":{},"status":"error-notFound"}',
])
def test_api_error_envelopes_are_recognised_whatever_the_formatting(tmp_path, body):
    path = tmp_path / "a.bin"
    path.write_bytes(body)
    assert gofile.looks_like_web_page(body, "a.bin")
    with pytest.raises(gofile.GofileError) as info:
        gofile.verify_download(path, len(body), "a.bin")
    assert info.value.user_message == gofile.LANDING_PAGE
    net = resolve_net(response=Resp(status=200, headers={"Content-Type": "application/json"}, body=body))
    with pytest.raises(gofile.GofileError):
        gofile.resolve_file("ObvAFn9h", F1, "a.bin", settings(), session=net)


@pytest.mark.parametrize("head", [b"<!DOCTYPE HTML>", b"\xef\xbb\xbf  \n<html>", b"<script>x</script>",
                                  b'{"status":"error-token","data":{}}', b"<body>"])
def test_page_and_error_shapes_are_recognised(tmp_path, head):
    path = tmp_path / "x.bin"
    path.write_bytes(head + b" " * 10)
    with pytest.raises(gofile.GofileError):
        gofile.verify_download(path, None, "x.bin")


GENUINE_PAGE = b"<!doctype html><html><body>My own saved page</body></html>"


def test_genuine_files_pass_including_html_and_json_that_were_actually_shared(tmp_path):
    binary = tmp_path / "a.zip"
    binary.write_bytes(b"PK\x03\x04" + bytes(range(256)))
    gofile.verify_download(binary, binary.stat().st_size, "a.zip")
    page = tmp_path / "saved.html"
    page.write_bytes(GENUINE_PAGE)
    gofile.verify_download(page, len(GENUINE_PAGE), "saved.html")
    data = tmp_path / "log.json"
    data.write_bytes(b'{"status":"ok","items":[]}')
    gofile.verify_download(data, None, "log.json")
    text = tmp_path / "notes.txt"
    text.write_bytes(b"a < b and <b>bold</b>")
    gofile.verify_download(text, None, "notes.txt")


@pytest.mark.parametrize("name", ["saved.html", "page.htm", "x.xhtml", "log.json", "movie.mp4"])
@pytest.mark.parametrize("body", [LANDING, b'{"status":"error-token","data":{}}', b'{"status": "error-notPremium"}'])
def test_gofile_pages_and_errors_are_refused_under_any_filename(tmp_path, name, body):
    """A response cannot dodge the guard by choosing an HTML/JSON-looking name."""
    path = tmp_path / name
    path.write_bytes(body)
    with pytest.raises(gofile.GofileError) as info:
        gofile.verify_download(path, len(body), name)
    assert info.value.user_message == gofile.LANDING_PAGE
    assert gofile.looks_like_web_page(body, name)


def test_size_must_match_what_gofile_reported(tmp_path):
    path = tmp_path / "a.zip"
    path.write_bytes(b"PK" * 100)
    with pytest.raises(gofile.GofileError) as info:
        gofile.verify_download(path, 12_400_000_000, "a.zip")
    assert info.value.user_message == gofile.SIZE_MISMATCH
    gofile.verify_download(path, 200, "a.zip")
    gofile.verify_download(path, None, "a.zip")  # None = never learned; the only unchecked case


def test_a_zero_byte_file_is_a_real_size_not_an_unknown_one(tmp_path):
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    gofile.verify_download(empty, 0, "empty.bin")          # genuinely empty: fine
    wrong = tmp_path / "wrong.bin"
    wrong.write_bytes(b"PK" * 100)
    with pytest.raises(gofile.GofileError) as info:       # reported empty, got bytes: refuse
        gofile.verify_download(wrong, 0, "wrong.bin")
    assert info.value.user_message == gofile.SIZE_MISMATCH
    net = Net(api={(F1, None): ok(file_node(F1, "empty.bin", 0))},
              downloads={link_for(F1, "empty.bin"): good_download()})
    assert gofile.resolve_file("ObvAFn9h", F1, "empty.bin", settings(), session=net).size == 0
    assert "Range" not in net.calls[1][1]["headers"]       # an empty file has no satisfiable range


def test_an_empty_file_resolves_and_a_range_enforcing_server_cannot_break_it():
    link = link_for(F1, "empty.bin")
    strict = Resp(status=416, headers={"Content-Range": "bytes */0"})
    net = Net(api={(F1, None): ok(file_node(F1, "empty.bin", 0))}, downloads={link: Resp(status=200, body=b"")})
    assert gofile.resolve_file("ObvAFn9h", F1, "empty.bin", settings(), session=net).size == 0
    # A non-empty file is still probed with a range, and a 416 there is a real failure.
    net = Net(api={(F1, None): ok(file_node(F1, "a.bin", 5))}, downloads={link_for(F1, "a.bin"): strict})
    with pytest.raises(gofile.GofileError):
        gofile.resolve_file("ObvAFn9h", F1, "a.bin", settings(), session=net)


def test_verification_reads_only_a_bounded_head(tmp_path, monkeypatch):
    path = tmp_path / "big.bin"
    path.write_bytes(b"\x00" * 100_000)
    reads = []
    real_open = open

    def spy(*args, **kwargs):
        handle = real_open(*args, **kwargs)
        original = handle.read
        handle.read = lambda n=-1: reads.append(n) or original(n)
        return handle
    monkeypatch.setattr("builtins.open", spy)
    gofile.verify_download(path, 100_000, "big.bin")
    assert reads == [gofile.PROBE_BYTES]


def resolve_net(link=None, response=None, name="a.bin", extra=None):
    link = link or link_for(F1, name)
    return Net(api={(F1, None): ok(file_node(F1, name, 14, link=link, **(extra or {})))},
               downloads={link: response if response is not None else good_download()})


def test_resolve_file_returns_a_verified_fresh_request():
    net = resolve_net()
    result = gofile.resolve_file("ObvAFn9h", F1, "a.bin", settings(), session=net)
    assert result.url == link_for(F1, "a.bin") and result.size == 14
    assert not hasattr(result, "headers")                # aria2 is never handed a Cookie header
    probe = net.calls[1][1]
    assert probe["headers"]["Cookie"] == f"accountToken={TOKEN}" and probe["stream"] is True
    assert probe["headers"]["Range"] == f"bytes=0-{gofile.PROBE_BYTES - 1}" and probe["allow_redirects"] is False


@pytest.mark.parametrize("response, message", [
    (Resp(status=200, headers={"Content-Type": "text/html"}, body=LANDING), gofile.LANDING_PAGE),
    (Resp(status=200, headers={}, body=LANDING), gofile.LANDING_PAGE),
    (Resp(status=200, headers={"Content-Type": "text/html; charset=utf-8"}, body=b"plain"), gofile.LANDING_PAGE),
    (Resp(status=206, body=b'{"status":"error-token"}'), gofile.LANDING_PAGE),
    (Resp(status=401, body=LANDING), gofile.FILE_DENIED),
    (Resp(status=403, body=LANDING), gofile.FILE_DENIED),
    (Resp(status=404), gofile.FILE_GONE), (Resp(status=410), gofile.FILE_GONE),
    (Resp(status=429), gofile.RATE_LIMITED),
    (Resp(status=500), gofile.UNAVAILABLE), (Resp(status=204), gofile.UNAVAILABLE),
    (Resp(status=302), gofile.FILE_DENIED),   # redirect with no Location
])
def test_bad_download_responses_fail_clearly_and_close(response, message):
    net = resolve_net(response=response)
    with pytest.raises(gofile.GofileError) as info:
        gofile.resolve_file("ObvAFn9h", F1, "a.bin", settings(), session=net)
    assert info.value.user_message == message and response.closed


def test_a_real_html_file_is_not_second_guessed():
    net = resolve_net(name="saved.html", response=Resp(status=200, headers={"Content-Type": "text/html"}, body=GENUINE_PAGE))
    assert gofile.resolve_file("ObvAFn9h", F1, "saved.html", settings(), session=net).size == 14


@pytest.mark.parametrize("link", [
    "http://store-1.gofile.io/download/web/x/a.bin", "https://evil.test/a.bin",
    "https://gofile.io.evil.test/a.bin", "https://evilgofile.io/a.bin",
    "https://u:p@store-1.gofile.io/a.bin", "https://store-1.gofile.io:8443/a.bin",
    "ftp://store-1.gofile.io/a.bin", "", None, 5,
])
def test_token_is_never_sent_to_a_non_gofile_host(link):
    net = Net(api={(F1, None): ok(file_node(F1, "a.bin", 14, link=link))})
    with pytest.raises(gofile.GofileError):
        gofile.resolve_file("ObvAFn9h", F1, "a.bin", settings(), session=net)
    assert len(net.calls) == 1  # only the API call; no download request, so no cookie sent anywhere


@pytest.mark.parametrize("link", [
    "https://attacker.example\\.gofile.io/file",           # Requests sends this to attacker.example
    "https://attacker.example\\@store-1.gofile.io/file",
    "https://store-1.gofile.io\\.evil.test/file",
    "https://store-1.gofile.io/a b", "https://store-1.gofile.io/a\tb", "https://store-1.gofile.io/a\nb",
    "https://st\u00f6re.gofile.io/file",
])
def test_parser_discrepancies_never_receive_the_token(link):
    """urllib and Requests disagree on these; the token is only sent when they agree on Gofile."""
    net = Net(api={(F1, None): ok(file_node(F1, "a.bin", 14, link=link))})
    with pytest.raises(gofile.GofileError):
        gofile.resolve_file("ObvAFn9h", F1, "a.bin", settings(), session=net)
    assert len(net.calls) == 1
    if "\t" in link or "\n" in link:
        return  # urljoin itself strips these from a Location, leaving an ordinary Gofile URL
    start = link_for(F1, "a.bin")
    hop = Net(api={(F1, None): ok(file_node(F1, "a.bin", 14))},
              downloads={start: Resp(status=302, headers={"Location": link})})
    with pytest.raises(gofile.GofileError):
        gofile.resolve_file("ObvAFn9h", F1, "a.bin", settings(), session=hop)
    assert [u for u, _ in hop.calls][-1] == start       # the hostile hop was never requested


def test_a_unicode_file_name_in_the_path_is_still_a_valid_gofile_link():
    name = "caf\u00e9 \u65e5\u672c.bin".replace(" ", "_")
    link = link_for(F1, name)
    net = Net(api={(F1, None): ok(file_node(F1, name, 14, link=link))}, downloads={link: good_download()})
    assert gofile.resolve_file("ObvAFn9h", F1, name, settings(), session=net).url == link


def test_redirects_stay_on_gofile_hosts_and_are_followed_by_hand():
    start, end = link_for(F1, "a.bin"), "https://store-2.gofile.io/download/web/final/a.bin"
    net = Net(api={(F1, None): ok(file_node(F1, "a.bin", 14))},
              downloads={start: Resp(status=302, headers={"Location": end}), end: good_download()})
    result = gofile.resolve_file("ObvAFn9h", F1, "a.bin", settings(), session=net)
    assert result.url == end  # aria2 is handed the final location, not a redirecting one

    evil = Net(api={(F1, None): ok(file_node(F1, "a.bin", 14))},
               downloads={start: Resp(status=302, headers={"Location": "https://evil.test/steal"})})
    with pytest.raises(gofile.GofileError):
        gofile.resolve_file("ObvAFn9h", F1, "a.bin", settings(), session=evil)
    assert [u for u, _ in evil.calls] == [f"https://api.gofile.io/contents/{F1}", start]

    loop = Net(api={(F1, None): ok(file_node(F1, "a.bin", 14))},
               downloads={start: Resp(status=302, headers={"Location": start})})
    with pytest.raises(gofile.GofileError):
        gofile.resolve_file("ObvAFn9h", F1, "a.bin", settings(), session=loop)


@pytest.mark.parametrize("data", [
    folder_node(F1, "x", []), file_node("other-id-12345", "a.bin", 1), file_node(F1, "a.bin", 1, isFrozen=True),
])
def test_resolve_refuses_a_file_that_is_not_what_was_listed(data):
    net = Net(api={(F1, None): ok(data)})
    with pytest.raises(gofile.GofileError) as info:
        gofile.resolve_file("ObvAFn9h", F1, "a.bin", settings(), session=net)
    assert info.value.user_message == gofile.FILE_GONE and len(net.calls) == 1


# --- G. launch, retry and restart ------------------------------------------


def hold_cookie(queue, token=TOKEN):
    """Make the queue's engine report that it loaded a jar for `token` (as the real daemon does)."""
    fingerprint = gofile.token_fingerprint(token) if token else ""
    queue.rpc.gofile_cookie_fingerprint = lambda: fingerprint


def one_task(queue, tmp_path, size=14):
    ids = queue.add_gofile_files([entry(("a.bin",), size=size)], str(tmp_path))
    return queue.tasks[ids[0]]


def test_launch_hands_aria2_only_the_resolved_link_and_never_a_cookie_header(queue_env, monkeypatch, tmp_path):
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN, connections_per_server=4)
    hold_cookie(queue)
    task = one_task(queue, tmp_path)
    net = resolve_net()
    monkeypatch.setattr(queue, "_bound_session", lambda: net)
    sent, heads = [], []
    monkeypatch.setattr(rpc, "add_uri", lambda *a, **k: sent.append((a, k)) or "gid-1")
    net.head = lambda *a, **k: heads.append(a)
    inline(queue)
    queue._launch(task)
    assert not heads  # no unauthenticated HEAD against the download host
    (args, kwargs), = sent
    assert args[0] == [link_for(F1, "a.bin")] and args[4] == "a.bin"
    assert "headers" not in kwargs and TOKEN not in repr((args, kwargs))
    task.clear_debrid()                      # end of this launch: nothing secret outlives it
    assert task.resolved_url == "" and task.expected_size is None
    assert task.url == gofile.file_ref("ObvAFn9h", F1) and task.total_bytes == 14
    assert TOKEN not in repr(task) and "store-1" not in task.url


def test_every_launch_asks_gofile_for_a_fresh_link(queue_env, monkeypatch, tmp_path):
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN)
    hold_cookie(queue)
    task = one_task(queue, tmp_path)
    first, second = link_for(F1, "a.bin"), "https://store-2.gofile.io/download/web/new/a.bin"
    net = Net(api={(F1, None): [ok(file_node(F1, "a.bin", 14, link=first)),
                                ok(file_node(F1, "a.bin", 14, link=second))]},
              downloads={first: good_download(), second: good_download()})
    monkeypatch.setattr(queue, "_bound_session", lambda: net)
    assert queue._resolve_debrid(task) == first
    queue._persist(task)
    with db.connect() as conn:
        row = dict(conn.execute("SELECT * FROM downloads WHERE id=?", (task.id,)).fetchone())
    assert row["url"] == gofile.file_ref("ObvAFn9h", F1) and "store" not in repr(row) and TOKEN not in repr(row)
    # A new process: the row survives, the transient request does not.
    fresh, _, _ = queue_env(gofile_api_token=TOKEN)
    hold_cookie(fresh)
    restored = next(t for t in fresh.tasks.values() if t.id == task.id)
    assert restored.resolved_url == "" and restored.expected_size is None
    monkeypatch.setattr(fresh, "_bound_session", lambda: net)
    assert fresh._resolve_debrid(restored) == second
    assert len(net.api_calls()) == 2


@pytest.mark.parametrize("resp, message", [
    (Resp({"status": "error-notFound", "data": {}}, 404), gofile.NOT_FOUND),
    (Resp({"status": "error-token", "data": {}}, 401), gofile.BAD_TOKEN),
])
def test_relaunch_failure_is_a_clear_task_error_not_a_download(queue_env, monkeypatch, tmp_path, resp, message):
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN)
    hold_cookie(queue)
    task = one_task(queue, tmp_path)
    monkeypatch.setattr(queue, "_bound_session", lambda: Net(api={(F1, None): resp}))
    inline(queue)
    queue._launch(task)
    assert task.status == "error" and task.error == message and not rpc.added


def test_launch_without_a_token_explains_what_to_do(queue_env, monkeypatch, tmp_path):
    queue, rpc, _ = queue_env()
    task = one_task(queue, tmp_path)
    monkeypatch.setattr(queue, "_bound_session", lambda: Net())
    inline(queue)
    queue._launch(task)
    assert task.status == "error" and task.error == gofile.NEEDS_TOKEN and not rpc.added


def test_pause_and_concurrency_use_the_normal_queue(queue_env, monkeypatch, tmp_path):
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN, max_concurrent=1)
    files = [entry((f"f{i}.bin",), file_id=f"{i:08d}-aaaa") for i in range(3)]
    ids = queue.add_gofile_files(files, str(tmp_path))
    assert len(ids) == 3 and all(queue.tasks[i].status == "queued" for i in ids)
    assert not rpc.added  # scheduler is held; nothing starts behind the user's back
    assert queue._active_count() == 0


# --- I. flow: cancel, stale results, closing --------------------------------

def flow_owner(queue):
    import cove.main_window as mw
    owner = SimpleNamespace(queue=queue, settings=queue.settings, _preflights_closed=False)
    owner._gofile_share_preflight = lambda *a: mw.MainWindow._gofile_share_preflight(owner, *a)
    owner._resolve_gofile_share = lambda *a: mw.MainWindow._resolve_gofile_share(owner, *a)
    owner.add_urls_checked = lambda *a: mw.MainWindow.add_urls_checked(owner, *a)
    return owner


class Choice:
    """Stand-in for the selection dialog."""

    Accepted = QDialog.Accepted

    def __init__(self, accept=True, count=1, files=()):
        self.accept, self.count, self.files = accept, count, files

    def __call__(self, listing, dest, parent=None):
        self.listing, self.dest = listing, dest
        return self

    def exec(self):
        return QDialog.Accepted if self.accept else QDialog.Rejected

    def result_selection(self):
        return self.listing.files[:self.count]


@pytest.mark.parametrize("accept", [False, True])
def test_main_flow_only_commits_what_was_confirmed(queue_env, monkeypatch, tmp_path, accept):
    import cove.main_window as mw
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN)
    inline(queue)
    result = three_files()
    monkeypatch.setattr(gofile, "list_share", lambda *a, **k: result)
    choice = Choice(accept=accept, count=2)
    monkeypatch.setattr(mw, "GofileFilesDialog", choice)
    real_loading = mw.MagnetMetadataDialog
    monkeypatch.setattr(mw, "MagnetMetadataDialog", lambda *a: real_loading())
    monkeypatch.setattr(queue, "add_url", lambda *a, **k: pytest.fail("share in plain intake"))
    ids = flow_owner(queue).add_urls_checked([SHARE], str(tmp_path))
    assert len(ids) == (2 if accept else 0) and len(queue.tasks) == len(ids)
    assert choice.dest == str(tmp_path)  # the configured destination is preserved
    assert not rpc.added
    assert {t.filename for t in queue.tasks.values()} == ({"A.zip", "B.mp4"} if accept else set())


def test_cancel_during_listing_cannot_create_a_task_later(queue_env, monkeypatch, tmp_path):
    import cove.main_window as mw
    queue, _, _ = queue_env(gofile_api_token=TOKEN)
    finishes = []
    owner = flow_owner(queue)
    monkeypatch.setattr(mw, "GofileFilesDialog", lambda *a: pytest.fail("Late selection dialog"))
    monkeypatch.setattr(queue, "add_gofile_files", lambda *a: pytest.fail("Late commit"))
    real_loading = mw.MagnetMetadataDialog

    def loading(*args):
        dlg = real_loading()
        finish = dlg.finish

        def record_finish():
            finishes.append(True)
            finish()
        dlg.finish = record_finish
        return dlg
    monkeypatch.setattr(mw, "MagnetMetadataDialog", loading)

    def spawn(fn, *, on_done, **kwargs):
        # Cancel first, then deliver a success before resolve can return.
        assert owner._gofile_open and owner._gofile_dialog is not None
        owner._gofile_dialog.reject()
        assert finishes == [True]
        on_done((three_files(), ""))
        assert finishes == [True]
    monkeypatch.setattr(queue, "_spawn", spawn)
    assert mw.MainWindow.add_urls_checked(owner, [SHARE], str(tmp_path)) == []
    assert not queue.tasks and finishes == [True] and owner._gofile_dialog is None


def test_closing_cove_during_listing_ignores_the_late_delivery(queue_env, monkeypatch):
    import cove.main_window as mw
    queue, _, _ = queue_env(gofile_api_token=TOKEN)
    owner = flow_owner(queue)
    owner.discard_magnet_requests = lambda: None
    owner.discard_torrent_preflights = lambda: None
    owner._torrent_contents_dialog = None
    calls = []
    monkeypatch.setattr(queue, "_spawn", lambda fn, **kw: calls.append((fn, kw)))
    real_loading = mw.MagnetMetadataDialog

    def loading(*args):
        dlg = real_loading()
        QTimer.singleShot(0, lambda: mw.MainWindow.close_interactive_preflights(owner))
        return dlg
    monkeypatch.setattr(mw, "MagnetMetadataDialog", loading)
    assert owner.add_urls_checked([SHARE]) == []
    assert owner._preflights_closed and owner._gofile_dialog is None
    calls[0][1]["on_done"]((three_files(), ""))
    assert calls[0][0]() == (None, "")  # cancelled before the worker ran: no request at all
    assert not queue.tasks


def test_reentrant_share_requests_do_not_nest(queue_env):
    import cove.main_window as mw
    queue, _, _ = queue_env(gofile_api_token=TOKEN)
    owner = flow_owner(queue)
    seen = []

    def resolve(url, out_dir):
        seen.append(url)
        if len(seen) == 1:
            assert owner._gofile_share_preflight("https://gofile.io/d/Second12", out_dir) == []
        return []
    owner._resolve_gofile_share = resolve
    assert mw.MainWindow._gofile_share_preflight(owner, SHARE, None) == []
    assert seen == [SHARE, "https://gofile.io/d/Second12"]


@pytest.mark.parametrize("failure", ["token", "network", "unexpected"])
def test_listing_failures_reach_the_user_as_fixed_text(queue_env, monkeypatch, failure):
    import cove.main_window as mw
    queue, _, _ = queue_env(gofile_api_token="" if failure == "token" else TOKEN)
    session = Net(api={("ObvAFn9h", 1): ConnectionError(f"{SHARE} {TOKEN}")})
    monkeypatch.setattr(queue, "_bound_session", lambda: session)
    if failure == "unexpected":
        monkeypatch.setattr(gofile, "list_share", lambda *a, **k: 1 / 0)
    real_loading = mw.MagnetMetadataDialog
    monkeypatch.setattr(mw, "MagnetMetadataDialog", lambda *a: real_loading())
    monkeypatch.setattr(mw, "GofileFilesDialog", lambda *a: pytest.fail("dialog for a failure"))
    warnings = []
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a: warnings.append(a[1:]))
    assert flow_owner(queue).add_urls_checked([SHARE]) == [] and not queue.tasks
    assert warnings == [("Gofile share unavailable",
                         {"token": gofile.NEEDS_TOKEN, "network": gofile.UNAVAILABLE,
                          "unexpected": gofile.UNAVAILABLE}[failure])]
    assert TOKEN not in repr(warnings)


def test_commit_failure_is_reported_and_creates_nothing(queue_env, monkeypatch, tmp_path):
    import cove.main_window as mw
    queue, _, _ = queue_env(gofile_api_token=TOKEN)
    inline(queue)
    result = three_files()
    monkeypatch.setattr(gofile, "list_share", lambda *a, **k: result)
    choice = Choice(count=3)
    monkeypatch.setattr(mw, "GofileFilesDialog", choice)
    bad = gofile.GofileFile("x", ("..", "x"), 1, F1, "ObvAFn9h")
    choice.result_selection = lambda: (result.files[0], bad)
    real_loading = mw.MagnetMetadataDialog
    monkeypatch.setattr(mw, "MagnetMetadataDialog", lambda *a: real_loading())
    warnings = []
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a: warnings.append(a[1:]))
    assert flow_owner(queue).add_urls_checked([SHARE], str(tmp_path)) == []
    assert warnings == [("Gofile share unavailable", gofile.BAD_LISTING)] and not queue.tasks


# --- J. unrelated links keep their behaviour -------------------------------


@pytest.mark.parametrize("url", [
    "https://example.test/file.zip", "https://gofile.io/", "https://gofile.io/premium",
    "https://store-1.gofile.io/download/web/id/archive.zip", "https://gofile.io.evil.test/d/ObvAFn9h",
])
def test_unrelated_links_are_still_ordinary_downloads(queue_env, url):
    queue, _, _ = queue_env()
    errors = []
    queue.error.connect(errors.append)
    prepared = queue.prepare_url(url)
    assert prepared is not None and prepared.source_type == "" and not errors


def test_persisted_plain_rows_for_other_hosts_still_launch(queue_env):
    queue, rpc, _ = queue_env(intelligent_segments=False)
    task = queue.tasks[queue.add_url("https://example.test/file.zip")]
    inline(queue)
    queue._launch(task)
    assert task.status != "error" and rpc.added[0]["uris"] == ["https://example.test/file.zip"]


def test_external_aria2_share_page_is_never_adopted(queue_env):
    queue, rpc, _ = queue_env()
    source = open(__import__("cove.queue", fromlist=["x"]).__file__, encoding="utf-8").read()
    assert "gofile.is_share_url(uri.get" in source  # the MEGA-style adoption guard is wired


# --- end to end: real aria2 against a loopback "Gofile storage" ------------


def has_token(cookie_header):
    """A Cookie header as any standard parser reads it (aria2 appends a trailing ';')."""
    return f"accountToken={TOKEN}" in [part.strip() for part in cookie_header.split(";")]

from tests.test_mega_folder import pump_until  # noqa: E402


@pytest.fixture
def real_gofile_factory(tmp_path, monkeypatch, queue_env):
    """Cove's real aria2 daemon plus a loopback storage host that demands the account cookie."""
    import hashlib
    import os
    import shutil
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit
    import requests
    from cove.aria2 import Aria2RPC

    executable = shutil.which("aria2c")
    if executable is None:
        pytest.skip("real aria2c is not installed")
    payload = bytes(range(256)) * 2048
    state = SimpleNamespace(payload=payload, sha256=hashlib.sha256(payload).hexdigest(), mode="good",
                            requests=[], hits=0, lock=threading.Lock(),
                            started=threading.Event(), release=threading.Event())
    state.requests_off_host = lambda: [r for r in state.requests if r[1] == "/steal"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self.serve(False)

        def do_GET(self):
            self.serve(True)

        def serve(self, body):
            cookie = self.headers.get("Cookie", "")
            with state.lock:
                state.requests.append((self.command, self.path, cookie, self.headers.get("User-Agent", "")))
                if self.path == "/file" and self.command == "GET":
                    state.hits += 1
                hit = state.hits
            if self.path == "/file" and state.mode == "redirect" and hit > 1:
                self.send_response(302)      # the probe saw bytes; the real transfer is bounced off-host
                self.send_header("Location", f"http://localhost:{server.server_port}/steal")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            if self.path == "/steal":
                content, ctype = payload, "application/octet-stream"
            elif self.path == "/plain.html":
                content, ctype = LANDING, "text/html"
            elif self.path != "/file" or not has_token(cookie):
                content, ctype = LANDING, "text/html"
            elif state.mode == "empty":
                content, ctype = b"", "application/octet-stream"
            elif state.mode == "swap" and hit > 1:      # probe saw bytes; the download gets a page
                content, ctype = LANDING, "text/html"
            elif state.mode == "short" and hit > 1:     # not HTML, but not the file either
                content, ctype = b"\x00" * 100, "application/octet-stream"
            else:
                content, ctype = payload, "application/octet-stream"
            status = 200 if has_token(cookie) or self.path in ("/plain.html", "/steal") else 403
            start, end = 0, len(content) - 1
            ranged = self.headers.get("Range", "")
            if ranged.startswith("bytes=") and status == 200 and ctype != "text/html" and content:
                first, _, last = ranged[6:].partition("-")
                start, end = int(first or 0), min(int(last), end) if last else end
            try:
                self.send_response(206 if end - start + 1 != len(content) else status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Connection", "close")
                if end - start + 1 != len(content):
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(content)}")
                self.end_headers()
                if body and state.mode == "slow" and self.path == "/file" and hit > 1:
                    half = start + (end - start + 1) // 2
                    self.wfile.write(content[start:half])
                    self.wfile.flush()
                    state.started.set()
                    state.release.wait(8)
                    self.wfile.write(content[half:end + 1])
                elif body:
                    self.wfile.write(content[start:end + 1])
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = None
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            rpc_port = sock.getsockname()[1]
    except PermissionError:
        if server is not None:
            server.server_close()
        pytest.skip("loopback sockets are unavailable in this execution environment")
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    serving.start()
    from cove import aria2
    for name in list(os.environ):
        if "proxy" in name.lower():
            monkeypatch.delenv(name, raising=False)
    data_dir = tmp_path / "aria2-data"
    data_dir.mkdir(mode=0o700)
    monkeypatch.setattr(aria2, "DATA_DIR", data_dir)
    monkeypatch.setattr(aria2, "ARIA2_LOG", data_dir / "aria2.log")
    base_url = f"http://127.0.0.1:{server.server_port}"
    # Loopback stands in for *.gofile.io: same jar code, scoped to the loopback host.
    monkeypatch.setattr(gofile, "_download_url", lambda v: v)
    monkeypatch.setattr(gofile, "COOKIE_DOMAIN", "127.0.0.1")
    monkeypatch.setattr(gofile, "COOKIE_SECURE", False)
    engine = SimpleNamespace(daemon=None, rpc=None)
    queues = []

    class Bridge:
        """Fake Gofile API; the storage host is the real loopback server."""

        def __init__(self, size):
            self.real, self.size = requests.Session(), size

        def get(self, url, **kwargs):
            if urlsplit(url).netloc == "api.gofile.io":
                return ok(file_node(F1, "movie.mp4", self.size, link=f"{base_url}/file"))
            return self.real.get(url, **kwargs)

    def start(token=TOKEN, secure=False):
        """Start Cove's own daemon (the real code path that writes and loads the jar)."""
        monkeypatch.setattr(gofile, "COOKIE_SECURE", secure)
        settings_ = Settings(rpc_port=rpc_port, download_dir=str(tmp_path / "engine-dir"), gofile_api_token=token,
                             connections_per_server=4)
        engine.daemon = aria2.Aria2Daemon(settings_)
        engine.settings = settings_
        engine.daemon.start()
        engine.rpc = Aria2RPC(settings_, timeout=2, secret_provider=engine.daemon.active_rpc_secret,
                              cookie_fingerprint_provider=engine.daemon.gofile_cookie_fingerprint)
        state.rpc = engine.rpc
        return engine.daemon

    def build(size=None, restore=False, rpc=None):
        queue, _, _ = queue_env(connections_per_server=4, gofile_api_token=TOKEN, intelligent_segments=False)
        queue.rpc = rpc or engine.rpc
        queues.append(queue)
        monkeypatch.setattr(queue, "_bound_session", lambda: Bridge(len(payload) if size is None else size))
        if restore:
            return queue, next(iter(queue.tasks.values()))
        item = gofile.GofileFile("movie.mp4", ("movie.mp4",), len(payload) if size is None else size, F1, "ObvAFn9h")
        return queue, queue.tasks[queue.add_gofile_files([item], str(tmp_path / "downloads"))[0]]

    state.build, state.start, state.engine, state.base_url = build, start, engine, base_url
    state.data_dir = data_dir
    try:
        yield state
    finally:
        state.release.set()
        for queue in queues:
            queue._scheduler_allows = False
            queue._poll.stop()
            queue._ext_poll.stop()
        if engine.daemon is not None:
            engine.daemon.stop()
        for queue in queues:
            queue._pool.waitForDone(2000)
        QApplication.processEvents()
        if engine.rpc is not None:
            engine.rpc.close()
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)
        assert not serving.is_alive()


@pytest.fixture
def real_gofile_io(real_gofile_factory):
    real_gofile_factory.start()
    return real_gofile_factory


def run_to_end(io, queue, task):
    queue._scheduler_allows = True
    queue._launch(task)

    def finished():
        if task.gid and task.id not in queue._publishing and task.status not in ("completed", "error"):
            queue._apply_status(task.id, io.rpc.tell_status(task.gid))
        return task.status in ("completed", "error")
    pump_until(finished)


def test_real_aria2_downloads_the_genuine_bytes_with_the_account_cookie(real_gofile_io, tmp_path):
    import hashlib
    io = real_gofile_io
    queue, task = io.build()
    events = []
    queue._diag = lambda *a, **k: events.append((a, k))
    run_to_end(io, queue, task)
    assert task.status == "completed", task.error
    published = tmp_path / "downloads" / "movie.mp4"
    assert Path(task.output_path) == published and task.filename == "movie.mp4"
    assert hashlib.sha256(published.read_bytes()).hexdigest() == io.sha256
    assert not published.read_bytes().startswith(b"<")
    # aria2 itself (not just Cove's probe) presented the account cookie.
    assert any(ua.startswith("aria2") and has_token(cookie) for _, _, cookie, ua in io.requests)
    assert not any(path == "/d/ObvAFn9h" for _, path, _, _ in io.requests)
    assert TOKEN not in repr(events) and TOKEN not in repr(task)
    with db.connect() as conn:
        assert TOKEN not in repr([dict(r) for r in conn.execute("SELECT * FROM downloads")])
    assert task.resolved_url == ""  # transient request cleared on completion
    assert [p.name for p in (tmp_path / "downloads").rglob("*")] == ["movie.mp4"]  # staging is gone


@pytest.mark.parametrize("mode, message", [("swap", gofile.LANDING_PAGE), ("short", gofile.SIZE_MISMATCH)])
def test_real_aria2_page_posing_as_the_file_cannot_complete(real_gofile_io, tmp_path, mode, message):
    """The reported bug: a ~3 KB page saved as movie.mp4 and marked Done."""
    io = real_gofile_io
    io.mode = mode
    queue, task = io.build()
    run_to_end(io, queue, task)
    assert task.status == "error" and task.error == message
    assert not (tmp_path / "downloads" / "movie.mp4").exists()
    assert not [p for p in (tmp_path / "downloads").rglob("*") if p.is_file()]  # bad bytes are not left behind
    with db.connect() as conn:
        row = dict(conn.execute("SELECT * FROM downloads WHERE id=?", (task.id,)).fetchone())
    assert row["status"] == "error" and row["error"] == message
    # Retry after the problem is fixed downloads the real file instead of resuming the bad one.
    io.mode = "good"
    task.gid, task.status = None, "queued"
    run_to_end(io, queue, task)
    assert task.status == "completed", task.error
    assert (tmp_path / "downloads" / "movie.mp4").read_bytes() == io.payload


def test_real_aria2_redirect_off_gofile_never_carries_the_account_token(real_gofile_io, tmp_path):
    """A transfer-time redirect to another host (here 127.0.0.1 -> localhost) must not carry the token."""
    io = real_gofile_io
    io.mode = "redirect"
    queue, task = io.build()
    run_to_end(io, queue, task)
    on_host = [c for _, path, c, ua in io.requests if path == "/file" and ua.startswith("aria2")]
    stolen = [(path, cookie) for _, path, cookie, ua in io.requests if path == "/steal" and ua.startswith("aria2")]
    assert on_host and all(has_token(c) for c in on_host), "positive control: the jar cookie reaches the scoped host"
    assert stolen, "the redirect was never followed, so this test proves nothing"
    assert all(cookie == "" for _, cookie in stolen), stolen
    assert not any(TOKEN in cookie for _, _, cookie, _ in io.requests_off_host())


def test_real_aria2_completes_a_genuinely_empty_file(real_gofile_io, tmp_path):
    io = real_gofile_io
    io.mode = "empty"
    queue, task = io.build(size=0)
    run_to_end(io, queue, task)
    assert task.status == "completed", task.error
    assert (tmp_path / "downloads" / "movie.mp4").read_bytes() == b""


def test_real_aria2_storage_refusing_the_engines_token_is_a_clear_error(real_gofile_factory, tmp_path):
    io = real_gofile_factory
    wrong = "Z" * 32                                     # engine and settings agree, storage does not
    io.start(token=wrong)
    queue, task = io.build()
    queue.settings.gofile_api_token = wrong
    run_to_end(io, queue, task)
    assert task.status == "error" and task.error == gofile.FILE_DENIED
    assert io.rpc.tell_active() == [] and not (tmp_path / "downloads" / "movie.mp4").exists()
    assert not any(ua.startswith("aria2") for _, _, _, ua in io.requests)


def test_real_aria2_secure_cookie_is_not_sent_over_plain_http(real_gofile_factory, tmp_path):
    """Proves aria2 honours the Secure flag the production jar carries."""
    io = real_gofile_factory
    io.start(secure=True)
    queue, task = io.build()
    run_to_end(io, queue, task)
    seen = [c for _, path, c, ua in io.requests if path == "/file" and ua.startswith("aria2")]
    assert seen and all(c == "" for c in seen), seen
    assert task.status == "error" and not (tmp_path / "downloads" / "movie.mp4").exists()


def test_real_aria2_jar_is_gone_once_ready_and_the_token_is_nowhere_visible(real_gofile_io):
    import os
    io = real_gofile_io
    assert [p.name for p in io.data_dir.iterdir() if "cookie" in p.name or "launch" in p.name] == []
    argv = Path(f"/proc/{io.engine.daemon._proc.pid}/cmdline").read_bytes() if os.name == "posix" else b""
    assert TOKEN.encode() not in argv
    log = io.data_dir / "aria2.log"
    assert not log.exists() or TOKEN not in log.read_text(errors="replace")
    queue, task = io.build()
    run_to_end(io, queue, task)                           # the cookie still works after the file was unlinked
    assert task.status == "completed", task.error
    assert any(has_token(c) for _, path, c, ua in io.requests if path == "/file" and ua.startswith("aria2"))


def test_real_aria2_changed_token_fails_closed_without_disturbing_the_engine(real_gofile_io, tmp_path):
    io = real_gofile_io
    pid = io.engine.daemon._proc.pid
    queue, task = io.build()
    queue.settings.gofile_api_token = "N" * 32
    run_to_end(io, queue, task)
    assert task.status == "error" and task.error == gofile.COOKIE_RESTART
    assert not any(ua.startswith("aria2") for _, _, _, ua in io.requests)
    assert io.engine.daemon.is_running() and io.engine.daemon._proc.pid == pid     # never restarted
    queue.settings.gofile_api_token = TOKEN                                         # change back: works again
    task.gid, task.status = None, "queued"
    run_to_end(io, queue, task)
    assert task.status == "completed", task.error


def test_real_aria2_running_transfer_survives_a_token_change(real_gofile_io, tmp_path):
    io = real_gofile_io
    io.mode = "slow"
    pid = io.engine.daemon._proc.pid
    queue, task = io.build()
    queue._scheduler_allows = True
    queue._launch(task)
    pump_until(lambda: io.started.is_set() and bool(task.gid))
    queue.settings.gofile_api_token = "N" * 32            # the user edits the token mid-transfer
    assert io.engine.daemon.is_running() and io.engine.daemon._proc.pid == pid
    assert io.rpc.tell_status(task.gid)["status"] == "active"
    io.release.set()

    def finished():
        if task.gid and task.id not in queue._publishing and task.status not in ("completed", "error"):
            queue._apply_status(task.id, io.rpc.tell_status(task.gid))
        return task.status in ("completed", "error")
    pump_until(finished)
    assert task.status == "completed", task.error
    assert (tmp_path / "downloads" / "movie.mp4").read_bytes() == io.payload


def test_real_aria2_engine_started_without_a_token_asks_for_a_restart(real_gofile_factory, tmp_path):
    io = real_gofile_factory
    io.start(token="")
    assert io.engine.daemon.gofile_cookie_fingerprint() == ""
    assert not [p for p in io.data_dir.iterdir() if "cookie" in p.name]
    queue, task = io.build()
    run_to_end(io, queue, task)
    assert task.status == "error" and task.error == gofile.COOKIE_RESTART
    assert not any(ua.startswith("aria2") for _, _, _, ua in io.requests)


def test_external_aria2_cannot_carry_the_token_so_gofile_fails_closed(real_gofile_io, tmp_path):
    """A client with no Cove-started engine behind it (an external aria2) must never be handed the file."""
    from cove.aria2 import Aria2RPC
    io = real_gofile_io
    external = Aria2RPC(io.engine.settings, timeout=2, secret_provider=io.engine.daemon.active_rpc_secret)
    assert external.gofile_cookie_fingerprint() is None
    queue, task = io.build(rpc=external)
    run_to_end(io, queue, task)
    assert task.status == "error" and task.error == gofile.COOKIE_EXTERNAL
    assert io.rpc.tell_active() == [] and not io.requests


def test_real_aria2_ordinary_html_download_is_unchanged(real_gofile_io, tmp_path):
    """Only Gofile rows are screened: a plain URL that serves HTML still downloads as before."""
    io = real_gofile_io
    queue, _ = io.build()
    tid = queue.add_url(f"{io.base_url}/plain.html", str(tmp_path / "plain"), filename="page.html")
    task = queue.tasks[tid]
    run_to_end(io, queue, task)
    assert task.status == "completed", task.error
    assert Path(task.output_path).read_bytes() == LANDING


# --- settings --------------------------------------------------------------


@pytest.mark.parametrize("stored, expected", [(TOKEN, TOKEN), (5, ""), (None, ""), (["x"], "")])
def test_gofile_token_persists_and_a_hand_edited_value_is_repaired(tmp_path, monkeypatch, stored, expected):
    import json
    from cove import config
    config_dir, data_dir = tmp_path / "config", tmp_path / "data"
    for directory in (config_dir, data_dir):
        directory.mkdir(mode=0o700)
    path = config_dir / "settings.json"
    path.write_text(json.dumps({"gofile_api_token": stored}))
    path.chmod(0o600)
    monkeypatch.setattr(config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    assert config.Settings.load().gofile_api_token == expected


def test_settings_dialog_round_trips_the_token_without_echoing_it():
    from PySide6.QtWidgets import QLineEdit
    from cove.dialogs import SettingsDialog
    current = Settings(gofile_api_token=TOKEN)
    dialog = SettingsDialog(current)
    assert dialog.gofile_token.text() == TOKEN and dialog.gofile_token.echoMode() == QLineEdit.Password
    dialog.gofile_token.setText("  " + "N" * 32 + "  ")
    dialog._on_accept()
    assert current.gofile_api_token == "N" * 32


# --- the cookie jar: text, daemon lifecycle, permissions, secrecy ----------


def test_production_jar_is_secure_host_scoped_and_long_lived():
    line = gofile.cookie_jar_text(TOKEN).splitlines()[1]
    domain, subdomains, path, secure, expires, name, value = line.split("\t")
    assert (domain, subdomains, path, secure, name, value) == (
        ".gofile.io", "TRUE", "/", "TRUE", "accountToken", TOKEN)
    assert int(expires) == 2**31 - 1
    assert gofile.cookie_jar_text(TOKEN).startswith("# Netscape HTTP Cookie File\n")


@pytest.mark.parametrize("token", ["", "short", "has space 0123456789abcdef", "tab\t0123456789abcdefghij",
                                   "new\nline0123456789abcdef", None, 5, "x" * 200])
def test_jar_refuses_a_token_that_could_inject_fields(token):
    with pytest.raises(gofile.GofileError):
        gofile.cookie_jar_text(token)


def test_fingerprint_identifies_a_token_without_containing_it():
    a, b = gofile.token_fingerprint(TOKEN), gofile.token_fingerprint("N" * 32)
    assert a != b and a == gofile.token_fingerprint(TOKEN) and TOKEN not in a and len(a) == 64


@pytest.fixture
def daemon_env(tmp_path, monkeypatch):
    from cove import aria2
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    monkeypatch.setattr(aria2, "DATA_DIR", data)
    monkeypatch.setattr(aria2, "ARIA2_LOG", data / "aria2.log")
    return aria2, data


def test_daemon_jar_is_private_exclusive_and_removed_with_the_launch_conf(daemon_env):
    aria2, data = daemon_env
    daemon = aria2.Aria2Daemon(Settings(gofile_api_token=f"  {TOKEN}  "))
    jar = daemon._write_gofile_jar()
    assert jar.parent == data and jar.name.startswith(".aria2.cookies.")
    if hasattr(__import__("os"), "geteuid"):
        assert (jar.stat().st_mode & 0o777) == 0o600
    assert jar.read_text() == gofile.cookie_jar_text(TOKEN)
    assert daemon._gofile_fingerprint == gofile.token_fingerprint(TOKEN)   # whitespace is not part of the token
    daemon._unlink_launch_conf()
    assert not jar.exists() and daemon._launch_jar is None


@pytest.mark.parametrize("token", ["", "   ", "short", "bad token 0123456789abcdef"])
def test_daemon_writes_no_jar_without_a_usable_token(daemon_env, token):
    aria2, data = daemon_env
    daemon = aria2.Aria2Daemon(Settings(gofile_api_token=token))
    assert daemon._write_gofile_jar() is None and daemon._gofile_fingerprint == ""
    assert list(data.iterdir()) == []


def test_daemon_jar_never_follows_a_planted_symlink(daemon_env, monkeypatch):
    aria2, data = daemon_env
    daemon = aria2.Aria2Daemon(Settings(gofile_api_token=TOKEN))
    victim = data.parent / "victim.txt"
    victim.write_text("keep")
    monkeypatch.setattr(aria2.secrets, "token_hex", lambda n: "planted")
    (data / ".aria2.cookies.planted.txt").symlink_to(victim)
    with pytest.raises(aria2.Aria2Error):
        daemon._write_gofile_jar()
    assert victim.read_text() == "keep" and TOKEN not in victim.read_text()


def test_launch_conf_names_the_jar_but_argv_and_the_record_never_hold_the_token(daemon_env, monkeypatch):
    aria2, data = daemon_env
    spawned = []
    monkeypatch.setattr(aria2, "_resolve_aria2c", lambda: "/usr/bin/aria2c")
    monkeypatch.setattr(aria2.Aria2Daemon, "_port_in_use", lambda self: False)
    monkeypatch.setattr(aria2.Aria2Daemon, "_spawn", lambda self, args: spawned.append(args))
    daemon = aria2.Aria2Daemon(Settings(gofile_api_token=TOKEN, rpc_port=16899))
    daemon.begin_start()
    (args,) = spawned
    assert TOKEN not in repr(args) and not any(a.startswith("--load-cookies") for a in args)
    conf = Path(next(a for a in args if a.startswith("--conf-path=")).split("=", 1)[1])
    jars = [p for p in data.iterdir() if p.name.startswith(".aria2.cookies.")]
    assert len(jars) == 1 and f"load-cookies={jars[0]}\n" in conf.read_text()
    assert TOKEN not in conf.read_text() and jars[0] != conf
    record = (data / "aria2.rpc.auth").read_text()
    assert "load-cookies" not in record and TOKEN not in record
    daemon._unlink_launch_conf()                          # what commit_ready / stop() do once the engine is ready
    assert [p.name for p in data.iterdir()] == ["aria2.rpc.auth"]


def test_failed_conf_write_leaves_no_jar_behind(daemon_env, monkeypatch):
    aria2, data = daemon_env
    monkeypatch.setattr(aria2, "_resolve_aria2c", lambda: "/usr/bin/aria2c")
    monkeypatch.setattr(aria2.Aria2Daemon, "_port_in_use", lambda self: False)
    monkeypatch.setattr(aria2.Aria2Daemon, "_write_rpc_conf", lambda self, *a, **k: (_ for _ in ()).throw(aria2.Aria2Error("x")))
    daemon = aria2.Aria2Daemon(Settings(gofile_api_token=TOKEN, rpc_port=16898))
    with pytest.raises(aria2.Aria2Error):
        daemon.begin_start()
    assert [p for p in data.iterdir() if "cookie" in p.name] == [] and daemon.gofile_cookie_fingerprint() is None


def test_only_a_committed_running_engine_vouches_for_its_jar(daemon_env):
    aria2, _ = daemon_env
    daemon = aria2.Aria2Daemon(Settings(gofile_api_token=TOKEN))
    daemon._write_gofile_jar()
    assert daemon.gofile_cookie_fingerprint() is None     # written, but no running committed engine
    daemon._unlink_launch_conf()
    assert aria2.Aria2RPC(Settings()).gofile_cookie_fingerprint() is None       # external: no provider


@pytest.mark.parametrize("held, token, message", [
    (None, TOKEN, gofile.COOKIE_EXTERNAL), ("", TOKEN, gofile.COOKIE_RESTART),
    (gofile.token_fingerprint("N" * 32), TOKEN, gofile.COOKIE_RESTART),
])
def test_queue_refuses_to_launch_unless_the_engine_holds_the_current_token(queue_env, monkeypatch, tmp_path, held, token, message):
    queue, rpc, _ = queue_env(gofile_api_token=token)
    task = one_task(queue, tmp_path)
    if held is not None:
        queue.rpc.gofile_cookie_fingerprint = lambda: held
    net = Net()
    monkeypatch.setattr(queue, "_bound_session", lambda: net)
    inline(queue)
    queue._launch(task)
    assert task.status == "error" and task.error == message and not rpc.added and not net.calls
