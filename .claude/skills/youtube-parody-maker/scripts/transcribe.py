#!/usr/bin/env python3
"""Turn the source audio (or existing subtitles) into timestamped segments.

Writes <workdir>/transcript.json:
  [{"id": 1, "start": 0.0, "end": 2.4, "text": "..."}, ...]

Usage:
  python transcribe.py --workdir work/                 # prefer subtitles, else faster-whisper
  python transcribe.py --workdir work/ --whisper       # force speech recognition
  python transcribe.py --workdir work/ --model small --language ko
"""
import argparse
import json
import re
import sys
from pathlib import Path

TS = re.compile(r"(\d+):(\d{2}):(\d{2})[.,](\d{3})|(\d{2}):(\d{2})[.,](\d{3})")


def ts_to_sec(s):
    m = TS.search(s)
    if not m:
        raise ValueError(s)
    if m.group(1):
        h, mi, se, ms = m.group(1, 2, 3, 4)
    else:
        h, (mi, se, ms) = 0, m.group(5, 6, 7)
    return int(h) * 3600 + int(mi) * 60 + int(se) + int(ms) / 1000


def parse_vtt(path):
    segs, cur = [], None
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if "-->" in line:
            a, b = line.split("-->")
            cur = {"start": ts_to_sec(a), "end": ts_to_sec(b.split()[0]), "lines": []}
            segs.append(cur)
        elif line and cur is not None and not line.isdigit():
            text = re.sub(r"<[^>]+>", "", line).strip()
            if text and (not cur["lines"] or cur["lines"][-1] != text):
                cur["lines"].append(text)
        elif not line:
            cur = None
    out = []
    for s in segs:
        text = " ".join(s["lines"]).strip()
        if not text:
            continue
        # YouTube auto-subs repeat the previous line as a rolling caption; drop exact repeats
        if out and (text == out[-1]["text"] or out[-1]["text"].endswith(text)):
            out[-1]["end"] = max(out[-1]["end"], s["end"])
            continue
        if out and text.startswith(out[-1]["text"]):
            text = text[len(out[-1]["text"]):].strip()
            if not text:
                continue
        out.append({"start": round(s["start"], 2), "end": round(s["end"], 2), "text": text})
    return out


def whisper(work, model_name, language):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("faster-whisper not installed: pip install faster-whisper")
    model = WhisperModel(model_name, device="auto", compute_type="auto")
    segments, info = model.transcribe(str(work / "source.wav"), language=language,
                                      vad_filter=True, beam_size=5)
    print(f"detected language: {info.language} ({info.language_probability:.2f})", file=sys.stderr)
    return [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
            for s in segments if s.text.strip()]


def pick_subtitle(work, language):
    subs = sorted(work.glob("source*.vtt"))
    if language:
        subs.sort(key=lambda p: (f".{language}" not in p.name, p.name))
    return subs[0] if subs else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--whisper", action="store_true", help="ignore subtitles, run speech recognition")
    ap.add_argument("--model", default="small", help="faster-whisper model: tiny/base/small/medium/large-v3")
    ap.add_argument("--language", help="e.g. ko, en (auto-detect if omitted)")
    args = ap.parse_args()
    work = Path(args.workdir)

    sub = None if args.whisper else pick_subtitle(work, args.language)
    if sub:
        print(f"using subtitles: {sub.name}", file=sys.stderr)
        segs = parse_vtt(sub)
    else:
        if not (work / "source.wav").exists():
            sys.exit("source.wav missing: run fetch.py first")
        segs = whisper(work, args.model, args.language)
    for i, s in enumerate(segs, 1):
        s["id"] = i
    segs = [{"id": s["id"], "start": s["start"], "end": s["end"], "text": s["text"]} for s in segs]
    (work / "transcript.json").write_text(json.dumps(segs, ensure_ascii=False, indent=1), encoding="utf-8")
    for s in segs:
        print(f'{s["id"]:>3} [{s["start"]:7.2f}-{s["end"]:7.2f}] {s["text"]}')
    print(f"\n{len(segs)} segments -> {work / 'transcript.json'}", file=sys.stderr)


if __name__ == "__main__":
    main()
