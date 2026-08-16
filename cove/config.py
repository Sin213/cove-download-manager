import errno
import json
import os
import secrets
import tempfile
import threading
import time
import typing
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List

from .portable import is_portable, portable_data_dir
from .search.indexers import CustomTorznabIndexer, parse_custom_indexers

if is_portable():
    _portable = Path(portable_data_dir("cove-download-manager"))
    CONFIG_DIR = _portable
    DATA_DIR = _portable
else:
    # Per the XDG spec an empty env var must be treated as unset, otherwise
    # these resolve to a relative "cove" in the current working directory.
    CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "cove"
    DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "cove"
CONFIG_FILE = CONFIG_DIR / "settings.json"
DB_FILE = DATA_DIR / "cove.db"
ARIA2_SESSION = DATA_DIR / "aria2.session"
ARIA2_LOG = DATA_DIR / "aria2.log"
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads"
DEFAULT_API_PORT = 17681
# Stock aria2 validates --max-connection-per-server in the range 1-16.
MAX_CONNECTIONS_PER_SERVER = 16

# Legacy default. Anything matching this on load is upgraded to a fresh
# random secret so existing installs stop using the predictable token.
_LEGACY_RPC_SECRET = "cove"

# Guards Settings.save() against interleaving. The magnet self-heal daemon
# thread (cove/magnet_startup.py) is the first caller of save() off the GUI
# thread, so two threads can now race to write settings.json at once. Each
# save uses its own unique temp file (see save()), so the lock's only job is
# to serialize the read-modify-os.replace sequence - without it, one
# thread's os.replace can publish the other thread's half-written file.
_SAVE_LOCK = threading.Lock()

# Windows shares files by handle, not by inode. CPython's open() asks for
# FILE_SHARE_READ | FILE_SHARE_WRITE but *not* FILE_SHARE_DELETE, so while any
# reader holds settings.json open, MoveFileExW cannot unlink the destination
# and os.replace fails with ERROR_ACCESS_DENIED (5); the reverse race makes a
# reader fail against a delete-pending target with the same code. Neither is a
# real failure - the loser just has to look again a moment later. Retrying
# keeps the publication atomic (a reader still never observes a partial file)
# while making save() and load() survive the interleaving. Nothing to do on
# POSIX, where rename(2) is unaffected by open descriptors.
_WINDOWS_SHARING_ERRORS = frozenset((5, 32, 33))
_SHARING_RETRY_SECONDS = 2.0
_SHARING_RETRY_BACKOFF = 0.001


def _is_transient_sharing_error(exc: OSError) -> bool:
    if os.name != "nt":
        return False
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in _WINDOWS_SHARING_ERRORS
    # os.replace surfaces the Win32 code, but open() goes through the CRT and
    # raises a bare PermissionError(EACCES) with no winerror at all, so the
    # read side has to be recognised by errno. A genuine ACL denial also lands
    # here; it just costs one retry window before being re-raised.
    return exc.errno == errno.EACCES


def _retry_on_sharing_error(operation):
    """Run ``operation`` until it stops losing a Windows sharing race."""

    deadline = time.monotonic() + _SHARING_RETRY_SECONDS
    delay = _SHARING_RETRY_BACKOFF
    while True:
        try:
            return operation()
        except OSError as exc:
            if not _is_transient_sharing_error(exc) or time.monotonic() >= deadline:
                raise
        time.sleep(delay)
        # The contended window is microseconds wide, so the cap stays small:
        # backing off further just adds latency without reducing contention.
        delay = min(delay * 2, 0.005)

# Debrid providers Cove can resolve links through. cove.debrid imports these
# so the accepted setting values and the resolver can't drift apart. Order
# here is also the deterministic fallback order after the preferred provider:
# cove.debrid._enabled_providers walks this tuple to build the rest of the
# chain, so appending a provider is what puts it last in that chain.
DEBRID_ALL_DEBRID = "alldebrid"
DEBRID_REAL_DEBRID = "real_debrid"
DEBRID_TORBOX = "torbox"
DEBRID_PROVIDERS = (DEBRID_ALL_DEBRID, DEBRID_REAL_DEBRID, DEBRID_TORBOX)
DEBRID_DEFAULT_PROVIDER = DEBRID_ALL_DEBRID

# What Cove does with a torrent no enabled debrid provider has cached.
#   "automatic"  download it locally through Cove's own aria2 BitTorrent
#   "never"      fail the task instead of joining a swarm
# There is deliberately no "ask every time" mode yet.
TORRENT_FALLBACK_AUTOMATIC = "automatic"
TORRENT_FALLBACK_NEVER = "never"
TORRENT_FALLBACK_MODES = (TORRENT_FALLBACK_AUTOMATIC, TORRENT_FALLBACK_NEVER)


