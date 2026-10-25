"""Strict share parsing and short-lived one-shot browser consent. No Qt or HTTP."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from .output_paths import OutputPathError, validate_public_filename

ALLOWED_HOST = "fuckingfast.co"
TICKET_TTL = 100
_MAX_TICKET_BYTES = 65536  # Includes a 4096-character directory with JSON Unicode escaping.
MARKER = "cove-fetch"
BROWSER_REQUIRED = ("This Fuckingfast.co page needs your browser; add it through Add Download with "
                    "the Cove Firefox extension, or download it in your browser.")
RESERVED = frozenset("help login logout terms privacy contact speed speedtest register signup "
                     "upload api static about faq dmca account admin dashboard".split())
_ID = re.compile(r"[A-Za-z0-9_-]{4,64}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_PATH = re.compile(r"/(?:f/)?([A-Za-z0-9_-]{4,64})/?\Z")


@dataclass(frozen=True)
class ShareLink:
    file_id: str
    page_url: str
    filename_hint: str


def _valid_id(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value)) and value.lower() not in RESERVED


def parse_share_url(url: str) -> ShareLink | None:
    if not isinstance(url, str) or not url or any(
        c.isspace() or ord(c) < 32 or 127 <= ord(c) <= 159 or c == "\\" for c in url
    ):
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme.lower() not in ("http", "https")
                or parts.hostname not in (ALLOWED_HOST, "www." + ALLOWED_HOST)
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443)
                or (":" in parts.netloc and not parts.netloc.endswith(":443"))):
            return None
        match = _PATH.fullmatch(parts.path)
        if not match or not _valid_id(match[1]):
            return None
        try:
            hint = validate_public_filename(unquote(parts.fragment, errors="strict"))
        except (OutputPathError, UnicodeError):
            hint = ""
        return ShareLink(match[1], f"https://{parts.hostname}/{match[1]}", hint)
    except ValueError:
        return None


def is_landing_page_url(url) -> bool:
    """Conservatively block apex/www pages; never authorize a browser click.

    Downloaders differ in handling malformed authorities. Check both slash
    interpretations of backslashes, ignoring whitespace and controls, and
    retain a textual authority fallback for URLs urlsplit cannot parse.
    """
    if not isinstance(url, str):
        return False
    cleaned = "".join(c for c in url if not (
        c.isspace() or ord(c) < 32 or 127 <= ord(c) <= 159
    ))
    for candidate in (cleaned.replace("\\", "/"), cleaned.replace("\\", "")):
        match = re.match(r"https?:/+([^/?#]*)", candidate, re.IGNORECASE)
        if not match:
            continue
        try:
            host = urlsplit(candidate).hostname
        except ValueError:
            host = None
        if not host:
            host = match[1].rsplit("@", 1)[-1].split(":", 1)[0].strip("[]")
        if host.rstrip(".").lower() in (ALLOWED_HOST, "www." + ALLOWED_HOST):
            return True
    return False


def share_link_reason(url) -> str:
    return BROWSER_REQUIRED if is_landing_page_url(url) else ""


def _directory(data_dir, create=False):
    path = Path(data_dir) / "browser-fetch"
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or (os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077))):
        raise OSError("Unsafe browser ticket directory")
    return path


def _nonce(value):
    return isinstance(value, str) and bool(_NONCE.fullmatch(value))


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate ticket field")
        result[key] = value
    return result


def _read(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_TICKET_BYTES or path.is_symlink()
                or (os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077))):
            return None
        return json.loads(os.read(fd, _MAX_TICKET_BYTES), object_pairs_hook=_json_object)
    finally:
        os.close(fd)


def _sweep(folder, now):
    for path in folder.iterdir():
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            value = _read(path)
            expired = (isinstance(value, dict) and type(value.get("exp")) in (int, float)
                       and value["exp"] <= now)
            if expired or info.st_mtime < now - 300:
                path.unlink()
        except (OSError, ValueError, TypeError):
            # Foreign malformed files may be swept only once they are stale.
            try:
                if path.lstat().st_mtime < now - 300 and path.is_file() and not path.is_symlink():
                    path.unlink()
            except OSError:
                pass


def _clean_directory(value):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)):
        return ""
    return value


def issue_ticket(data_dir, link, now=None, directory=None) -> str:
    try:
        if not isinstance(link, ShareLink) or not _valid_id(link.file_id):
            return ""
        now = time.time() if now is None else now
        folder = _directory(data_dir, create=True)
        _sweep(folder, now)
        nonce = secrets.token_hex(16)
        payload = json.dumps({"id": link.file_id, "hint": link.filename_hint,
                              "exp": now + TICKET_TTL, "dir": _clean_directory(directory)})
        fd = os.open(folder / (nonce + ".json"),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
        return nonce
    except (OSError, ValueError, TypeError):
        return ""


def ticket_exists(data_dir, nonce) -> bool:
    if not _nonce(nonce):
        return False
    try:
        path = _directory(data_dir) / (nonce + ".json")
        info = path.lstat()
        return stat.S_ISREG(info.st_mode) and not path.is_symlink()
    except (OSError, TypeError, ValueError):
        return False


def revoke_ticket(data_dir, nonce) -> bool:
    if not _nonce(nonce):
        return False
    try:
        (_directory(data_dir) / (nonce + ".json")).unlink()
        return True
    except (OSError, TypeError, ValueError):
        return False


def consume_ticket(data_dir, nonce, file_id, now=None):
    if not _nonce(nonce) or not _valid_id(file_id):
        return None
    try:
        path = _directory(data_dir) / (nonce + ".json")
        value = _read(path)
        now = time.time() if now is None else now
        if (not isinstance(value, dict) or set(value) != {"id", "hint", "exp", "dir"}
                or type(value["id"]) is not str or value["id"] != file_id
                or type(value["dir"]) is not str
                or type(value["hint"]) is not str or type(value["exp"]) not in (int, float)
                or not math.isfinite(value["exp"]) or not now < value["exp"] <= now + 600):
            return None
        if value["hint"]:
            validate_public_filename(value["hint"])
        path.unlink()  # Exactly one consumer can win, including across processes.
        return {"hint": value["hint"], "dir": _clean_directory(value["dir"])}
    except (OSError, ValueError, TypeError, OutputPathError):
        return None


def fetch_url(link, nonce) -> str:
    return link.page_url + "#" + MARKER + "=" + nonce
