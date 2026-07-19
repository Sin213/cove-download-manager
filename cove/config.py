import json
import os
import secrets
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List

from .portable import is_portable, portable_data_dir

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

    @classmethod
    def load(cls) -> "Settings":
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_FILE.exists():
            s = cls()
            s.rpc_secret = _new_rpc_secret()
            s.api_token = _new_distinct_api_token(s.rpc_secret)
            s.save()
            return s
        try:
            raw = json.loads(CONFIG_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            raw = None
        if not isinstance(raw, dict):
            # Unreadable or corrupted (valid JSON that isn't an object):
            # start over with defaults rather than crashing on load.
            s = cls()
            s.rpc_secret = _new_rpc_secret()
            s.api_token = _new_distinct_api_token(s.rpc_secret)
            s.save()
            return s
        speed_limit_unit_missing = "speed_limit_unit" not in raw

        def _sub_fields(data, klass):
            """Keyword args for a nested dataclass, dropping unknown keys
            and tolerating a non-dict value from a hand-edited file."""
            if not isinstance(data, dict):
                return {}
            return {k: v for k, v in data.items() if k in klass.__annotations__}

        sched = ScheduleWindow(**_sub_fields(raw.pop("schedule", None), ScheduleWindow))
        sched_reset = not _schedule_valid(sched)
        if sched_reset:
            sched = ScheduleWindow()
        cat = CategoryDirs(**_sub_fields(raw.pop("category_dirs", None), CategoryDirs))
        s = cls(**{k: v for k, v in raw.items() if k in cls.__annotations__})
        s.schedule = sched
        s.category_dirs = cat
        if s.theme not in ("dark", "light"):
            s.theme = "dark"
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
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, CONFIG_FILE)
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass
