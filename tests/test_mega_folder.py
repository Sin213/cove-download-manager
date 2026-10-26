"""MEGA folders use fake AllDebrid envelopes only; no provider traffic."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog

from cove import db, debrid
from cove.config import Settings
from tests.test_debrid import FakeSession, _Resp
from tests.test_queue import queue_env  # noqa: F401
from tests.test_queue_output_ownership import inline

KEY = "FOLDER_KEY_CANARY_12345"
APIKEY = "FAKE_API_KEY_CANARY"  # gitleaks:allow
URL = f"https://mega.nz/folder/AbCd1234#{KEY}"
LINKS = tuple(f"https://redirect.alldebrid.com/test-{i}-abcdef" for i in range(3))


@pytest.fixture(scope="module", autouse=True)
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    from urllib.parse import urlsplit
    original = requests.sessions.Session.request
    def refuse(session, method, url, **kwargs):
        if urlsplit(url).hostname == "127.0.0.1" and urlsplit(url).scheme == "http":
            return original(session, method, url, **kwargs)
        pytest.fail("Unexpected real network request")
    monkeypatch.setattr(requests.sessions.Session, "request", refuse)


def settings(**kwargs):
    return Settings(**dict(all_debrid_enabled=True, all_debrid_api_key=APIKEY, **kwargs))


def success(data):
    return _Resp({"status": "success", "data": data})


def session_for(names=("Folder/file1.txt", "file2.bin", "Subfolder/file3.txt"),
                infos=None, links=LINKS):
    if infos is None:
        infos = [dict(link=link, filename=name, size=i + 1, host="mega",
                      hostDomain="mega.co.nz")
                 for i, (link, name) in enumerate(zip(links, names))][::-1]
    return FakeSession({"/link/redirector": success({"links": list(links)}),
                        "/link/infos": success({"infos": infos})})


def listing():
    return debrid.list_mega_folder(URL, settings(), session=session_for())


@pytest.mark.parametrize("url", [
    URL, URL + "/file/child", URL + "/folder/child",
    f"http://www.mega.nz/folder/AbCd1234#{KEY}",
    f"https://mega.co.nz/#F!AbCd1234!{KEY}",
    f"https://mega.io/folder/AbCd1234#{KEY}",
])
def test_folder_forms(url):
    assert debrid.is_mega_folder_url(url)
    assert debrid.parse_mega_folder_url(url) is not None


@pytest.mark.parametrize("url", [
    "https://mega.nz/folder/AbCd1234", "https://mega.nz/folder/",
    "https://mega.nz/folder/AbCd1234#", "https://mega.nz/#F!AbCd1234",
    "https://mega.nz/folder/AbCd1234#bad!key",
    URL + "/file/", URL + "/oops/child",
])
def test_bad_folder_is_guarded_but_not_parsed(url):
    assert debrid.is_mega_folder_url(url)
    assert debrid.parse_mega_folder_url(url) is None
    fake = session_for()
    with pytest.raises(debrid.DebridError, match="incorrect or missing key"):
        debrid.list_mega_folder(url, settings(), session=fake)
    assert not fake.calls


@pytest.mark.parametrize("url", [
    "https://mega.nz/file/AbCd1234#filekey", "https://mega.nz/#!AbCd1234!filekey",
    URL.replace("mega.nz", "mega.nz.evil.test"),
    URL.replace("mega.nz", "evilmega.nz"), "https://example.org/folder/a#key",
])
def test_other_links(url):
    assert not debrid.is_mega_folder_url(url)
    assert debrid.parse_mega_folder_url(url) is None
    assert not debrid.share_link_reason(url)


def test_listing_matches_echoed_link_and_sorts_paths():
    fake = session_for()
    result = debrid.list_mega_folder(URL, settings(), session=fake)
    assert [(f.name, f.path, f.size, f.locked_link) for f in result.files] == [
        ("file1.txt", ("Folder", "file1.txt"), 1, LINKS[0]),
        ("file3.txt", ("Subfolder", "file3.txt"), 3, LINKS[2]),
        ("file2.bin", ("file2.bin",), 2, LINKS[1]),
    ]
    assert result.unavailable == 0
    assert fake.calls[0][2]["data"] == {"link": URL}
    assert fake.calls[1][2]["data"] == {"link[]": list(LINKS)}
    assert all(c[2]["headers"] == {"Authorization": f"Bearer {APIKEY}"} for c in fake.calls)
    assert KEY not in repr(result) and APIKEY not in repr(result)


def test_flat_names():
    result = debrid.list_mega_folder(URL, settings(), session=session_for(
        ("file1.txt", "file2.bin", "file3.txt")))
    assert all(f.path == (f.name,) for f in result.files)


def test_partial_unavailable_and_all_bad():
    bad = dict(link=LINKS[1], error=dict(code="LINK_HOST_NOT_SUPPORTED", message=URL + APIKEY))
    good = dict(link=LINKS[0], filename="good.txt", size=12)
    result = debrid.list_mega_folder(URL, settings(), session=session_for(
        infos=[bad, good], links=LINKS[:2]))
    assert result.unavailable == 1 and len(result.files) == 1
    with pytest.raises(debrid.DebridError):
        debrid.list_mega_folder(URL, settings(), session=session_for(infos=[bad], links=LINKS[1:2]))


@pytest.mark.parametrize("kind", ["missing", "duplicate", "extra"])
def test_ambiguous_infos_rejected(kind):
    infos = [dict(link=link, filename=f"f{i}", size=1) for i, link in enumerate(LINKS)]
    if kind == "missing":
        infos.pop()
    elif kind == "duplicate":
        infos.append(infos[0])
    else:
        infos.append(dict(link="https://redirect.alldebrid.com/extra", filename="extra", size=1))
    with pytest.raises(debrid.DebridError):
        debrid.list_mega_folder(URL, settings(), session=session_for(infos=infos))


def test_chunking_and_cap():
    links = [f"https://redirect.alldebrid.com/{i}-0-abc" for i in range(51)]
    fake = session_for(links=links)
    fake.routes["/link/infos"] = [success({"infos": [
        dict(link=link, filename=f"f{i}", size=1) for i, link in enumerate(chunk)]})
        for chunk in (links[:25], links[25:50], links[50:])]
    assert len(debrid.list_mega_folder(URL, settings(), session=fake).files) == 51
    assert all(len(c[2]["data"]["link[]"]) <= 25 for c in fake.calls[1:])
    fake = session_for(links=links * 20)
    with pytest.raises(debrid.DebridError, match="1000"):
        debrid.list_mega_folder(URL, settings(), session=fake)
    assert len(fake.calls) == 1


@pytest.mark.parametrize("enabled,key", [(False, APIKEY), (True, ""), (True, " ")])
def test_requires_enabled_account(enabled, key):
    fake = session_for()
    config = Settings(all_debrid_enabled=enabled, all_debrid_api_key=key)
    with pytest.raises(debrid.DebridError, match="Settings"):
        debrid.list_mega_folder(URL, config, session=fake)
    assert not fake.calls


@pytest.mark.parametrize("code,phrase", [
    ("REDIRECTOR_NOT_SUPPORTED", "not supported"),
    ("REDIRECTOR_ERROR", "unavailable or deleted"),
    ("LINK_PASS_PROTECTED", "incorrect or missing key"),
    ("AUTH_BAD_APIKEY", "Settings"),
    ("LINK_HOST_FULL", "quota"), ("MAINTENANCE", "unavailable"),
    (URL + APIKEY, "unavailable"),
])
def test_fixed_errors(code, phrase):
    fake = FakeSession({"/link/redirector": _Resp({"status": "error", "error": {
        "code": code, "message": URL + APIKEY}})})
    with pytest.raises(debrid.DebridError) as caught:
        debrid.list_mega_folder(URL, settings(), session=fake)
    assert phrase in caught.value.user_message
    for value in (str(caught.value), repr(caught.value), repr(vars(caught.value))):
        assert KEY not in value and APIKEY not in value and "AbCd1234" not in value


def test_transport_error_is_fixed():
    fake = FakeSession({"/link/redirector": requests.ConnectionError(URL + APIKEY)})
    with pytest.raises(debrid.DebridError) as caught:
        debrid.list_mega_folder(URL, settings(), session=fake)
    assert "unavailable" in str(caught.value)
    assert KEY not in repr(caught.value) and APIKEY not in repr(caught.value)


@pytest.mark.parametrize("name", ["..", "/absolute", "A/../bad", "A\\bad", "bad\x00", "x" * 256,
                                 "bad. ", "bad\nname", "A//b", "C:/bad"])
def test_unsafe_names_rejected(name):
    with pytest.raises(debrid.DebridError):
        debrid.list_mega_folder(URL, settings(), session=session_for((name,), links=LINKS[:1]))


@pytest.mark.parametrize("link", ["http://redirect.alldebrid.com/a", "https://evil.test/a",
                                 "https://redirect.alldebrid.com.evil.test/a"])
def test_virtual_links_validated(link):
    with pytest.raises(debrid.DebridError):
        debrid.list_mega_folder(URL, settings(), session=session_for(("f",), links=(link,)))


@pytest.mark.parametrize("enabled", [True, False])
def test_plain_intake_and_legacy_launch_are_blocked(queue_env, enabled):
    queue, rpc, _ = queue_env(all_debrid_enabled=enabled, all_debrid_api_key=APIKEY)
    errors, events = [], []
    queue.error.connect(errors.append)
    queue._diag = lambda *args, **kwargs: events.append((args, kwargs))
    assert queue.prepare_url(URL) is None
    assert queue.add_url(URL) is None
    assert len(errors) == 2 and all("Add Download" in e for e in errors)
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM downloads").fetchone()[0] == 0
    # Negative control: a persisted plain row must fail even without the intake guard.
    tid = queue.add_url("https://example.test/file")
    task = queue.tasks[tid]
    task.url = URL
    inline(queue)
    queue._launch(task)
    assert task.status == "error" and task.error == debrid.share_link_reason(URL)
    assert not rpc.added
    assert KEY not in repr(events) and APIKEY not in repr(events)


def test_selected_rows_and_identity(queue_env, tmp_path):
    from cove.queue import SOURCE_DEBRID_FILE
    queue, rpc, _ = queue_env()
    files = listing().files
    assert queue.add_debrid_files([], str(tmp_path)) == []
    ids = queue.add_debrid_files(files[:1], str(tmp_path))
    assert len(ids) == 1
    ids += queue.add_debrid_files(files, str(tmp_path))
    assert len(ids) == 3 and queue.add_debrid_files(files, str(tmp_path)) == []
    with db.connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM downloads")]
    assert KEY not in repr(rows) and "AbCd1234" not in repr(rows) and APIKEY not in repr(rows)
    assert {r["url"] for r in rows} == set(LINKS)
    assert all(r["source_type"] == SOURCE_DEBRID_FILE and r["debrid_route"] == debrid.ALL_DEBRID
               and r["status"] == "queued" for r in rows)
    assert {Path(t.out_dir).relative_to(tmp_path).as_posix() for t in queue.tasks.values()} == {
        "Folder", "Subfolder", "."}
    assert not rpc.added


def test_paths_revalidated_atomically(queue_env, tmp_path):
    queue, _, _ = queue_env()
    file = listing().files[0]
    for parts in [("..", "bad"), ("/absolute", "bad"), ("A\\B", "bad")]:
        with pytest.raises(debrid.DebridError):
            queue.add_debrid_files([file, replace(file, path=parts, name=parts[-1])], str(tmp_path))
        assert not queue.tasks
    outside = tmp_path / "outside"
    outside.mkdir()
    base = tmp_path / "chosen"
    base.mkdir()
    (base / "Folder").symlink_to(outside, target_is_directory=True)
    with pytest.raises(debrid.DebridError):
        queue.add_debrid_files([file], str(base))
    assert not queue.tasks


def test_unlock_launch_collision_and_remove(queue_env, monkeypatch, tmp_path):
    queue, rpc, _ = queue_env(intelligent_segments=False, all_debrid_enabled=False,
                              all_debrid_api_key=APIKEY)
    entry = listing().files[0]
    tid = queue.add_debrid_files([entry], str(tmp_path))[0]
    task = queue.tasks[tid]
    public = Path(task.out_dir) / entry.name
    public.parent.mkdir(parents=True, exist_ok=True)
    public.write_bytes(b"unrelated")
    fake = FakeSession({"/link/unlock": success(dict(filename=entry.name, filesize=1,
                       link="https://node.debrid.it/file", host="mega"))})
    monkeypatch.setattr(queue, "_bound_session", lambda: fake)
    inline(queue)
    queue._launch(task)
    assert fake.calls[0][2]["data"] == {"link": entry.locked_link}
    assert rpc.added[0]["uris"] == ["https://node.debrid.it/file"]
    assert task.url == entry.locked_link and not task.info_hash and not task.torrent_name
    payload = Path(task.work_dir) / entry.name
    payload.write_bytes(b"new")
    queue._apply_status(tid, {"status": "complete", "files": [{"path": str(payload)}]})
    assert task.status == "completed" and public.read_bytes() == b"unrelated"
    assert Path(task.output_path).read_bytes() == b"new" and Path(task.output_path) != public
    queue.remove(tid, delete_file=True)
    assert tid not in queue.tasks and public.read_bytes() == b"unrelated"


def test_dialog_selection_and_cancel():
    from cove.dialogs import MegaFolderDialog
    result = replace(listing(), unavailable=2)
    dlg = MegaFolderDialog(result, "/tmp")
    assert dlg.selected_count() == 3 and "2 unavailable" in dlg.summary_text()
    dlg.select_none()
    assert not dlg.download_button().isEnabled()
    dlg.confirm()
    assert dlg.result_selection() == ()
    dlg.select_all()
    assert dlg.download_button().isEnabled()
    dlg.leaves()[0][0].setCheckState(0, Qt.Unchecked)
    dlg.confirm()
    assert dlg.result() == QDialog.Accepted and dlg.result_selection() == result.files[1:]
    dlg.reject()
    assert dlg.result_selection() == ()


def test_main_flow_cancel_and_late_result(queue_env, monkeypatch, tmp_path):
    import cove.main_window as mw
    queue, _, _ = queue_env()
    finishes = []
    owner = SimpleNamespace(queue=queue, settings=queue.settings, _preflights_closed=False)
    owner._mega_folder_preflight = lambda *args: mw.MainWindow._mega_folder_preflight(owner, *args)
    owner._resolve_mega_folder = lambda *args: mw.MainWindow._resolve_mega_folder(owner, *args)
    monkeypatch.setattr(mw, "MegaFolderDialog", lambda *args: pytest.fail("Late selection dialog"))
    monkeypatch.setattr(queue, "add_debrid_files", lambda *args: pytest.fail("Late commit"))
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
        # Resolve is still on its stack. Cancel first, then deliver a success
        # before it can return. A callback must not touch the dialog again.
        assert owner._mega_open and owner._mega_dialog is not None
        owner._mega_dialog.reject()
        assert finishes == [True]
        on_done((listing(), ""))
        assert finishes == [True]
    monkeypatch.setattr(queue, "_spawn", spawn)
    assert mw.MainWindow.add_urls_checked(owner, [URL], str(tmp_path)) == []
    assert not queue.tasks and finishes == [True]


@pytest.mark.parametrize("accept", [False, True])
def test_main_flow_selection(queue_env, monkeypatch, tmp_path, accept):
    import cove.main_window as mw
    queue, _, _ = queue_env()
    inline(queue)
    listing_result = listing()
    monkeypatch.setattr(debrid, "list_mega_folder", lambda *args, **kw: listing_result)
    owner = SimpleNamespace(queue=queue, settings=queue.settings, _preflights_closed=False)
    owner._mega_folder_preflight = lambda *args: mw.MainWindow._mega_folder_preflight(owner, *args)
    owner._resolve_mega_folder = lambda *args: mw.MainWindow._resolve_mega_folder(owner, *args)
    class Choice:
        Accepted = QDialog.Accepted
        def __init__(self, *args):
            pass
        def exec(self):
            return QDialog.Accepted if accept else QDialog.Rejected
        def result_selection(self):
            return listing_result.files[:1]
    monkeypatch.setattr(mw, "MegaFolderDialog", Choice)
    real_loading = mw.MagnetMetadataDialog
    monkeypatch.setattr(mw, "MagnetMetadataDialog", lambda *args: real_loading())
    monkeypatch.setattr(queue, "add_url", lambda *args, **kw: pytest.fail("Folder in plain intake"))
    ids = mw.MainWindow.add_urls_checked(owner, [URL], str(tmp_path))
    assert len(ids) == int(accept) and len(queue.tasks) == int(accept)


def flow_owner(queue):
    import cove.main_window as mw
    owner = SimpleNamespace(queue=queue, settings=queue.settings, _preflights_closed=False)
    owner._mega_folder_preflight = lambda *args: mw.MainWindow._mega_folder_preflight(owner, *args)
    owner._resolve_mega_folder = lambda *args: mw.MainWindow._resolve_mega_folder(owner, *args)
    owner.add_urls_checked = lambda *args: mw.MainWindow.add_urls_checked(owner, *args)
    return owner


@pytest.mark.parametrize("failure", [False, True])
def test_real_worker_thread_and_fixed_gui_error(queue_env, monkeypatch, app, failure):
    from PySide6.QtCore import QThread
    import cove.main_window as mw
    queue, _, _ = queue_env()
    result = listing()
    fake_session = object()
    monkeypatch.setattr(queue, "_bound_session", lambda: fake_session)
    threads, warnings = [], []
    def fetch(url, config, *, session):
        threads.append(QThread.currentThread() == app.thread())
        assert url == URL and session is fake_session
        if failure:
            raise requests.ConnectionError(URL + APIKEY)
        return result
    monkeypatch.setattr(debrid, "list_mega_folder", fetch)
    real_loading = mw.MagnetMetadataDialog
    monkeypatch.setattr(mw, "MagnetMetadataDialog", lambda *args: real_loading())
    class Choice:
        Accepted = QDialog.Accepted
        def __init__(self, *args):
            assert QThread.currentThread() == app.thread()
        def exec(self):
            return QDialog.Accepted
        def result_selection(self):
            return result.files[:1]
    monkeypatch.setattr(mw, "MegaFolderDialog", Choice)
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *args: warnings.append(args[1:]))
    ids = flow_owner(queue).add_urls_checked([URL])
    assert threads == [False]
    assert len(ids) == int(not failure)
    assert len(warnings) == int(failure)
    assert KEY not in repr(warnings) and APIKEY not in repr(warnings)


def test_close_during_loading_ignores_delivery(queue_env, monkeypatch):
    import cove.main_window as mw
    queue, _, _ = queue_env()
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
    assert owner.add_urls_checked([URL]) == []
    assert owner._preflights_closed and owner._mega_dialog is None
    calls[0][1]["on_done"]((listing(), ""))
    assert calls[0][0]() == (None, "")  # Cancelled before worker admission: no request.
    assert not queue.tasks


def test_mixed_paste_keeps_plain_url_route(queue_env, monkeypatch):
    import cove.main_window as mw
    queue, _, _ = queue_env()
    owner = flow_owner(queue)
    seen = []
    owner._mega_folder_preflight = lambda *args: seen.append(args) or []
    owner._candidate = mw.MainWindow._candidate
    owner._add_single_interactive = lambda *args: None
    ids = owner.add_urls_checked([URL, "https://example.test/file.bin"])
    assert seen == [(URL, None)] and len(ids) == 1
    assert queue.tasks[ids[0]].url == "https://example.test/file.bin"


def test_reentrant_folder_flows_do_not_nest(queue_env):
    import cove.main_window as mw
    queue, _, _ = queue_env()
    owner = flow_owner(queue)
    seen = []
    def resolve(url, out_dir):
        seen.append(url)
        if len(seen) == 1:
            assert owner._mega_folder_preflight(URL + "/file/child", out_dir) == []
        return []
    owner._resolve_mega_folder = resolve
    assert mw.MainWindow._mega_folder_preflight(owner, URL, None) == []
    assert seen == [URL, URL + "/file/child"]


def test_debrid_file_pause_and_active_remove(queue_env, monkeypatch, tmp_path):
    queue, rpc, _ = queue_env(intelligent_segments=False, all_debrid_api_key=APIKEY)
    tid = queue.add_debrid_files(listing().files[:1], str(tmp_path))[0]
    task = queue.tasks[tid]
    fake = FakeSession({"/link/unlock": success(dict(filename=task.filename, filesize=1,
                       link="https://node.debrid.it/file"))})
    monkeypatch.setattr(queue, "_bound_session", lambda: fake)
    inline(queue)
    queue._scheduler_allows = True
    queue._launch(task)
    queue.pause(tid)
    assert task.status == "paused" and task.gid in rpc.paused
    rpc.status_result = {"status": "removed"}
    queue.remove(tid)
    assert tid not in queue.tasks and len(rpc.added) == 1


@pytest.mark.parametrize("status", ["active", "paused", "complete", "error"])
@pytest.mark.parametrize("position", ["first", "second_uri", "second_file"])
def test_external_folder_never_adopted(queue_env, monkeypatch, tmp_path, status, position):
    queue, rpc, _ = queue_env()
    uri = {"uri": URL}
    files = [{"path": str(tmp_path / "landing.html"), "uris": [uri]}]
    if position != "first":
        files[0]["uris"] = [{"uri": "https://example.test/landing"}]
        if position == "second_uri":
            files[0]["uris"].append(uri)
        else:
            files.append({"path": str(tmp_path / "other"), "uris": [uri]})
    blocked = dict(gid="external-folder", status=status, files=files,
                   totalLength="99", completedLength="99")
    ordinary = dict(gid="external-file", status=status, totalLength="10", completedLength="5",
                    files=[{"path": str(tmp_path / "good.bin"),
                            "uris": [{"uri": "https://example.test/good.bin"}]}])
    monkeypatch.setattr(rpc, "tell_external_snapshot", lambda: [blocked, ordinary], raising=False)
    events, errors, added = [], [], []
    queue._diag = lambda *args, **kw: events.append((args, kw))
    queue.error.connect(errors.append)
    queue.task_added.connect(added.append)
    inline(queue)
    queue._check_external()
    queue._check_external()  # Do not rediscover either job on the next poll.
    with db.connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM downloads")]
    assert len(rows) == 1 and rows[0]["url"] == "https://example.test/good.bin"
    assert rows[0]["status"] == queue._ARIA2_STATUS[status]
    assert len(queue.tasks) == len(added) == 1
    assert not errors and not rpc.added
    evidence = repr((rows, events, errors))
    assert URL not in evidence and KEY not in evidence and "AbCd1234" not in evidence


@pytest.mark.parametrize("paths", [
    (("A",), ("A", "child.txt")),
    (("A", "child.txt"), ("A",)),
    (("a",), ("A", "child.txt")),
    (("Dir", "File.txt"), ("dir", "file.TXT")),
])
def test_selected_path_conflicts_are_atomic(queue_env, tmp_path, paths):
    queue, _, _ = queue_env()
    good = listing().files[-1]
    entries = [good] + [debrid.MegaFolderFile(p[-1], p, 1, LINKS[i]) for i, p in enumerate(paths)]
    with pytest.raises(debrid.DebridError) as caught:
        queue.add_debrid_files(entries, str(tmp_path))
    assert KEY not in str(caught.value) and APIKEY not in repr(caught.value)
    assert not queue.tasks
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM downloads").fetchone()[0] == 0


@pytest.mark.parametrize("bad", [" photo.jpg", "movie.", "space ", "..", "/absolute", "A/../bad",
                                 "A\\bad", "bad\x00", "A//b", "x" * 256])
def test_unrepresentable_file_skipped(bad):
    result = debrid.list_mega_folder(URL, settings(), session=session_for(
        ("good.txt", bad), links=LINKS[:2]))
    assert [(f.name, f.size) for f in result.files] == [("good.txt", 1)]
    assert result.unavailable == 1


def test_unrepresentable_names_counted_and_all_bad_fixed():
    result = debrid.list_mega_folder(URL, settings(), session=session_for(
        ("good.txt", " photo.jpg", "movie.")))
    assert len(result.files) == 1 and result.unavailable == 2
    with pytest.raises(debrid.DebridError, match="No files in this MEGA folder are available"):
        debrid.list_mega_folder(URL, settings(), session=session_for(
            (" photo.jpg", "movie."), links=LINKS[:2]))


@pytest.mark.parametrize("field,value", [("filename", {}), ("size", "12"), ("size", True), ("size", -1)])
def test_malformed_info_still_fails_closed(field, value):
    infos = [dict(link=link, filename=f"file{i}.txt", size=1) for i, link in enumerate(LINKS[:2])]
    infos[1][field] = value
    with pytest.raises(debrid.DebridError):
        debrid.list_mega_folder(URL, settings(), session=session_for(infos=infos, links=LINKS[:2]))


@pytest.mark.parametrize("size", [2**63 - 1, 2**63])
def test_size_sqlite_boundary_listing(size):
    infos = [dict(link=LINKS[0], filename="good.txt", size=1),
             dict(link=LINKS[1], filename="large.bin", size=size)]
    result = debrid.list_mega_folder(URL, settings(), session=session_for(infos=infos, links=LINKS[:2]))
    assert len(result.files) == (2 if size < 2**63 else 1)
    assert result.unavailable == int(size >= 2**63)
    if size < 2**63:
        assert result.files[1].size == size


@pytest.mark.parametrize("size", [2**63 - 1, 2**63, float("inf"), float("nan")])
def test_size_sqlite_boundary_queue(queue_env, tmp_path, size):
    queue, _, _ = queue_env()
    good = listing().files[0]
    large = debrid.MegaFolderFile("large.bin", ("large.bin",), size, LINKS[1])
    if size == 2**63 - 1:
        ids = queue.add_debrid_files([large], str(tmp_path))
        assert queue.tasks[ids[0]].total_bytes == size
    else:
        with pytest.raises(debrid.DebridError):
            queue.add_debrid_files([good, large], str(tmp_path))
        assert not queue.tasks
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM downloads").fetchone()[0] == int(size == 2**63 - 1)


def test_main_commit_unexpected_error_is_fixed(queue_env, monkeypatch):
    import cove.main_window as mw
    queue, _, _ = queue_env()
    result = listing()
    owner = flow_owner(queue)
    inline(queue)
    monkeypatch.setattr(debrid, "list_mega_folder", lambda *args, **kw: result)
    real_loading = mw.MagnetMetadataDialog
    monkeypatch.setattr(mw, "MagnetMetadataDialog", lambda *args: real_loading())
    class Choice:
        Accepted = QDialog.Accepted
        def __init__(self, *args):
            pass
        def exec(self):
            return QDialog.Accepted
        def result_selection(self):
            return result.files
    monkeypatch.setattr(mw, "MegaFolderDialog", Choice)
    def fail(*args):
        raise RuntimeError(URL + APIKEY)
    monkeypatch.setattr(queue, "add_debrid_files", fail)
    warnings = []
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *args: warnings.append(args[1:]))
    assert owner.add_urls_checked([URL]) == []
    assert warnings == [("MEGA folder unavailable", debrid._MEGA_UNAVAILABLE)]
    assert KEY not in repr(warnings) and APIKEY not in repr(warnings)
    assert not queue.tasks


@pytest.mark.parametrize("value,expected", [(1, 1), (64, 64), (None, 0), (0, 0),
                                          (True, 0), ("1", 0), (-1, 0), (999, 0)])
def test_unlock_max_chunks_defensive(value, expected):
    fake = FakeSession({"/link/unlock": success(dict(filename="file.bin", filesize=1,
                       link="https://node.debrid.it/file", max_chunks=value))})
    result = debrid.all_debrid_unrestrict(LINKS[0], APIKEY, session=fake)
    assert result.max_chunks == expected


@pytest.mark.parametrize("probe", [False, True])
@pytest.mark.parametrize("limit", [1, None, 0, True, "1", -1, 999])
def test_debrid_file_connection_limit_and_speed(queue_env, monkeypatch, tmp_path, probe, limit):
    queue, rpc, _ = queue_env(connections_per_server=16, all_debrid_api_key=APIKEY)
    size = 0 if probe else 200_000_000
    entry = debrid.MegaFolderFile("file.bin", ("file.bin",), size, LINKS[0])
    tid = queue.add_debrid_files([entry], str(tmp_path))[0]
    task = queue.tasks[tid]
    task.speed_limit_kbps = 123
    payload = dict(filename=entry.name, filesize=size, link="https://node.debrid.it/file")
    if limit is not None:
        payload["max_chunks"] = limit
    fake = FakeSession({"/link/unlock": success(payload)})
    heads = []
    def head(*args, **kwargs):
        heads.append(True)
        return SimpleNamespace(ok=True, headers={"Content-Length": "200000000", "Accept-Ranges": "bytes"})
    fake.head = head
    monkeypatch.setattr(queue, "_bound_session", lambda: fake)
    speeds = []
    add_uri = rpc.add_uri
    def add(*args, **kwargs):
        speeds.append(args[3])
        return add_uri(*args, **kwargs)
    monkeypatch.setattr(rpc, "add_uri", add)
    inline(queue)
    queue._launch(task)
    expected = 1 if type(limit) is int and limit == 1 else 16
    assert rpc.added[0]["connections"] == task.segments == expected
    assert speeds == [123] and bool(heads) is probe


def test_plain_hoster_is_not_clamped(queue_env, monkeypatch):
    queue, rpc, _ = queue_env(connections_per_server=16, all_debrid_enabled=True,
                              all_debrid_api_key=APIKEY)
    fake = FakeSession({"/link/unlock": success(dict(filename="file.bin", filesize=1,
                       link="https://node.debrid.it/file", max_chunks=1))})
    # Resolve with the real AllDebrid decoder, bypassing only host discovery.
    monkeypatch.setattr(debrid, "resolve", lambda *args, **kw:
                        debrid.all_debrid_unrestrict("https://example.test/file", APIKEY, session=fake))
    inline(queue)
    tid = queue.add_url("https://example.test/file.bin")
    queue._launch(queue.tasks[tid])
    assert rpc.added[0]["connections"] == 16


def test_relaunch_reloads_and_clears_chunk_limit(queue_env, monkeypatch, tmp_path):
    queue, rpc, _ = queue_env(connections_per_server=16, all_debrid_api_key=APIKEY)
    entry = debrid.MegaFolderFile("file.bin", ("file.bin",), 1, LINKS[0])
    task = queue.tasks[queue.add_debrid_files([entry], str(tmp_path))[0]]
    responses = [success(dict(filename=entry.name, filesize=1, link="https://node.debrid.it/file", **extra))
                 for extra in ({"max_chunks": 1}, {"max_chunks": 2}, {})]
    fake = FakeSession({"/link/unlock": responses})
    monkeypatch.setattr(queue, "_bound_session", lambda: fake)
    inline(queue)
    for expected in (1, 2, 16):
        task.gid = None
        task.status = "queued"
        queue._launch(task)
        assert rpc.added[-1]["connections"] == expected
    assert len(fake.calls) == 3


def pump_until(predicate, timeout=8):
    import threading
    import time
    deadline = time.monotonic() + timeout
    tick = threading.Event()
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        tick.wait(.01)
    pytest.fail("Timed out waiting for loopback integration")


@pytest.fixture
def real_mega_io(tmp_path, monkeypatch, queue_env):
    """Isolated native aria2 + loopback HTTP, following real_proxy's skip/cleanup pattern."""
    import hashlib
    import os
    import shutil
    import socket
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from cove.aria2 import Aria2Error, Aria2RPC

    executable = shutil.which("aria2c")
    if executable is None:
        pytest.skip("real aria2c is not installed")
    payload = bytes(range(256)) * 2048
    state = SimpleNamespace(payload=payload, sha256=hashlib.sha256(payload).hexdigest(),
                            active=0, maximum=0, requests=[], release=threading.Event(),
                            started=threading.Event(), closed=threading.Event(), lock=threading.Lock())
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_HEAD(self):
            self.serve(False)
        def do_GET(self):
            self.serve(True)
        def serve(self, body):
            content = (b"<!doctype html><title>Landing</title>" if self.path == "/html"
                       else payload * (16 if self.path == "/slow" else 1))
            with state.lock:
                state.requests.append((self.command, self.path))
                state.active += 1
                state.maximum = max(state.maximum, state.active)
            try:
                start, end = 0, len(content) - 1
                ranged = self.headers.get("Range", "")
                if ranged.startswith("bytes="):
                    first, _, last = ranged[6:].partition("-")
                    start, end = int(first or 0), int(last) if last else end
                self.send_response(206 if ranged else 200)
                self.send_header("Content-Type", "text/html" if self.path == "/html" else "application/octet-stream")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Connection", "close")
                if ranged:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(content)}")
                self.end_headers()
                if body:
                    for offset in range(start, end + 1, 8192):
                        self.wfile.write(content[offset:min(offset + 8192, end + 1)])
                        self.wfile.flush()
                        if self.path == "/slow" and offset == start:
                            state.started.set()
                            state.release.wait(8)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with state.lock:
                    state.active -= 1
                if self.path == "/slow":
                    state.closed.set()
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
    conf = tmp_path / "integration-aria2.conf"
    conf.write_text("\n".join([
        "enable-rpc=true", "rpc-listen-all=false", f"rpc-listen-port={rpc_port}",
        "rpc-secret=synthetic-mega-test", "enable-dht=false", "enable-dht6=false",
        "enable-peer-exchange=false", "bt-enable-lpd=false", "disable-ipv6=true",
        "all-proxy=", "file-allocation=none", "max-tries=1", "connect-timeout=2", "timeout=10",
    ]) + "\n")
    child_env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    proc = subprocess.Popen([executable, f"--conf-path={conf}"], cwd=tmp_path, env=child_env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rpc = Aria2RPC(Settings(rpc_port=rpc_port, rpc_secret="synthetic-mega-test"), timeout=.5)
    queues, added, options = [], [], []
    rpc_call = rpc._call
    def record_call(method, params=(), **kwargs):
        if method == "aria2.addUri":
            options.append(dict(params[1]))
        return rpc_call(method, params, **kwargs)
    monkeypatch.setattr(rpc, "_call", record_call)
    add_uri = rpc.add_uri
    def record_add(*args, **kwargs):
        added.append(args)
        return add_uri(*args, **kwargs)
    monkeypatch.setattr(rpc, "add_uri", record_add)
    def ready():
        assert proc.poll() is None, "isolated aria2 exited at startup"
        try:
            return bool(rpc.get_version())
        except Aria2Error:
            return False
    def build(endpoint="/payload", *, restore=False):
        queue, _, _ = queue_env(connections_per_server=16, all_debrid_api_key=APIKEY,
                                intelligent_segments=False)
        queue.rpc = rpc
        queues.append(queue)
        node = f"http://127.0.0.1:{server.server_port}{endpoint}"
        size = len(payload) * (16 if endpoint == "/slow" else 1)
        if endpoint == "/html":
            size = len(b"<!doctype html><title>Landing</title>")
        fake = FakeSession({"/link/unlock": success(dict(filename="payload.bin", filesize=size,
                           link=node, max_chunks=1))})
        monkeypatch.setattr(queue, "_bound_session", lambda: fake)
        if restore:
            task = next(t for t in queue.tasks.values() if t.url == LINKS[0])
        else:
            entry = debrid.MegaFolderFile("payload.bin", ("Nested", "payload.bin"), size, LINKS[0])
            task = queue.tasks[queue.add_debrid_files([entry], str(tmp_path / "downloads"))[0]]
        return queue, task, fake
    state.build, state.rpc, state.added, state.options = build, rpc, added, options
    try:
        pump_until(ready)
        yield state
    finally:
        state.release.set()
        for queue in queues:
            queue._scheduler_allows = False
            queue._poll.stop()
            queue._ext_poll.stop()
        rpc.shutdown()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=3)
        for queue in queues:
            queue._pool.waitForDone(2000)
        QApplication.processEvents()
        rpc.close()
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)
        assert proc.poll() is not None and not serving.is_alive()


