from cove.hls import is_hls_url, parse_ffmpeg_progress, ffmpeg_command


class TestIsHlsUrl:
    def test_m3u8_extension(self):
        assert is_hls_url("https://example.com/stream/master.m3u8") is True

    def test_m3u8_with_query_params(self):
        assert is_hls_url("https://cdn.example.com/live/index.m3u8?token=abc123&exp=999") is True

    def test_m3u8_case_insensitive(self):
        assert is_hls_url("https://example.com/video.M3U8") is True

    def test_regular_url(self):
        assert is_hls_url("https://example.com/file.zip") is False

    def test_mp4_url(self):
        assert is_hls_url("https://example.com/video.mp4") is False

    def test_empty_url(self):
        assert is_hls_url("") is False

    def test_m3u8_in_path_not_extension(self):
        assert is_hls_url("https://example.com/m3u8/other.txt") is False


class TestParseFfmpegProgress:
    def test_time_and_speed(self):
        line = "frame=  120 fps=30 size=   1024kB time=00:01:23.45 bitrate= 500kbits/s speed=2.10x"
        result = parse_ffmpeg_progress(line)
        assert result["time_secs"] == 83.45
        assert result["speed"] == "2.10x"

    def test_duration_line(self):
        line = "  Duration: 00:05:40.00, start: 0.000000, bitrate: 3000 kb/s"
        result = parse_ffmpeg_progress(line)
        assert result["duration_secs"] == 340.0

    def test_no_match(self):
        result = parse_ffmpeg_progress("some random ffmpeg output")
        assert result == {}

    def test_time_zero(self):
        line = "frame=    0 fps=0.0 size=       0kB time=00:00:00.00 speed=N/A"
        result = parse_ffmpeg_progress(line)
        assert result["time_secs"] == 0.0
        assert result["speed"] == "N/A"

    def test_percentage_calculation(self):
        line = "time=00:01:00.00 speed=1.50x"
        result = parse_ffmpeg_progress(line, duration_secs=120.0)
        assert result["pct"] == 50


class TestFfmpegCommand:
    def test_basic_command(self):
        cmd = ffmpeg_command("https://example.com/stream.m3u8", "/tmp/out.mp4")
        assert cmd == [
            "ffmpeg", "-y", "-i", "https://example.com/stream.m3u8",
            "-c", "copy", "/tmp/out.mp4",
        ]

    def test_browser_headers_are_input_options(self):
        cmd = ffmpeg_command(
            "https://example.com/stream.m3u8",
            "/tmp/out.mp4",
            cookies="session=abc",
            referrer="https://example.com/page",
            user_agent="TestUA/1.0",
        )
        headers_idx = cmd.index("-headers")
        assert cmd[headers_idx + 1] == (
            "Cookie: session=abc\r\nReferer: https://example.com/page\r\n"
        )
        ua_idx = cmd.index("-user_agent")
        assert cmd[ua_idx + 1] == "TestUA/1.0"
        # Input options must come before -i to apply to the HLS input.
        assert headers_idx < cmd.index("-i")
        assert ua_idx < cmd.index("-i")

    def test_no_header_flags_without_headers(self):
        cmd = ffmpeg_command("https://example.com/stream.m3u8", "/tmp/out.mp4")
        assert "-headers" not in cmd
        assert "-user_agent" not in cmd
