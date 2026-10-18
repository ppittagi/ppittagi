#!/usr/bin/env python3
"""Check that everything the parody pipeline needs is installed and reachable."""
import importlib.util
import shutil
import subprocess
import urllib.request


def ok(flag, name, hint=""):
    print(f"[{'OK' if flag else '--'}] {name}" + ("" if flag else f"   -> {hint}"))
    return flag


def reachable(url):
    try:
        urllib.request.urlopen(url, timeout=8)
        return True
    except urllib.error.HTTPError:
        return True          # the host answered
    except Exception:
        return False


def main():
    ffmpeg = shutil.which("ffmpeg")
    ok(ffmpeg, "ffmpeg", "brew install ffmpeg / sudo apt install ffmpeg")
    if ffmpeg:
        filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        ok(" ass " in filters, "ffmpeg libass (burned-in subtitles)", "install an ffmpeg build with --enable-libass")
    ok(shutil.which("ffprobe"), "ffprobe", "comes with ffmpeg")
    ok(shutil.which("yt-dlp"), "yt-dlp", "pip install -U yt-dlp")
    ok(shutil.which("edge-tts"), "edge-tts (dub voices)", "pip install edge-tts")
    ok(importlib.util.find_spec("faster_whisper"), "faster-whisper (transcription)", "pip install faster-whisper")
    ok(importlib.util.find_spec("yaml"), "PyYAML", "pip install pyyaml")
    if shutil.which("fc-list"):
        fonts = subprocess.run(["fc-list", ":lang=ko", "family"], capture_output=True, text=True).stdout
        ok(bool(fonts.strip()), "Korean font", "install Noto Sans CJK KR or NanumGothic")
        if fonts.strip():
            print("     ", ", ".join(sorted({f.split(",")[0] for f in fonts.splitlines()})[:6]))
    ok(reachable("https://www.youtube.com"), "network: youtube.com", "use fetch.py --local with a downloaded file")
    ok(reachable("https://speech.platform.bing.com"), "network: edge-tts service",
       "use tts: gtts, per-line audio: recordings, or mode: subtitles")


if __name__ == "__main__":
    main()
