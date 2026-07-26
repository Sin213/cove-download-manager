"""Torrent input parsing: magnet URIs and `.torrent` metadata.

Both are untrusted input. A magnet carries tracker passkeys that must never
leave the machine, and a `.torrent` is a binary file from the internet whose
contents choose filenames on disk. Everything here is therefore bounded
(source size, nesting depth, collection sizes) and every path component is
validated before it can reach the filesystem.

Two rules drive the design:

1. The info hash is SHA-1 over the *original* byte span of the bencoded
   `info` dictionary. Decoding and re-encoding is not equivalent: key order
   and integer spelling are not normalised by real-world clients, so a
   round-trip can produce a different hash for a perfectly valid torrent.
2. Nothing here raises an exception that quotes the input. A magnet
   contains secrets and a `.torrent` is raw binary; every rejection is one
   of this module's own fixed sentences.

The module is deliberately Qt-free, network-free and dependency-free so the
queue can call it from a background worker and the tests can exercise it
without an event loop.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl

# A `.torrent` big enough to matter is a `.torrent` that is attacking us:
# real ones are kilobytes. The cap is applied to the file on disk before it
# is read, and again to the bytes handed to the parser.
MAX_TORRENT_BYTES = 10 * 1024 * 1024
# Long enough for a magnet with a full tracker list, short enough that a
# pathological URI can't be used to burn parsing time.
MAX_MAGNET_LENGTH = 8192

_MAX_DEPTH = 32
_MAX_ITEMS = 100_000
_MAX_FILES = 20_000
_MAX_PATH_PARTS = 64
_MAX_FILE_BYTES = 1 << 50
_MAX_TOTAL_BYTES = 1 << 53
_MAX_TEXT_BYTES = 1024
_MAX_INT_DIGITS = 20
_MAX_LENGTH_DIGITS = 12


class TorrentError(ValueError):
    """A rejected magnet or `.torrent`, carrying a sentence safe to show."""


_BAD_MAGNET = "This magnet link could not be read."
_BAD_TORRENT = "This .torrent file could not be read."
_TOO_LARGE = "This .torrent file is larger than Cove will read (10 MiB)."
_MAGNET_TOO_LONG = "This magnet link is too long for Cove to read."
_NOT_MAGNET = "This is not a magnet link."
_NO_BTIH = "This magnet link has no BitTorrent v1 info hash."
_CONTRADICTORY = "This magnet link contains more than one info hash."
_V2_ONLY = "BitTorrent v2-only torrents are not supported yet."
_UNSAFE_PATH = "This torrent contains a file path Cove will not write to."
_MANAGED_MISSING = "Cove's stored copy of this .torrent is missing."
_MANAGED_CHANGED = (
    "Cove's stored copy of this .torrent no longer matches this torrent."
)
_MANAGED_UNSAFE = "Cove will not write its .torrent copy to that path."


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TorrentFile:
    """One file inside a torrent. `path` is already validated as safe."""

    index: int
    path: tuple[str, ...]
    size: int

    @property
    def relative_path(self) -> str:
        return "/".join(self.path)

    @property
    def name(self) -> str:
        return self.path[-1]


@dataclass(frozen=True)
class MagnetInfo:
    info_hash: str
    display_name: str = ""
    original_uri: str = ""


@dataclass(frozen=True)
class TorrentMetadata:
    info_hash: str
    name: str
    files: tuple[TorrentFile, ...]
    total_size: int
    multi_file: bool
    raw_bytes: bytes = field(repr=False, default=b"")
    # The original byte span of the `info` dictionary, kept verbatim.
    info_bytes: bytes = field(repr=False, default=b"")

    def info_only_document(self) -> bytes:
        """A `.torrent` carrying nothing but this torrent's `info` value.

        Everything a passkey can hide in -- `announce`, `announce-list`,
        `url-list`, `comment`, `created by` -- lives *outside* `info`, so
        this is what gets uploaded to a debrid provider rather than the
        user's original file. The info span is reused byte for byte, so
        the info hash of this document is identical to the original's;
        re-encoding it would not be.

        A torrent with no announce list is an ordinary trackerless torrent,
        which is exactly what a cache probe needs.
        """
        return b"d4:info" + self.info_bytes + b"e"

    def destination_parts(self, file: TorrentFile) -> tuple[str, ...]:
        """Path components for `file`, relative to the chosen output dir.

        A multi-file torrent is rooted under its own name, matching what
        every other client does; a single-file torrent writes the file
        straight into the output directory.
        """
        return (self.name,) + file.path if self.multi_file else file.path


# ---------------------------------------------------------------------------
# Info hashes and magnets
# ---------------------------------------------------------------------------

_HEX_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_B32_RE = re.compile(r"\A[A-Z2-7]{32}\Z")


def normalize_info_hash(value) -> str:
    """Return a lowercase 40-char hex v1 info hash, or raise.

    Accepts the two spellings a BTIH magnet may use: 40 hex characters or
    32 base32 characters, in either case.
    """
    if not isinstance(value, str):
        raise TorrentError(_BAD_MAGNET)
    text = value.strip()
    lowered = text.lower()
    if _HEX_RE.match(lowered):
        return lowered
    upper = text.upper()
    if _B32_RE.match(upper):
        try:
            raw = base64.b32decode(upper)
        except Exception:
            raise TorrentError(_BAD_MAGNET) from None
        if len(raw) != 20:
            raise TorrentError(_BAD_MAGNET)
        return raw.hex()
    raise TorrentError(_BAD_MAGNET)


def is_magnet(value) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith("magnet:?")


def parse_magnet(uri) -> MagnetInfo:
    """Parse a BitTorrent v1 (or hybrid) BTIH magnet link.

    Tracker, web-seed and peer parameters are read past and discarded: the
    only thing that leaves this function is the info hash and a display
    name, so a passkey in `tr=` cannot be forwarded anywhere.
    """
    if not isinstance(uri, str):
        raise TorrentError(_NOT_MAGNET)
    text = uri.strip()
    if len(text) > MAX_MAGNET_LENGTH:
        raise TorrentError(_MAGNET_TOO_LONG)
    if not text.lower().startswith("magnet:?"):
        raise TorrentError(_NOT_MAGNET)

    hashes: set[str] = set()
    saw_v2 = False
    display = ""
    for key, value in parse_qsl(text[len("magnet:?"):], keep_blank_values=True):
        name = key.strip().lower()
        # Multiple topics are spelled xt, xt.1, xt.2 ... in the wild.
        if name == "xt" or name.startswith("xt."):
            topic = value.strip()
            lowered = topic.lower()
            if lowered.startswith("urn:btih:"):
                hashes.add(normalize_info_hash(topic[len("urn:btih:"):]))
            elif lowered.startswith("urn:btmh:"):
                # A v2 multihash. Reinterpreting its SHA-256 digest as a
                # BTIH would silently address a different torrent.
                saw_v2 = True
        elif name == "dn" and not display:
            display = _clean_text(value)

    if len(hashes) > 1:
        raise TorrentError(_CONTRADICTORY)
    if not hashes:
        raise TorrentError(_V2_ONLY if saw_v2 else _NO_BTIH)
    return MagnetInfo(
        info_hash=next(iter(hashes)), display_name=display, original_uri=text
    )


def minimal_magnet(info_hash: str) -> str:
    """The only magnet form Cove ever sends to a debrid provider.

    Trackers, web seeds, peer sources and the display name are all dropped:
    the provider needs the info hash and nothing else, and the user's
    original magnet may carry a private-tracker passkey.
    """
    return f"magnet:?xt=urn:btih:{normalize_info_hash(info_hash)}"


# ---------------------------------------------------------------------------
# Bencode
# ---------------------------------------------------------------------------


def _decode_int(data: bytes, pos: int) -> tuple[int, int]:
    end = data.find(b"e", pos + 1)
    if end < 0:
        raise TorrentError(_BAD_TORRENT)
    raw = data[pos + 1:end]
    if not raw or len(raw) > _MAX_INT_DIGITS:
        raise TorrentError(_BAD_TORRENT)
    negative = raw[:1] == b"-"
    body = raw[1:] if negative else raw
    if not body.isdigit():
        raise TorrentError(_BAD_TORRENT)
    # "i-0e" and any leading zero are not valid bencode.
    if negative and body[:1] == b"0":
        raise TorrentError(_BAD_TORRENT)
    if len(body) > 1 and body[:1] == b"0":
        raise TorrentError(_BAD_TORRENT)
    return int(raw), end + 1


def _decode_bytes(data: bytes, pos: int) -> tuple[bytes, int]:
    colon = data.find(b":", pos)
    if colon < 0:
        raise TorrentError(_BAD_TORRENT)
    raw = data[pos:colon]
    if not raw.isdigit() or len(raw) > _MAX_LENGTH_DIGITS:
        raise TorrentError(_BAD_TORRENT)
    if len(raw) > 1 and raw[:1] == b"0":
        raise TorrentError(_BAD_TORRENT)
    end = colon + 1 + int(raw)
    if end > len(data):
        raise TorrentError(_BAD_TORRENT)
    return data[colon + 1:end], end


def _decode_list(data: bytes, pos: int, depth: int) -> tuple[list, int]:
    pos += 1
    out: list = []
    while True:
        if pos >= len(data):
            raise TorrentError(_BAD_TORRENT)
        if data[pos:pos + 1] == b"e":
            return out, pos + 1
        if len(out) >= _MAX_ITEMS:
            raise TorrentError(_BAD_TORRENT)
        value, pos = _decode_value(data, pos, depth + 1)
        out.append(value)


def _decode_dict(
    data: bytes, pos: int, depth: int, capture: bool = False
) -> tuple[dict, int, dict]:
    pos += 1
    out: dict = {}
    spans: dict = {}
    while True:
        if pos >= len(data):
            raise TorrentError(_BAD_TORRENT)
        if data[pos:pos + 1] == b"e":
            return out, pos + 1, spans
        if len(out) >= _MAX_ITEMS:
            raise TorrentError(_BAD_TORRENT)
        if not data[pos:pos + 1].isdigit():
            # Keys must be byte strings; anything else is malformed.
            raise TorrentError(_BAD_TORRENT)
        key, pos = _decode_bytes(data, pos)
        if key in out:
            # Which duplicate wins is client-specific, so the document is
            # ambiguous. Refuse it rather than pick.
            raise TorrentError(_BAD_TORRENT)
        start = pos
        value, pos = _decode_value(data, pos, depth + 1)
        out[key] = value
        if capture:
            spans[key] = (start, pos)


def _decode_value(data: bytes, pos: int, depth: int):
    if depth > _MAX_DEPTH:
        raise TorrentError(_BAD_TORRENT)
    if pos >= len(data):
        raise TorrentError(_BAD_TORRENT)
    head = data[pos:pos + 1]
    if head == b"i":
        return _decode_int(data, pos)
    if head == b"l":
        return _decode_list(data, pos, depth)
    if head == b"d":
        value, end, _ = _decode_dict(data, pos, depth)
        return value, end
    if head.isdigit():
        return _decode_bytes(data, pos)
    raise TorrentError(_BAD_TORRENT)


def bdecode_root(data: bytes) -> tuple[dict, tuple[int, int] | None]:
    """Decode a complete bencoded dictionary.

    Returns the decoded root plus the (start, end) byte range of its `info`
    value. The span is the whole point: hashing a re-encoded dictionary
    would produce the wrong info hash whenever the original spelling isn't
    canonical, which is common in the wild.
    """
    if isinstance(data, bytearray):
        data = bytes(data)
    if not isinstance(data, bytes) or data[:1] != b"d":
        raise TorrentError(_BAD_TORRENT)
    root, pos, spans = _decode_dict(data, 0, 1, capture=True)
    if pos != len(data):
        # Trailing garbage after a complete document.
        raise TorrentError(_BAD_TORRENT)
    return root, spans.get(b"info")


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

# Kept in step with cove.debrid._safe_filename and api_server.validate_filename.
_RESERVED_CHARS = '/\\:<>"|?*'
_WINDOWS_RESERVED_NAMES = frozenset(
    ("CON", "PRN", "AUX", "NUL")
    + tuple(f"COM{i}" for i in range(1, 10))
    + tuple(f"LPT{i}" for i in range(1, 10))
)


def _clean_text(value) -> str:
    """Decode a bencoded byte string to a printable str, bounded."""
    if isinstance(value, str):
        raw = value.encode("utf-8", "replace")
    elif isinstance(value, bytes):
        raw = value
    else:
        raise TorrentError(_BAD_TORRENT)
    if len(raw) > _MAX_TEXT_BYTES:
        raise TorrentError(_BAD_TORRENT)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Plenty of real torrents predate the UTF-8 convention. latin-1
        # always succeeds and cannot introduce a separator or a NUL that
        # wasn't already in the bytes, and safe_component still vets it.
        text = raw.decode("latin-1")
    return "".join(c for c in text if ord(c) >= 32 and ord(c) != 127).strip()


def safe_component(value) -> str:
    """Validate one path component, or raise.

    This is the only gate between torrent metadata and the filesystem, so
    it rejects rather than repairs: a component that has to be rewritten to
    become safe is a component we do not understand.
    """
    if isinstance(value, bytes):
        if b"\x00" in value:
            raise TorrentError(_UNSAFE_PATH)
    elif isinstance(value, str):
        if "\x00" in value:
            raise TorrentError(_UNSAFE_PATH)
    else:
        raise TorrentError(_UNSAFE_PATH)
    try:
        text = _clean_text(value)
    except TorrentError:
        raise TorrentError(_UNSAFE_PATH) from None
    if "/" in text or "\\" in text:
        # Separators inside a single component: absolute paths, UNC paths
        # and smuggled parents all land here.
        raise TorrentError(_UNSAFE_PATH)
    # Windows silently drops trailing spaces and periods, which would turn
    # "..." into ".." after the fact.
    text = text.rstrip(" .")
    if not text or text in (".", ".."):
        raise TorrentError(_UNSAFE_PATH)
    if any(char in text for char in _RESERVED_CHARS):
        # Also catches the "C:" drive prefix.
        raise TorrentError(_UNSAFE_PATH)
    if text.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise TorrentError(_UNSAFE_PATH)
    if len(text.encode("utf-8")) > 255:
        raise TorrentError(_UNSAFE_PATH)
    return text


def safe_relative_parts(parts) -> tuple[str, ...]:
    """Validate a whole relative path, component by component."""
    if not isinstance(parts, (list, tuple)) or not parts:
        raise TorrentError(_UNSAFE_PATH)
    if len(parts) > _MAX_PATH_PARTS:
        raise TorrentError(_UNSAFE_PATH)
    return tuple(safe_component(p) for p in parts)


# ---------------------------------------------------------------------------
# .torrent metadata
# ---------------------------------------------------------------------------


def _safe_size(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TorrentError(_BAD_TORRENT)
    if value < 0 or value > _MAX_FILE_BYTES:
        raise TorrentError(_BAD_TORRENT)
    return value


def parse_torrent(data) -> TorrentMetadata:
    """Parse `.torrent` bytes into validated metadata."""
    if isinstance(data, bytearray):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise TorrentError(_BAD_TORRENT)
    if len(data) > MAX_TORRENT_BYTES:
        raise TorrentError(_TOO_LARGE)

    root, info_span = bdecode_root(data)
    info = root.get(b"info")
    if info_span is None or not isinstance(info, dict):
        raise TorrentError(_BAD_TORRENT)

    pieces = info.get(b"pieces")
    if not isinstance(pieces, bytes) or not pieces or len(pieces) % 20:
        # No usable v1 piece table. If the torrent declares v2 structure,
        # say so specifically instead of calling the file corrupt.
        if b"meta version" in info or b"file tree" in info:
            raise TorrentError(_V2_ONLY)
        raise TorrentError(_BAD_TORRENT)

    info_hash = hashlib.sha1(data[info_span[0]:info_span[1]]).hexdigest()
    name = safe_component(info.get(b"name"))

    files: list[TorrentFile] = []
    entries = info.get(b"files")
    multi_file = entries is not None
    if multi_file:
        if not isinstance(entries, list) or not entries:
            raise TorrentError(_BAD_TORRENT)
        if len(entries) > _MAX_FILES:
            raise TorrentError(_BAD_TORRENT)
        seen: set[tuple[str, ...]] = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise TorrentError(_BAD_TORRENT)
            parts = safe_relative_parts(entry.get(b"path"))
            if parts in seen:
                # Two files writing to one path is ambiguous, not merely
                # wasteful; refuse rather than let one clobber the other.
                raise TorrentError(_UNSAFE_PATH)
            seen.add(parts)
            files.append(TorrentFile(index, parts, _safe_size(entry.get(b"length"))))
    else:
        files.append(TorrentFile(0, (name,), _safe_size(info.get(b"length"))))

    total = 0
    for f in files:
        total += f.size
        if total > _MAX_TOTAL_BYTES:
            raise TorrentError(_BAD_TORRENT)

    return TorrentMetadata(
        info_hash=info_hash,
        name=name,
        files=tuple(files),
        total_size=total,
        multi_file=multi_file,
        raw_bytes=data,
        info_bytes=data[info_span[0]:info_span[1]],
    )


# ---------------------------------------------------------------------------
# Cove's managed .torrent copies
# ---------------------------------------------------------------------------
#
# A local torrent has to survive a restart, a pause and a retry, none of
# which can depend on the file the user picked still being where it was.
# Cove therefore keeps its own copy, named after the info hash, inside its
# data directory: the name makes the identity checkable, and the identity is
# re-checked on every read so a replaced or corrupted copy can never quietly
# start a different torrent.


def managed_torrent_dir():
    """DATA_DIR/torrents, resolved at call time.

    Imported lazily and read off the module so a test (or a portable
    install) can redirect cove.config.DATA_DIR.
    """
    from . import config

    return os.path.join(str(config.DATA_DIR), "torrents")


def managed_torrent_path(info_hash: str) -> str:
    return os.path.join(
        managed_torrent_dir(), f"{normalize_info_hash(info_hash)}.torrent"
    )


def is_managed_torrent_path(path) -> bool:
    """True when `path` is a file Cove itself placed in its torrent store."""
    if not isinstance(path, str) or not path:
        return False
    base = os.path.realpath(managed_torrent_dir())
    parent = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    return parent == base


def store_managed_torrent(meta: TorrentMetadata) -> str:
    """Copy validated `.torrent` bytes into Cove's store; return the path.

    An existing copy that still hashes to the same info hash is reused
    untouched. Anything else under that name is not a different torrent —
    the name *is* the hash — so it is replaced, but only after refusing to
    follow a symlink: the replacement is written to a temp file in the same
    directory and renamed over the target, so an attacker-planted link can
    never turn this into a write somewhere else.
    """
    directory = managed_torrent_dir()
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError:
        raise TorrentError(_MANAGED_UNSAFE) from None
    target = managed_torrent_path(meta.info_hash)

    if os.path.islink(target):
        raise TorrentError(_MANAGED_UNSAFE)
    if os.path.exists(target):
        try:
            if read_managed_torrent(target, meta.info_hash) == meta.raw_bytes:
                return target
        except TorrentError:
            pass  # Junk under our own name; replaced below.

    tmp = target + ".tmp"
    try:
        if os.path.islink(tmp):
            raise TorrentError(_MANAGED_UNSAFE)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, meta.raw_bytes)
        finally:
            os.close(fd)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, target)
    except TorrentError:
        raise
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise TorrentError(_MANAGED_UNSAFE) from None
    return target


def read_managed_torrent(path, info_hash: str) -> bytes:
    """The stored bytes for `info_hash`, or raise.

    Missing, replaced and corrupted copies are all distinguishable failures
    for the caller, and none of them quote the file's contents.
    """
    if not isinstance(path, str) or not path:
        raise TorrentError(_MANAGED_MISSING)
    if os.path.islink(path):
        raise TorrentError(_MANAGED_UNSAFE)
    if not os.path.isfile(path):
        raise TorrentError(_MANAGED_MISSING)
    try:
        meta = read_torrent_file(path)
    except TorrentError:
        raise TorrentError(_MANAGED_CHANGED) from None
    if meta.info_hash != normalize_info_hash(info_hash):
        raise TorrentError(_MANAGED_CHANGED)
    return meta.raw_bytes


def discard_managed_torrent(path) -> None:
    """Delete a copy Cove owns. Anything else is left alone."""
    if not is_managed_torrent_path(path):
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def read_torrent_file(path) -> TorrentMetadata:
    """Read and parse a local `.torrent`, checking its size before reading.

    Never call this from the GUI thread: it does blocking file IO and a
    SHA-1 over the info dictionary.
    """
    try:
        if not os.path.isfile(path):
            raise TorrentError("That is not a .torrent file.")
        size = os.path.getsize(path)
    except OSError:
        raise TorrentError("That .torrent file could not be opened.") from None
    if size > MAX_TORRENT_BYTES:
        raise TorrentError(_TOO_LARGE)
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_TORRENT_BYTES + 1)
    except OSError:
        raise TorrentError("That .torrent file could not be opened.") from None
    return parse_torrent(data)
