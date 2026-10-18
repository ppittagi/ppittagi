#!/usr/bin/env python3
"""Download a YouTube (or any yt-dlp supported) video into a work directory.

Produces in <workdir>:
  source.mp4        video (H.264/AAC when available, <= --max-height)
  source.info.json  metadata (title, uploader, duration, license ...)
  source.*.vtt      existing subtitles, if any (manual first, then auto)
  source.wav        16 kHz mono audio for transcription

Usage:
  python fetch.py URL --workdir work/ [--start 00:01:00 --end 00:02:30] [--max-height 720]
  python fetch.py --local my_clip.mp4 --workdir work/      # skip download, use a local file
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def run(cmd):
    print("+", " ".join(str(c) for c in cmd), file=sys.stderr)
    subprocess.run([str(c) for c in cmd], check=True)


def to_seconds(t):
    if t is None:
        return None
    parts = [float(p) for p in str(t).split(":")]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def download(url, work, args):
    if not shutil.which("yt-dlp"):
        sys.exit("yt-dlp not found: pip install -U yt-dlp")
    fmt = (f"bv*[height<={args.max_height}][ext=mp4]+ba[ext=m4a]/"
           f"b[height<={args.max_height}][ext=mp4]/bv*[height<={args.max_height}]+ba/b")
    cmd = ["yt-dlp", "-f", fmt, "--merge-output-format", "mp4",
           "-o", str(work / "source.%(ext)s"), "--write-info-json", "--no-playlist",
           "--write-subs", "--write-auto-subs", "--sub-langs", args.sub_langs,
           "--sub-format", "vtt", "--ignore-errors"]
    if args.start or args.end:
        start = args.start or "0"
        end = args.end or "inf"
        cmd += ["--download-sections", f"*{start}-{end}", "--force-keyframes-at-cuts"]
    if args.cookies_from_browser:
        cmd += ["--cookies-from-browser", args.cookies_from_browser]
    cmd.append(url)
    run(cmd)
    if not (work / "source.mp4").exists():
        sys.exit("download failed: source.mp4 not created (see yt-dlp output above)")


def use_local(path, work, args):
    src = Path(path)
    if not src.exists():
        sys.exit(f"not found: {src}")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if args.start:
        cmd += ["-ss", str(to_seconds(args.start))]
    if args.end:
        cmd += ["-to", str(to_seconds(args.end))]
    cmd += ["-i", src, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k", work / "source.mp4"]
    run(cmd)
    (work / "source.info.json").write_text(json.dumps(
        {"title": src.stem, "uploader": "local file", "webpage_url": str(src.resolve())},
        ensure_ascii=False, indent=2), encoding="utf-8")


def probe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", help="YouTube URL")
    ap.add_argument("--local", help="use a local video file instead of downloading")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--start", help="clip start (SS, MM:SS or HH:MM:SS)")
    ap.add_argument("--end", help="clip end")
    ap.add_argument("--max-height", type=int, default=720)
    ap.add_argument("--sub-langs", default="ko.*,en.*", help="subtitle languages for yt-dlp")
    ap.add_argument("--cookies-from-browser", help="e.g. chrome, firefox (age-gated / sign-in videos)")
    args = ap.parse_args()
    if not args.url and not args.local:
        ap.error("give a URL or --local FILE")
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found: install ffmpeg first")

    work = Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)
    if args.local:
        use_local(args.local, work, args)
    else:
        download(args.url, work, args)

    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", work / "source.mp4",
         "-vn", "-ac", "1", "-ar", "16000", work / "source.wav"])

    info = json.loads((work / "source.info.json").read_text(encoding="utf-8"))
    summary = {
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "url": info.get("webpage_url"),
        "license": info.get("license"),
        "duration": round(probe_duration(work / "source.mp4"), 2),
        "subtitles": sorted(p.name for p in work.glob("source*.vtt")),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
