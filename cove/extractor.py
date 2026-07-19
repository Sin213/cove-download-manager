"""Page-based video extraction handled by yt-dlp."""

import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse


_YOUTUBE_PATH = re.compile(r"^/(?:shorts|live|embed)/[^/]+")
_PROGRESS = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
_SPEED = re.compile(r"\bat\s+(\d+(?:\.\d+)?)\s*([KMG]iB)/s", re.IGNORECASE)


def is_extractor_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if host == "youtu.be":
            return bool(parsed.path.strip("/"))
        if host not in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
            return False
        if parsed.path == "/watch":
            return bool(parse_qs(parsed.query).get("v"))
        return bool(_YOUTUBE_PATH.match(parsed.path))
    except (TypeError, ValueError):
        return False


def resolve_ytdlp() -> str | None:
    name = "yt-dlp.exe" if os.name == "nt" else "yt-dlp"
    candidates: list[Path] = []
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        candidates.append(Path(bundle_dir) / name)
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates.extend((executable_dir / name, executable_dir / "_internal" / name))
    appdir = os.environ.get("APPDIR")
    if appdir:
        candidates.append(Path(appdir) / "usr" / "bin" / name)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("yt-dlp")


def ytdlp_command(
    url: str,
    output_template: str,
    executable: str | None = None,
    *,
    cookies: str = "",
    referrer: str = "",
    user_agent: str = "",
) -> list[str]:
    cmd = [
        executable or resolve_ytdlp() or "yt-dlp",
        "--newline",
        "--no-playlist",
        "--merge-output-format",
        "mp4",
        "-f",
        "bv*[height<=1080]+ba/b[height<=1080]/b",
    ]
    if cookies:
        cmd += ["--add-header", f"Cookie: {cookies}"]
    if referrer:
        cmd += ["--referer", referrer]
    if user_agent:
        cmd += ["--user-agent", user_agent]
    cmd += ["-o", output_template, url]
    return cmd


def parse_ytdlp_progress(line: str) -> dict[str, float]:
    match = _PROGRESS.search(line)
    if not match:
        return {}
    result = {"percent": min(100.0, float(match.group(1)))}
    speed = _SPEED.search(line)
    if speed:
        scale = {"kib": 1024, "mib": 1024**2, "gib": 1024**3}
        result["speed_bps"] = float(speed.group(1)) * scale[speed.group(2).lower()]
    return result
