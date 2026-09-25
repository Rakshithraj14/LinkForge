"""Run: python test_app.py"""

from fastapi import HTTPException

from app import MODES, check_rate, check_url, quality_args, reason


def test_modes():
    for m in ("both", "video", "audio", "subtitles", "auto_subtitles", "thumbnail", "info"):
        assert m in MODES
    assert "-x" in MODES["audio"]  # audio must extract, not just grab a stream
    assert "--skip-download" in MODES["subtitles"]  # must not also pull the video


def test_check_url_rejects_bad():
    for bad in ("file:///etc/passwd", "-evil-flag", "http://127.0.0.1/x",
                "http://169.254.169.254/latest/meta-data/", "ftp://x.com/f"):
        try:
            check_url(bad)
            raise AssertionError(f"should have rejected: {bad}")
        except HTTPException as e:
            assert e.status_code == 400


def test_check_url_accepts_good():
    check_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")  # must not raise


def test_rate_limit():
    import app as m
    m.RATE_LIMIT = 3
    m._hits.clear()
    for _ in range(3):
        check_rate("1.2.3.4")
    try:
        check_rate("1.2.3.4")
        raise AssertionError("4th request should have been rate limited")
    except HTTPException as e:
        assert e.status_code == 429
    check_rate("5.6.7.8")  # different IP, unaffected


def test_quality_args():
    assert quality_args("both", "720", "webm") == ["-S", "res:720,ext:webm", "--merge-output-format", "webm/mkv"]
    assert quality_args("audio", "best", "flac") == ["--audio-format", "flac", "--audio-quality", "0"]
    assert quality_args("audio", "192", "mp3")[-1] == "192K"
    assert quality_args("video", "1152", "mp4")[1] == "res:1152,ext:mp4"
    assert quality_args("subtitles", "x", "y") == []
    for mode, q, f in (("both", "1080; rm", "mp4"), ("video", "720", "avi"), ("audio", "best", "wav"), ("audio", "999", "mp3"), ("video", "99999", "mp4")):
        try:
            quality_args(mode, q, f)
            raise AssertionError(f"should have rejected: {mode} {q} {f}")
        except HTTPException as e:
            assert e.status_code == 400


def test_cookies_copy_is_used_and_removed():
    import os, tempfile
    import app as m
    fd, src = tempfile.mkstemp(); os.write(fd, b"# Netscape HTTP Cookie File\n"); os.close(fd)
    os.chmod(src, 0o444)  # read-only, like a mounted secret
    m.COOKIES_FILE = src
    seen = {}
    real_run = m.subprocess.run
    def fake_run(cmd, **kw):
        seen["path"] = cmd[cmd.index("--cookies") + 1]
        seen["tail"] = cmd[-2:]
        assert os.path.exists(seen["path"]) and seen["path"] != src
        return real_run(["true"], **kw)
    m.subprocess.run = fake_run
    try:
        m.run_ytdlp(["yt-dlp"], "https://x.com/a", 5)
    finally:
        m.subprocess.run = real_run
        m.COOKIES_FILE = ""
        os.chmod(src, 0o644); os.unlink(src)
    assert seen["tail"] == ["--", "https://x.com/a"]  # url stays last, after --
    assert not os.path.exists(seen["path"])  # copy cleaned up


def test_reason_messages():
    assert "cookies" in reason("ERROR: Sign in to confirm you're not a bot")
    assert "bigger" in reason("ERROR: File is larger than max-filesize")
    assert "downloadable" in reason("ERROR: Unsupported URL")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