def real_complete(io, queue, task):
    queue._scheduler_allows = True
    queue._launch(task)
    def completed():
        assert task.status != "error", task.error
        if task.gid and task.id not in queue._publishing and task.status != "completed":
            queue._apply_status(task.id, io.rpc.tell_status(task.gid))
        return task.status == "completed"
    pump_until(completed)


def test_real_aria2_mega_completion_integrity(real_mega_io, tmp_path):
    import hashlib
    io = real_mega_io
    queue, task, fake = io.build()
    real_complete(io, queue, task)
    published = tmp_path / "downloads" / "Nested" / "payload.bin"
    assert Path(task.output_path) == published and task.filename == "payload.bin"
    assert hashlib.sha256(published.read_bytes()).hexdigest() == io.sha256
    assert published.read_bytes() == io.payload and not published.read_bytes().startswith(b"<")
    assert io.maximum == 1 and len(fake.calls) == 1
    assert io.added[0][2] == 1
    options = io.options[0]
    assert options["split"] == options["max-connection-per-server"] == "1"


def test_real_aria2_legacy_folder_is_never_launched(real_mega_io, tmp_path):
    io = real_mega_io
    queue, _, fake = io.build()
    # Construct the historical row directly, rather than passing admission.
    with db.connect() as conn:
        cur = conn.execute("INSERT INTO downloads (url,out_dir,status,created_at) VALUES (?,?,'queued',1)",
                           (URL, str(tmp_path / "legacy")))
        tid = cur.lastrowid
    queue._load_persisted()
    task = queue.tasks[tid]
    queue._launch(task)
    assert task.status == "error" and task.error == debrid.share_link_reason(URL)
    assert not io.added and not io.requests and not fake.calls
    assert io.rpc.tell_active() == []


