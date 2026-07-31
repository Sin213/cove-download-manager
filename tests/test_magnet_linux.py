"""Linux magnet handler: desktop entry authoring and xdg-mime calls.

Every subprocess is injected. These tests never run xdg-mime and never
write to the real ~/.local/share/applications.
"""
import cove.magnet_identity as mi
from cove import magnet_linux as ml


class FakeRunner:
    """Records argv lists and replays scripted (exit_code, stdout) results."""

    def __init__(self, results=None, missing=False):
        self.calls = []
        self.results = dict(results or {})
        self.missing = missing

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.missing:
            raise FileNotFoundError(argv[0])
        return self.results.get(argv[0], (0, ""))


def test_debian_and_appimage_ids_never_collide():
    assert ml.desktop_id(mi.DEBIAN) == "cove-download-manager.desktop"
    assert ml.desktop_id(mi.APPIMAGE) == "cove-download-manager-appimage.desktop"
    assert ml.desktop_id(mi.DEBIAN) != ml.desktop_id(mi.APPIMAGE)


def test_exec_escaping_handles_spaces_quotes_and_percent():
    assert ml.escape_exec("/home/a b/Cove.AppImage") == '"/home/a b/Cove.AppImage"'
    # A literal percent must be doubled or the desktop file is unlaunchable.
    assert ml.escape_exec("/tmp/100%/Cove") == '"/tmp/100%%/Cove"'
    assert ml.escape_exec('/tmp/we"ird/Cove') == '"/tmp/we\\"ird/Cove"'
    assert ml.escape_exec("/tmp/do$llar/Cove") == '"/tmp/do\\$llar/Cove"'
    assert ml.escape_exec("/tmp/back\\slash/Cove") == '"/tmp/back\\\\slash/Cove"'


def test_entry_declares_the_magnet_scheme_and_round_trips_the_path():
    text = ml.entry_text("/opt/Cove 3.2.0.AppImage")
    assert "MimeType=x-scheme-handler/magnet;" in text
    assert 'Exec="/opt/Cove 3.2.0.AppImage" %u' in text
    assert ml.entry_exec_path(text) == "/opt/Cove 3.2.0.AppImage"


def test_entry_exec_path_round_trips_a_percent():
    text = ml.entry_text("/tmp/100%/Cove.AppImage")
    assert ml.entry_exec_path(text) == "/tmp/100%/Cove.AppImage"


def test_entry_exec_path_of_junk_is_empty():
    assert ml.entry_exec_path("[Desktop Entry]\nName=x\n") == ""


def test_write_user_entry_creates_the_file_and_refreshes_the_database(tmp_path):
    run = FakeRunner()
    ml.write_user_entry(
        ml.desktop_id(mi.APPIMAGE), "/opt/Cove.AppImage", tmp_path, run
    )
    written = tmp_path / "cove-download-manager-appimage.desktop"
    assert written.is_file()
    assert 'Exec="/opt/Cove.AppImage" %u' in written.read_text()
    assert ["update-desktop-database", str(tmp_path)] in run.calls


def test_a_missing_update_desktop_database_is_not_fatal(tmp_path):
    run = FakeRunner(missing=True)
    ml.write_user_entry(
        ml.desktop_id(mi.APPIMAGE), "/opt/Cove.AppImage", tmp_path, run
    )
    assert (tmp_path / "cove-download-manager-appimage.desktop").is_file()


def test_set_default_reports_only_what_the_query_confirms():
    desktop_id = ml.desktop_id(mi.APPIMAGE)
    confirmed = FakeRunner(results={"xdg-mime": (0, desktop_id + "\n")})
    assert ml.set_default(confirmed, desktop_id) is True
    assert ["xdg-mime", "default", desktop_id, ml.MAGNET_MIME] in confirmed.calls
    assert ["xdg-mime", "query", "default", ml.MAGNET_MIME] in confirmed.calls


def test_set_default_is_false_when_the_desktop_declined():
    desktop_id = ml.desktop_id(mi.APPIMAGE)
    declined = FakeRunner(results={"xdg-mime": (0, "org.qbittorrent.qBittorrent.desktop\n")})
    assert ml.set_default(declined, desktop_id) is False


def test_set_default_is_false_when_xdg_mime_is_missing():
    assert ml.set_default(FakeRunner(missing=True), ml.desktop_id(mi.APPIMAGE)) is False


def test_query_default_is_empty_when_xdg_mime_is_missing():
    assert ml.query_default(FakeRunner(missing=True)) == ""


def test_remove_user_entry_is_idempotent(tmp_path):
    desktop_id = ml.desktop_id(mi.APPIMAGE)
    ml.remove_user_entry(desktop_id, tmp_path)  # absent: must not raise
    (tmp_path / desktop_id).write_text("x")
    ml.remove_user_entry(desktop_id, tmp_path)
    assert not (tmp_path / desktop_id).exists()