@dataclass
class ScheduleWindow:
    enabled: bool = False
    start_hour: int = 2
    start_minute: int = 0
    end_hour: int = 6
    end_minute: int = 0
    days: List[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])


def _schedule_valid(w: ScheduleWindow) -> bool:
    """Range/type check for persisted schedule values; anything invalid
    would raise later when converted into datetime.time/QTime."""
    def _int_in(v, lo, hi):
        return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi
    return (
        isinstance(w.enabled, bool)
        and _int_in(w.start_hour, 0, 23)
        and _int_in(w.end_hour, 0, 23)
        and _int_in(w.start_minute, 0, 59)
        and _int_in(w.end_minute, 0, 59)
        and isinstance(w.days, list)
        and all(_int_in(d, 0, 6) for d in w.days)
    )


CONNECTION_CHOICES = (1, 2, 4, 8, MAX_CONNECTIONS_PER_SERVER)

CATEGORY_NAMES = ("Documents", "Videos", "Music", "Archives", "Programs", "Images")

CATEGORY_MAP: dict[str, str] = {
    "pdf": "Documents", "doc": "Documents", "docx": "Documents",
    "odt": "Documents", "txt": "Documents", "md": "Documents",
    "epub": "Documents", "xls": "Documents", "xlsx": "Documents",
    "ppt": "Documents", "pptx": "Documents", "csv": "Documents",
    "mp4": "Videos", "mkv": "Videos", "webm": "Videos",
    "mov": "Videos", "avi": "Videos", "flv": "Videos", "wmv": "Videos",
    "m3u8": "Videos",
    "mp3": "Music", "flac": "Music", "wav": "Music",
    "ogg": "Music", "m4a": "Music", "aac": "Music",
    "zip": "Archives", "rar": "Archives", "7z": "Archives",
    "tar": "Archives", "gz": "Archives", "bz2": "Archives",
    "xz": "Archives",
    "exe": "Programs", "msi": "Programs", "deb": "Programs",
    "rpm": "Programs", "dmg": "Programs", "pkg": "Programs",
    "appimage": "Programs",
    "jpg": "Images", "jpeg": "Images", "png": "Images",
    "gif": "Images", "webp": "Images", "svg": "Images",
    "bmp": "Images", "ico": "Images", "tiff": "Images",
}


def categorize(url: str) -> str:
    from urllib.parse import urlparse
    path = urlparse(url).path
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return CATEGORY_MAP.get(ext, "Other")


@dataclass
class CategoryDirs:
    Documents: str = ""
    Videos: str = ""
    Music: str = ""
    Archives: str = ""
    Programs: str = ""
    Images: str = ""

def _new_rpc_secret() -> str:
    """Per-install random token. 24 bytes ≈ 32 chars urlsafe-base64."""
    return secrets.token_urlsafe(24)


def _new_api_token() -> str:
    """Return a secret used only by Cove's first-party loopback API."""
    return secrets.token_urlsafe(32)


def _new_distinct_api_token(rpc_secret: str) -> str:
    token = _new_api_token()
    while token == rpc_secret:
        token = _new_api_token()
    return token


_SCALAR_ANNOTATIONS = {"str": str, "int": int, "bool": bool, "float": float}


def _is_list_annotation(annotation) -> bool:
    """Whether an annotation names a list (`List[int]`, `list[int]`, `list`)."""
    if isinstance(annotation, str):
        return annotation.replace(" ", "").lower().startswith("list")
    return annotation is list or typing.get_origin(annotation) is list


def _field_kind(annotation):
    """How `_well_typed_fields` handles this annotation, or None if it cannot.

    The single source of truth for both the validator and `understands`, so the
    two cannot drift apart and leave the guard test passing over a field that
    is in fact being dropped.
    """
    scalar = _scalar_annotation(annotation)
    if scalar is not None:
        return ("scalar", scalar)
    if _is_list_annotation(annotation):
        return ("list", list)
    return None


def understands(annotation) -> bool:
    """Whether _well_typed_fields can type-check this annotation.

    Anything it cannot check is dropped, which silently discards the user's
    stored value - so a new field of an unhandled kind must be caught by a test
    rather than by someone noticing their settings reverted.
    """
    return _field_kind(annotation) is not None