def test_real_aria2_html_node_existing_behavior(real_mega_io):
    """There is no MIME guard for unlocked nodes; document the existing behavior."""
    io = real_mega_io
    queue, task, _ = io.build("/html")
    real_complete(io, queue, task)
    assert Path(task.output_path).read_bytes().startswith(b"<!doctype html>")


def test_real_aria2_mega_cancel_mid_body(real_mega_io):
    io = real_mega_io
    queue, task, _ = io.build("/slow")
    queue._scheduler_allows = True
    queue._launch(task)
    pump_until(lambda: io.started.is_set() and bool(task.gid))
    gid = task.gid
    assert io.rpc.tell_status(gid)["status"] == "active"
    queue.remove(task.id, delete_file=True)
    pump_until(lambda: task.id not in queue.tasks)
    io.release.set()
    pump_until(lambda: io.closed.is_set() and io.active == 0)
    assert not (Path(task.out_dir) / task.filename).exists()
    assert io.rpc.tell_active() == []
    assert io.rpc._call("aria2.tellWaiting", [0, 100]) == []
    assert io.rpc.tell_status(gid)["status"] == "removed"
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM downloads WHERE id=?", (task.id,)).fetchone()[0] == 0


def test_real_aria2_mega_restart_unlocks_again(real_mega_io):
    io = real_mega_io
    first, task, first_fake = io.build()
    # A launch resolved in the previous process, but the node URL was never persisted.
    assert first._resolve_debrid(task).startswith("http://127.0.0.1:")
    first._persist(task)
    with db.connect() as conn:
        row = dict(conn.execute("SELECT * FROM downloads WHERE id=?", (task.id,)).fetchone())
    assert row["url"] == LINKS[0] and KEY not in repr(row) and "127.0.0.1" not in repr(row)
    fresh, restored, second_fake = io.build(restore=True)
    assert restored.resolved_url == ""
    real_complete(io, fresh, restored)
    assert len(first_fake.calls) == len(second_fake.calls) == 1
    assert Path(restored.output_path).read_bytes() == io.payload


