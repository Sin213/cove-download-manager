"""HLS/M3U8 stream support - URL detection, ffmpeg command, progress parsing."""

from __future__ import annotations

import re
from urllib.parse import urlparse

_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+)\.(\d+)")
_SPEED_RE = re.compile(r"speed=\s*(\S+)")
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)")


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


def ffmpeg_command(
    url: str,
    output_path: str,
    *,
    cookies: str = "",
    referrer: str = "",
    user_agent: str = "",
) -> list[str]:
    # No explicit "-bsf:a aac_adtstoasc": the MP4 muxer auto-inserts it for
    # ADTS AAC input, and forcing it fails HLS streams with non-AAC audio.
    cmd = ["ffmpeg", "-n"]
    header_lines = []
    if cookies:
        header_lines.append(f"Cookie: {cookies}")
    if referrer:
        header_lines.append(f"Referer: {referrer}")
    if header_lines:
        # Input options must precede -i; the hls demuxer forwards these
        # http options to every segment request.
        cmd += ["-headers", "\r\n".join(header_lines) + "\r\n"]
    if user_agent:
        cmd += ["-user_agent", user_agent]
    cmd += ["-i", url, "-c", "copy", output_path]
    return cmd
