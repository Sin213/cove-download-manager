"""HLS/M3U8 stream support - URL detection, ffmpeg command, progress parsing."""

from __future__ import annotations

import re
from ipaddress import IPv6Address
from urllib.parse import urlparse, urlsplit

_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+)\.(\d+)")
_SPEED_RE = re.compile(r"speed=\s*(\S+)")
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)")
_AUTH_FAILURE_RE = re.compile(
    r"\b(?:HTTP error|Server returned)\s+\b40[13]\b|\b(?:unauthorized|forbidden|access denied)\b",
    re.IGNORECASE,
)
HLS_BROWSER_LOGIN_FAILURE = (
    "This stream may require a browser login. Cove does not send browser logins "
    "to streaming (HLS) servers, so login-only streams cannot be downloaded yet."
)


def is_hls_url(url: str) -> bool:
    if not url:
        return False
    path = urlparse(url).path
    return path.lower().endswith(".m3u8")


def _hms_to_secs(h: str, m: str, s: str, cs: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(cs.ljust(2, "0")[:2]) / 100


def parse_ffmpeg_progress(line: str, duration_secs: float = 0.0) -> dict:
    result: dict = {}

    dur = _DURATION_RE.search(line)
    if dur:
        result["duration_secs"] = _hms_to_secs(*dur.groups())
        return result

    tm = _TIME_RE.search(line)
    if tm:
        secs = _hms_to_secs(*tm.groups())
        result["time_secs"] = secs
        sp = _SPEED_RE.search(line)
        result["speed"] = sp.group(1) if sp else ""
        if duration_secs > 0:
            result["pct"] = min(100, int(secs * 100 / duration_secs))
        return result

    return result


def browser_login_failure_hint(exit_code: int, stderr: str) -> str:
    if exit_code != 0 and _AUTH_FAILURE_RE.search(stderr):
        return HLS_BROWSER_LOGIN_FAILURE
    return ""


def referrer_origin(referrer: str) -> str:
    if not isinstance(referrer, str) or any(ord(char) < 0x20 or ord(char) == 0x7f for char in referrer):
        return ""
    try:
        page = urlsplit(referrer)
        if not page.netloc.isascii():
            return ""
        if page.scheme.lower() not in {"http", "https"} or not page.hostname:
            return ""
        if any(char == "\\" or char.isspace() or ord(char) < 0x20 or ord(char) == 0x7f for char in page.netloc):
            return ""
        host = page.hostname
        if not host.isascii() or "%" in host:
            return ""
        authority = page.netloc.rsplit("@", 1)[-1]
        if ":" in host:
            if not authority.startswith("["):
                return ""
            closing = authority.find("]")
            if closing == -1 or authority[1:closing].lower() != host:
                return ""
            IPv6Address(host)
            suffix = authority[closing + 1:]
            if suffix and not suffix.startswith(":"):
                return ""
            explicit_port, port_text = bool(suffix), suffix[1:]
        else:
            raw_host, separator, port_text = authority.partition(":")
            if raw_host.lower() != host or len(host) > 253:
                return ""
            labels = host.split(".")
            if any(
                not 1 <= len(label) <= 63
                or re.fullmatch(r"[A-Za-z0-9_]([A-Za-z0-9_-]*[A-Za-z0-9_])?", label) is None
                for label in labels
            ):
                return ""
            explicit_port = bool(separator)
        if explicit_port and re.fullmatch(r"[0-9]{1,5}", port_text) is None:
            return ""
        port = page.port  # Validate even when an explicit port is malformed.
    except ValueError:
        return ""
    if ":" in host:
        host = f"[{host}]"
    port_suffix = f":{port}" if port is not None else ""
    return f"{page.scheme.lower()}://{host}{port_suffix}/"


def ffmpeg_command(
    url: str,
    output_path: str,
    *,
    referrer: str = "",
    user_agent: str = "",
) -> list[str]:
    # No explicit "-bsf:a aac_adtstoasc": the MP4 muxer auto-inserts it for
    # ADTS AAC input, and forcing it fails HLS streams with non-AAC audio.
    cmd = ["ffmpeg", "-n"]
    origin = referrer_origin(referrer)
    if origin:
        # Input options must precede -i; the hls demuxer forwards these
        # http options to every segment request.
        cmd += ["-headers", f"Referer: {origin}\r\n"]
    if user_agent:
        cmd += ["-user_agent", user_agent]
    cmd += ["-i", url, "-c", "copy", output_path]
    return cmd
