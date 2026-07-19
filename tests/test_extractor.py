from cove.extractor import is_extractor_url, parse_ytdlp_progress, ytdlp_command


def test_recognizes_supported_youtube_pages():
    assert is_extractor_url("https://www.youtube.com/watch?v=BMcJirSZACw")
    assert is_extractor_url("https://youtu.be/BMcJirSZACw")
    assert is_extractor_url("https://www.youtube.com/shorts/BMcJirSZACw")
    assert not is_extractor_url("https://www.youtube.com/")
    assert not is_extractor_url("https://example.com/watch?v=BMcJirSZACw")


def test_command_uses_mp4_output_template():
    command = ytdlp_command(
        "https://youtu.be/id", "/tmp/Title.%(ext)s", executable="yt-dlp"
    )
    assert command[0] == "yt-dlp"
    assert "--cookies-from-browser" not in command
    assert "--merge-output-format" in command
    assert "/tmp/Title.%(ext)s" in command


def test_command_forwards_browser_headers():
    command = ytdlp_command(
        "https://youtu.be/id",
        "/tmp/Title.%(ext)s",
        executable="yt-dlp",
        cookies="session=abc",
        referrer="https://example.com/page",
        user_agent="TestUA/1.0",
    )
    assert command[command.index("--add-header") + 1] == "Cookie: session=abc"
    assert command[command.index("--referer") + 1] == "https://example.com/page"
    assert command[command.index("--user-agent") + 1] == "TestUA/1.0"
    assert command[-1] == "https://youtu.be/id"


def test_command_omits_header_flags_when_absent():
    command = ytdlp_command(
        "https://youtu.be/id", "/tmp/Title.%(ext)s", executable="yt-dlp"
    )
    assert "--add-header" not in command
    assert "--referer" not in command
    assert "--user-agent" not in command


def test_parses_download_progress():
    result = parse_ytdlp_progress("[download]  42.5% of 10MiB at 2.00MiB/s ETA 00:03")
    assert result == {"percent": 42.5, "speed_bps": 2 * 1024**2}