def _scalar_annotation(annotation):
    """The scalar type an annotation names, or None if it is not one.

    Accepts both forms: dataclass annotations are real type objects here, but
    become strings under `from __future__ import annotations`, and this must
    not quietly start accepting everything if that import is ever added.
    """
    if isinstance(annotation, str):
        return _SCALAR_ANNOTATIONS.get(annotation)
    if annotation in (str, int, bool, float):
        return annotation
    return None


def _well_typed_fields(klass, raw: dict) -> dict:
    """Keyword args for `klass`, dropping keys whose value is the wrong type.

    Recognising a key was never enough. Consumers do arithmetic on the numbers,
    compare the strings and test the booleans for truth, so a hand-edited,
    partially migrated or corrupted file could crash a feature outright - or,
    worse, silently invert one, because a non-empty string like "false" is
    truthy. A rejected value simply leaves the dataclass default in place.

    bool is checked before int deliberately: in Python `True` is an int, and a
    boolean where a count belongs is a mistake, not a 1.
    """
    out = {}
    for key, value in raw.items():
        kind = _field_kind(klass.__annotations__.get(key))
        if kind is None:
            continue  # unknown key, or a nested dataclass handled separately
        label, expected = kind
        if label == "list":
            # Element-level checking belongs to the owner: ScheduleWindow.days
            # is range-checked by _schedule_valid, which resets the whole
            # window if any day is wrong. Dropping the list here instead threw
            # the user's saved day selection away.
            if isinstance(value, list):
                out[key] = value
        elif expected is bool:
            if isinstance(value, bool):
                out[key] = value
        elif expected is int:
            if isinstance(value, int) and not isinstance(value, bool):
                out[key] = value
        elif isinstance(value, expected):
            out[key] = value
    return out