@pytest.mark.parametrize("delayed", [False, True])
@pytest.mark.parametrize("size", [2**63, 2**63 - 1, -1, True, "1", float("inf"), float("nan")])
def test_unlock_size_cannot_poison_persistence(queue_env, monkeypatch, tmp_path, size, delayed):
    queue, rpc, _ = queue_env(all_debrid_api_key=APIKEY)
    entry = debrid.MegaFolderFile("safe.bin", ("safe.bin",), 1, LINKS[0])
    task = queue.tasks[queue.add_debrid_files([entry], str(tmp_path))[0]]
    payload = dict(filename="untrusted/other.bin", filesize=size, max_chunks=1)
    if delayed:
        payload["delayed"] = 1
    else:
        payload["link"] = "https://node.debrid.it/file"
    fake = FakeSession({"/link/unlock": success(payload),
                        "/link/delayed": success(dict(status=2, link="https://node.debrid.it/file"))})
    monkeypatch.setattr(debrid, "DELAYED_POLL_INTERVAL", 0)
    monkeypatch.setattr(queue, "_bound_session", lambda: fake)
    inline(queue)
    queue._launch(task)
    valid = type(size) is int and size == 2**63 - 1
    if valid:
        assert len(rpc.added) == 1 and task.total_bytes == size
        assert task.status in ("active", "paused")
    else:
        assert not rpc.added
        assert task.status == "error"
        assert task.error in {
            str(debrid._mega_error(debrid._MEGA_BAD_LIST)),
            str(debrid._bad_response(debrid.ALL_DEBRID)),
        }
        assert task.total_bytes == 1
        assert KEY not in task.error and APIKEY not in task.error
        # A subsequent lifecycle write must remain safe after the failed launch.
        task.status = "paused"
        queue._persist(task)
    with db.connect() as conn:
        row = dict(conn.execute("SELECT * FROM downloads WHERE id=?", (task.id,)).fetchone())
    assert row["total_bytes"] == (size if valid else 1)
    assert row["filename"] == task.filename == "safe.bin"
    assert KEY not in repr(row) and APIKEY not in repr(row)
