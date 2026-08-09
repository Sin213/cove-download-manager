"""Page-based video extraction handled by yt-dlp."""

import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse


_YOUTUBE_PATH = re.compile(r"^/(?:shorts|live|embed)/[^/]+")
# Reddit keeps DASH audio in a separate file from video, so a direct media
# link downloads silent. yt-dlp resolves the post and muxes the two.
_REDDIT_HOSTS = frozenset({
    "reddit.com", "old.reddit.com", "new.reddit.com",
    "sh.reddit.com", "np.reddit.com",
})
_REDDIT_POST = re.compile(r"^/r/[^/]+/comments/[^/]+")
_PROGRESS = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
_SPEED = re.compile(r"\bat\s+(\d+(?:\.\d+)?)\s*([KMG]iB)/s", re.IGNORECASE)
FINAL_PATH_MARKER = "__COVE_FINAL_FILE__:"


def is_youtube_url(url: str) -> bool:
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


def is_reddit_post_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        return host in _REDDIT_HOSTS and bool(_REDDIT_POST.match(parsed.path))
    except (TypeError, ValueError):
        return False


def is_extractor_url(url: str) -> bool:
    return is_youtube_url(url) or is_reddit_post_url(url)


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
        "--no-overwrites",
        "--no-post-overwrites",
        "--progress",
        "--print",
        f"after_move:{FINAL_PATH_MARKER}%(filepath)s",
        "-f",
        "bv*[height<=1080]+ba/b[height<=1080]/b",
    ]
    # A browser's full cookie jar for a YouTube page is large enough that
    # YouTube answers yt-dlp's webpage/API requests with HTTP 413. Public
    # videos need no cookies at all, so drop the header for the pages we
    # extract rather than trusting each browser call site to omit it.
    #
    # YouTube only. Reddit has the opposite problem: it refuses anonymous
    # callers outright, so yt-dlp reports "Account authentication is required"
    # and downloads nothing. Testing every extractor URL here swept Reddit into
    # a workaround written for one site's quirk.
    if cookies and not is_youtube_url(url):
        cmd += ["--add-header", f"Cookie: {cookies}"]
    if referrer:
        cmd += ["--referer", referrer]
    if user_agent:
        cmd += ["--user-agent", user_agent]
    cmd += ["-o", output_template, url]
    return cmd


def parse_ytdlp_final_path(line: str) -> str | None:
    if not line.startswith(FINAL_PATH_MARKER):
        return None
    value = line[len(FINAL_PATH_MARKER) :].strip()
    return value or None


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