@dataclass
class Settings:
    download_dir: str = str(DEFAULT_DOWNLOAD_DIR)
    connections_per_server: int = 16
    max_concurrent: int = 1
    overall_speed_limit_kbps: int = 0
    speed_limiter_enabled: bool = False
    speed_limit_unit: str = "KB/s"
    time_format_24h: bool = False  # default: 12-hour with AM/PM
    auto_update_check: bool = True
    delete_completed_on_exit: bool = False
    # Pressing the window X hides Cove to the system tray instead of exiting,
    # keeping it available for browser downloads. Off by default so the
    # existing "X quits Cove" behavior is unchanged unless the user opts in.
    close_to_tray: bool = False
    theme: str = "dark"  # "dark" | "light"
    rpc_port: int = 6800
    rpc_secret: str = ""  # populated on first save; never persisted as "cove"
    proxy_type: str = "none"  # "none" | "http" | "https" | "socks5"
    proxy_host: str = ""
    proxy_port: int = 0
    proxy_username: str = ""
    proxy_password: str = ""
    intelligent_segments: bool = True
    notify_on_complete: bool = True
    notify_on_error: bool = True
    auto_sort_by_category: bool = False
    category_dirs: CategoryDirs = field(default_factory=CategoryDirs)
    schedule: ScheduleWindow = field(default_factory=ScheduleWindow)
    api_enabled: bool = True
    api_port: int = DEFAULT_API_PORT
    api_token: str = ""  # distinct from rpc_secret; never returned by the API
    # Debrid accounts. Credentials live here alongside the other secrets in
    # the 0600 settings file; the local API never serializes Settings, so
    # they are not reachable over the loopback API.
    all_debrid_enabled: bool = False
    all_debrid_api_key: str = ""
    real_debrid_enabled: bool = False
    real_debrid_api_token: str = ""
    # TorBox: the account setting below only means "the user has enabled and
    # configured their TorBox account". It is separate from the internal
    # feature-availability gate in cove.debrid (TORBOX_FEATURE_AVAILABLE),
    # which stays off until the full feature (hoster + cached torrent) ships.
    torbox_enabled: bool = False
    torbox_api_token: str = ""
    debrid_preferred_provider: str = "alldebrid"  # "alldebrid" | "real_debrid" | "torbox"
    # Torrent support. Magnets and .torrent files are checked against the
    # enabled debrid providers first; anything they don't have cached falls
    # back to Cove's own aria2 BitTorrent engine (see torrent_fallback_mode).
    torrent_support_enabled: bool = False
    torrent_fallback_mode: str = TORRENT_FALLBACK_AUTOMATIC
    # Cove's HTTP proxy settings cannot cover peer, DHT and UDP tracker
    # traffic, so a configured proxy blocks local BitTorrent unless the user
    # explicitly overrides it.
    torrent_allow_with_proxy: bool = False
    # Interface aria2 binds its sockets to, "" meaning "any interface".
    # Cove runs one shared aria2 daemon, so this binds every aria2-managed
    # transfer, not only torrents. If the name is gone at launch time Cove
    # refuses to start aria2 rather than picking another adapter.
    torrent_network_interface: str = ""
    # Set once the user has accepted the one-time P2P privacy disclosure.
    # Not a user-facing checkbox: it records a decision, it isn't an option.
    torrent_ip_disclosure_shown: bool = False
    # Whether Cove keeps its magnet-handler registration repaired after an
    # update changes the executable path. This is NOT a claim that Cove is
    # the current default: only the OS knows that, and it is read live.
    magnet_handler_enabled: bool = False
    # Records that the one-time "make Cove your magnet handler" offer has
    # been made. Like torrent_ip_disclosure_shown, this stores a decision,
    # it is not a user-facing option.
    magnet_prompt_shown: bool = False
    # Whether the first-run "install the browser extension" banner has been
    # answered (dismissed, acted on, or made moot by a connected extension).
    extension_prompt_shown: bool = False
    # User-configured generic Torznab indexers. These are dormant configuration
    # in S2: each record describes an indexer (stable id, enabled, display
    # name, full endpoint, optional api_key) so a later slice can build a
    # network-backed source from it. No network I/O, SearchService, registry,
    # or UI reads this yet, and configured order is preserved as entered.
    custom_indexers: List[CustomTorznabIndexer] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Settings":
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_FILE.exists():
            s = cls()
            s.rpc_secret = _new_rpc_secret()
            s.api_token = _new_distinct_api_token(s.rpc_secret)
            s.save()
            s.magnet_setting_missing = False
            return s
        # Retry first, so a brief sharing race is not mistaken for anything.
        # A read that still fails afterwards propagates deliberately: the file
        # exists but could not be read, which is not corruption. Treating it as
        # corruption would run the fallback below, regenerating rpc_secret and
        # api_token and overwriting every stored setting with defaults - so a
        # backup or antivirus agent holding settings.json open for longer than
        # the retry window would silently destroy the user's configuration and
        # rotate their secrets. Failing closed leaves the file intact.
        text = _retry_on_sharing_error(CONFIG_FILE.read_text)
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            raw = None
        if not isinstance(raw, dict):
            # Genuinely corrupt: unparseable, or valid JSON that isn't an
            # object. Start over with defaults rather than crashing on load.
            s = cls()
            s.rpc_secret = _new_rpc_secret()
            s.api_token = _new_distinct_api_token(s.rpc_secret)
            s.save()
            s.magnet_setting_missing = False
            return s
        speed_limit_unit_missing = "speed_limit_unit" not in raw
        magnet_setting_missing = "magnet_handler_enabled" not in raw

        def _sub_fields(data, klass):
            """Keyword args for a nested dataclass, dropping unknown keys
            and tolerating a non-dict value from a hand-edited file.

            Typed on the same terms as the top-level fields: a nested setting
            is no less reachable by a hand-edited file, and its consumers are
            no better at surviving a string where a path or a count belongs.
            """
            if not isinstance(data, dict):
                return {}
            return _well_typed_fields(klass, data)

        sched = ScheduleWindow(**_sub_fields(raw.pop("schedule", None), ScheduleWindow))
        sched_reset = not _schedule_valid(sched)
        if sched_reset:
            sched = ScheduleWindow()
        cat = CategoryDirs(**_sub_fields(raw.pop("category_dirs", None), CategoryDirs))
        indexers = parse_custom_indexers(raw.pop("custom_indexers", None))
        s = cls(**_well_typed_fields(cls, raw))
        s.schedule = sched
        s.category_dirs = cat
        s.custom_indexers = indexers
        if s.theme not in ("dark", "light"):
            s.theme = "dark"
        # A hand-edited non-boolean must not be read as "enabled" via Python
        # truthiness - that would hide the window on close with no opt-in.
        if not isinstance(s.close_to_tray, bool):
            s.close_to_tray = False
        # Same reasoning as close_to_tray: a hand-edited non-boolean must not
        # be read as "enabled" via Python truthiness.
        if not isinstance(s.magnet_handler_enabled, bool):
            s.magnet_handler_enabled = False
        if not isinstance(s.magnet_prompt_shown, bool):
            s.magnet_prompt_shown = False
        # Same reasoning again: a truthy non-boolean must not silence a
        # prompt the user has never actually answered.
        if not isinstance(s.extension_prompt_shown, bool):
            s.extension_prompt_shown = False
        # Not a dataclass field, so it is never written back to settings.json.
        # Task 6 consumes it once, at startup, to migrate existing opt-ins.
        s.magnet_setting_missing = magnet_setting_missing
        # Migrate legacy / empty / suspiciously-short secrets up to a real one.
        changed = speed_limit_unit_missing or sched_reset
        if (
            not isinstance(s.rpc_secret, str)
            or not s.rpc_secret
            or s.rpc_secret == _LEGACY_RPC_SECRET
            or len(s.rpc_secret) < 16
        ):
            s.rpc_secret = _new_rpc_secret()
            changed = True
        if (
            not isinstance(s.api_token, str)
            or not s.api_token
            or len(s.api_token) < 24
            or s.api_token == s.rpc_secret
        ):
            s.api_token = _new_distinct_api_token(s.rpc_secret)
            changed = True
        if isinstance(s.api_port, bool) or not isinstance(s.api_port, int) or not 1 <= s.api_port <= 65535:
            s.api_port = DEFAULT_API_PORT
            changed = True
        if isinstance(s.rpc_port, bool) or not isinstance(s.rpc_port, int) or not 1 <= s.rpc_port <= 65535:
            s.rpc_port = 6800
            changed = True
        if not isinstance(s.api_enabled, bool):
            s.api_enabled = True
            changed = True
        # Debrid: a hand-edited or partially-written file must never leave a
        # provider enabled with a non-string credential, or the resolver
        # would try to send it as a bearer header.
        for flag in ("all_debrid_enabled", "real_debrid_enabled", "torbox_enabled"):
            if not isinstance(getattr(s, flag), bool):
                setattr(s, flag, False)
                changed = True
        for credential in ("all_debrid_api_key", "real_debrid_api_token", "torbox_api_token"):
            if not isinstance(getattr(s, credential), str):
                setattr(s, credential, "")
                changed = True
        # Torrent flags: a hand-edited file must never leave local
        # BitTorrent enabled, unblocked by the proxy guard, or holding a
        # consent Cove never actually asked for.
        for flag in (
            "torrent_support_enabled",
            "torrent_allow_with_proxy",
            "torrent_ip_disclosure_shown",
        ):
            if not isinstance(getattr(s, flag), bool):
                setattr(s, flag, False)
                changed = True
        # A hand-edited non-string interface is meaningless; "" (any) is the
        # only safe repair, and it is visible in Settings.
        if not isinstance(s.torrent_network_interface, str):
            s.torrent_network_interface = ""
            changed = True
        if s.torrent_fallback_mode not in TORRENT_FALLBACK_MODES:
            s.torrent_fallback_mode = TORRENT_FALLBACK_AUTOMATIC
            changed = True
        if s.debrid_preferred_provider not in DEBRID_PROVIDERS:
            s.debrid_preferred_provider = DEBRID_DEFAULT_PROVIDER
            changed = True
        if s.speed_limit_unit not in ("KB/s", "MB/s"):
            s.speed_limit_unit = "KB/s"
            changed = True
        if (
            isinstance(s.connections_per_server, bool)
            or not isinstance(s.connections_per_server, int)
        ):
            s.connections_per_server = MAX_CONNECTIONS_PER_SERVER
            changed = True
        elif s.connections_per_server < 1:
            s.connections_per_server = 1
            changed = True
        elif s.connections_per_server > MAX_CONNECTIONS_PER_SERVER:
            s.connections_per_server = MAX_CONNECTIONS_PER_SERVER
            changed = True
        if changed:
            s.save()
        return s

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        # Write atomically with restrictive perms so the RPC secret isn't
        # readable by other local users. NOTE: chmod 0o600 only restricts
        # access on POSIX; on Windows it's effectively a no-op and the file
        # inherits the parent directory's ACL.
        #
        # The magnet self-heal daemon thread can call save() concurrently
        # with the GUI thread. A shared fixed tmp path would let one
        # thread's os.replace publish the other thread's half-written file,
        # so each call gets its own unique temp file in CONFIG_DIR (same
        # filesystem, so os.replace stays atomic), and the lock serializes
        # the whole write-then-replace sequence so the two saves can't
        # interleave at all.
        with _SAVE_LOCK:
            fd, tmp_name = tempfile.mkstemp(
                prefix=CONFIG_FILE.name + ".", suffix=".tmp", dir=str(CONFIG_DIR)
            )
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(json.dumps(data, indent=2))
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
                _retry_on_sharing_error(lambda: os.replace(tmp, CONFIG_FILE))
            except BaseException:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            try:
                os.chmod(CONFIG_FILE, 0o600)
            except OSError:
                pass
