#!/usr/bin/env python3
"""Render a parody video from source.mp4 + a parody spec (YAML).

Pipeline:
  1. timeline   source is cut into pieces (normal / speed-changed / freeze-frame / title cards)
                and concatenated into base.mp4; a time map converts source time -> output time
  2. voices     each parody line is synthesised (edge-tts / gTTS / your own recording / none)
                and fitted into its slot (speeds up to `max_tempo` when too long)
  3. sfx        built-in sound effects are synthesised with ffmpeg (no downloads)
  4. final      colour filter, zoom, shake, burned-in ASS subtitles/captions/label,
                original audio kept / ducked / muted, voices + sfx + optional BGM mixed

All times in the spec are SOURCE times (seconds or MM:SS), i.e. the times you see in
transcript.json -- render.py moves them for you when freezes/speed changes/cards are inserted.

Usage:
  python render.py --workdir work/ --spec work/parody.yaml            # full quality
  python render.py --workdir work/ --spec work/parody.yaml --preview  # fast 480p draft
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML missing: pip install pyyaml")

FILTERS = {
    "none": None,
    "vintage": "curves=preset=vintage,vignette=PI/5",
    "noir": "hue=s=0,eq=contrast=1.35:brightness=-0.03,vignette=PI/4",
    "vhs": "eq=saturation=1.4:contrast=1.1,gblur=sigma=0.7,noise=alls=18:allf=t",
    "deepfried": "eq=saturation=3:contrast=1.8:gamma=1.2,unsharp=7:7:2.5,noise=alls=25:allf=t",
    "warm": "colortemperature=temperature=4500,eq=saturation=1.2",
    "cold": "colortemperature=temperature=9000,eq=saturation=0.9",
}

# name -> (ffmpeg lavfi source, duration). Synthesised, so there is nothing to license.
SFX = {
    "beep": ("sine=frequency=1000:sample_rate=48000", 0.5),
    "boing": ("aevalsrc='0.6*sin(2*PI*(180+260*sin(2*PI*7*t))*t)*exp(-3.5*t)':s=48000", 0.8),
    "ding": ("aevalsrc='0.5*(sin(2*PI*1568*t)+0.5*sin(2*PI*3136*t))*exp(-4*t)':s=48000", 1.2),
    "whoosh": ("anoisesrc=color=pink:amplitude=0.6:sample_rate=48000,highpass=f=400,lowpass=f=3000,"
               "afade=t=in:d=0.25,afade=t=out:st=0.25:d=0.35", 0.6),
    "drumroll": ("anoisesrc=color=brown:amplitude=0.8:sample_rate=48000,"
                 "volume='0.4+0.6*abs(sin(2*PI*14*t))':eval=frame,lowpass=f=900", 1.6),
    "sad_trombone": ("aevalsrc='0.45*sin(2*PI*if(lt(t,0.45),311,if(lt(t,0.9),294,if(lt(t,1.35),277,"
                     "262+6*sin(2*PI*6*t))))*t)*if(lt(t,1.35),1,exp(-1.2*(t-1.35)))':s=48000", 2.6),
    "dramatic": ("aevalsrc='0.5*(sin(2*PI*98*t)+sin(2*PI*147*t)+0.5*sin(2*PI*196*t))/2*exp(-0.9*t)':s=48000", 2.5),
    "tada": ("aevalsrc='0.4*(sin(2*PI*523*t)+sin(2*PI*659*t)+sin(2*PI*784*t))/2*if(lt(t,0.12),1,exp(-1.5*(t-0.12)))':s=48000", 1.5),
}

DEFAULTS = {
    "mode": "dub",              # dub: TTS voices over the video | subtitles: keep audio, fake subtitles
    "tts": "edge",              # edge | gtts | none
    "voice": "ko-KR-InJoonNeural",
    "rate": "+0%",
    "pitch": "+0Hz",
    "original_audio": "duck",   # keep | duck | mute
    "duck_volume": 0.15,
    "voice_volume": 1.4,
    "max_tempo": 1.5,
    "filter": "none",
    "label": "PARODY",
    "subtitle": {},
    "effects": [],
    "lines": [],
}

SUB_DEFAULT = {"font": "Noto Sans KR", "size": 0, "color": "#FFFFFF", "outline": "#000000",
               "outline_width": 3, "bold": True, "margin_v": 0}


# ---------------------------------------------------------------- helpers
def run(cmd, quiet=True):
    cmd = [str(c) for c in cmd]
    if not quiet:
        print("+", " ".join(cmd), file=sys.stderr)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[-4000:])
        raise SystemExit(f"command failed: {' '.join(cmd[:6])} ...")
    return r.stdout


def probe(path):
    out = run(["ffprobe", "-v", "error", "-show_entries",
               "stream=codec_type,width,height,r_frame_rate:format=duration",
               "-of", "json", path])
    j = json.loads(out)
    v = next(s for s in j["streams"] if s["codec_type"] == "video")
    num, den = v["r_frame_rate"].split("/")
    fps = round(float(num) / float(den), 3) or 30
    has_audio = any(s["codec_type"] == "audio" for s in j["streams"])
    return {"w": v["width"], "h": v["height"], "fps": min(fps, 60),
            "duration": float(j["format"]["duration"]), "has_audio": has_audio}


def duration(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                      "-of", "default=nw=1:nk=1", path]).strip())


def sec(v):
    if v is None or isinstance(v, (int, float)):
        return v
    total = 0.0
    for p in str(v).split(":"):
        total = total * 60 + float(p)
    return total


def ass_time(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_color(hex_color, alpha=0):
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_text(s):
    return str(s).replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def atempo_chain(f):
    parts = []
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.4f}")
    return ",".join(parts)


def esc_filter_path(p):
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


# ---------------------------------------------------------------- timeline
class Timeline:
    """Ordered pieces of the output video and the source->output time map."""

    def __init__(self, spec, src_dur):
        timing = sorted((e for e in spec["effects"] if e["type"] in ("freeze", "slowmo", "speed")),
                        key=lambda e: sec(e.get("at", e.get("start"))))
        self.pieces, t, out = [], 0.0, 0.0
        intro = spec.get("intro_card")
        if intro:
            self._add({"kind": "card", "d": float(intro.get("duration", 2.5)), "card": intro})
        for e in timing:
            if e["type"] == "freeze":
                at = min(sec(e["at"]), src_dur - 0.05)
                if at > t:
                    self._add({"kind": "src", "s": t, "e": at, "speed": 1.0})
                self._add({"kind": "freeze", "at": at, "d": float(e.get("duration", 1.5)), "fx": e})
                t = max(t, at)
            else:
                s, en = sec(e["start"]), min(sec(e["end"]), src_dur)
                if s < t or en <= s:
                    raise SystemExit(f"overlapping/invalid timing effect: {e}")
                if s > t:
                    self._add({"kind": "src", "s": t, "e": s, "speed": 1.0})
                speed = float(e.get("factor", 0.5 if e["type"] == "slowmo" else 2.0))
                self._add({"kind": "src", "s": s, "e": en, "speed": speed, "fx": e})
                t = en
        if t < src_dur:
            self._add({"kind": "src", "s": t, "e": src_dur, "speed": 1.0})
        outro = spec.get("outro_card")
        if outro:
            self._add({"kind": "card", "d": float(outro.get("duration", 3)), "card": outro})
        self.total = sum(p["out_d"] for p in self.pieces)

    def _add(self, p):
        p["out_d"] = (p["e"] - p["s"]) / p["speed"] if p["kind"] == "src" else p["d"]
        p["out_s"] = sum(q["out_d"] for q in self.pieces)
        self.pieces.append(p)

    def map(self, t):
        """source time -> output time (freezes push later content back).

        A time exactly at a freeze maps to the START of the freeze, so an sfx/caption/line
        anchored at the freeze point plays over the frozen frame."""
        t = sec(t)
        for p in self.pieces:
            if p["kind"] == "src" and p["s"] <= t <= p["e"]:
                return p["out_s"] + (t - p["s"]) / p["speed"]
        srcs = [p for p in self.pieces if p["kind"] == "src"]
        if not srcs:
            return 0.0
        return srcs[0]["out_s"] if t < srcs[0]["s"] else srcs[-1]["out_s"] + srcs[-1]["out_d"]


def render_pieces(tl, source, info, work, enc):
    W, H, fps = info["w"], info["h"], info["fps"]
    vcommon = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p"
    out_args = ["-c:v", "libx264", "-preset", enc["preset"], "-crf", enc["crf"],
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    files = []
    for i, p in enumerate(tl.pieces):
        f = work / f"piece_{i:03d}.mp4"
        silence = ["-f", "lavfi", "-t", f"{p['out_d']:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        if p["kind"] == "src":
            vf = vcommon if p["speed"] == 1 else f"setpts=PTS/{p['speed']},{vcommon}"
            cmd = ["ffmpeg", "-y", "-ss", f"{p['s']:.3f}", "-to", f"{p['e']:.3f}", "-i", source]
            if info["has_audio"]:
                af = "aresample=48000" if p["speed"] == 1 else f"{atempo_chain(p['speed'])},aresample=48000"
                cmd += ["-vf", vf, "-af", af]
            else:
                cmd += silence + ["-vf", vf, "-map", "0:v", "-map", "1:a", "-shortest"]
        elif p["kind"] == "freeze":
            png = work / f"freeze_{i:03d}.png"
            run(["ffmpeg", "-y", "-ss", f"{p['at']:.3f}", "-i", source, "-frames:v", "1", png])
            zoom = float(p["fx"].get("zoom", 1.15))
            vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
                  f"scale=iw*{zoom}:ih*{zoom},crop={W}:{H},setsar=1,fps={fps},format=yuv420p")
            cmd = ["ffmpeg", "-y", "-loop", "1", "-framerate", fps, "-t", f"{p['d']:.3f}", "-i", png] + silence + \
                  ["-vf", vf, "-map", "0:v", "-map", "1:a", "-shortest"]
        else:
            color = p["card"].get("background", "black").lstrip("#")
            color = f"0x{color}" if len(color) == 6 and all(c in "0123456789abcdefABCDEF" for c in color) else color
            cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={W}x{H}:r={fps}:d={p['d']:.3f}"] + \
                  silence + ["-vf", "format=yuv420p", "-map", "0:v", "-map", "1:a", "-shortest"]
        run(cmd + out_args + [f])
        files.append(f)
    lst = work / "pieces.txt"
    lst.write_text("".join(f"file '{f.name}'\n" for f in files), encoding="utf-8")
    base = work / "base.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", base])
    for f in files:
        f.unlink()
    return base


# ---------------------------------------------------------------- voices
def synth(line, spec, work):
    engine = line.get("tts", spec["tts"])
    if line.get("audio"):
        return Path(line["audio"]).expanduser()
    if engine in (False, "none", None):
        return None
    voice = line.get("voice", spec["voice"])
    rate, pitch = line.get("rate", spec["rate"]), line.get("pitch", spec["pitch"])
    text = str(line["text"])
    key = hashlib.sha1(f"{engine}|{voice}|{rate}|{pitch}|{text}".encode()).hexdigest()[:16]
    tts_dir = work / "tts"
    tts_dir.mkdir(exist_ok=True)
    out = tts_dir / f"{key}.mp3"
    if out.exists() and out.stat().st_size > 0:
        return out
    if engine == "edge":
        if not shutil.which("edge-tts"):
            raise SystemExit("edge-tts missing: pip install edge-tts (or set tts: gtts / none)")
        run(["edge-tts", "--voice", voice, f"--rate={rate}", f"--pitch={pitch}",
             "--text", text, "--write-media", out])
    elif engine == "gtts":
        try:
            from gtts import gTTS
        except ImportError:
            raise SystemExit("gTTS missing: pip install gTTS")
        gTTS(text, lang=line.get("lang", spec.get("lang", voice.split("-")[0]))).save(str(out))
    else:
        raise SystemExit(f"unknown tts engine: {engine}")
    if not out.exists() or out.stat().st_size == 0:
        raise SystemExit(f"TTS produced no audio for line: {text!r} (network blocked?)")
    return out


def resolve_lines(spec, work, tl):
    transcript = {}
    tp = work / "transcript.json"
    if tp.exists():
        transcript = {s["id"]: s for s in json.loads(tp.read_text(encoding="utf-8"))}
    lines = []
    for ln in spec["lines"]:
        ln = dict(ln)
        if "id" in ln and ("start" not in ln or "end" not in ln):
            seg = transcript.get(ln["id"])
            if not seg:
                raise SystemExit(f"line id {ln['id']} not in transcript.json and no start/end given")
            ln.setdefault("start", seg["start"])
            ln.setdefault("end", seg["end"])
        if "start" not in ln:
            raise SystemExit(f"line needs start (or a transcript id): {ln}")
        ln["start"] = sec(ln["start"])
        ln["end"] = sec(ln.get("end"))
        lines.append(ln)
    lines.sort(key=lambda l: l["start"])
    out = []
    for i, ln in enumerate(lines):
        o_start = tl.map(ln["start"])
        nxt = tl.map(lines[i + 1]["start"]) if i + 1 < len(lines) else tl.total
        o_end = tl.map(ln["end"]) if ln["end"] is not None else min(o_start + 4, nxt)
        use_tts = spec["mode"] == "dub" and ln.get("tts", True) is not False
        clip = synth(ln, spec, work) if (use_tts or ln.get("audio")) else None
        tempo, voice_d = 1.0, 0.0
        if clip:
            d = duration(clip)
            slot = max(o_end - o_start, 0.3)
            room = max(slot, nxt - o_start - 0.05)
            if d > room:
                tempo = min(d / room, float(spec["max_tempo"]))
            voice_d = d / tempo
        out.append({**ln, "o_start": o_start, "o_end": max(o_end, o_start + voice_d),
                    "clip": clip, "tempo": tempo, "voice_d": voice_d})
    return out


# ---------------------------------------------------------------- subtitles
def write_ass(path, spec, info, tl, lines):
    W, H = info["w"], info["h"]
    st = {**SUB_DEFAULT, **(spec.get("subtitle") or {})}
    size = st["size"] or max(18, int(H * 0.058))
    mv = st["margin_v"] or int(H * 0.06)
    bold = -1 if st["bold"] else 0
    ol = st["outline_width"]
    font = st["font"]
    styles = [
        ("Sub", font, size, ass_color(st["color"]), ass_color(st["outline"]), ol, 2, mv),
        ("Caption", font, int(size * 1.1), ass_color("#FFE14D"), ass_color("#000000"), ol + 1, 8, int(H * 0.05)),
        ("Label", font, int(size * 0.5), ass_color("#FFFFFF", 0x40), ass_color("#000000", 0x40), 1, 9, int(H * 0.03)),
        ("Card", font, int(size * 1.5), ass_color("#FFFFFF"), ass_color("#000000"), 2, 5, 0),
    ]
    head = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
            "WrapStyle: 0", "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
            "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"]
    for name, fn, fs, pc, oc, olw, al, m in styles:
        head.append(f"Style: {name},{fn},{fs},{pc},{pc},{oc},&H80000000,{bold},0,0,0,100,100,0,0,1,"
                    f"{olw},1,{al},{int(W * 0.05)},{int(W * 0.05)},{m},1")
    head += ["", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    ev = []

    def add(style, s, e, text, layer=0, tags=""):
        ev.append(f"Dialogue: {layer},{ass_time(s)},{ass_time(e)},{style},,0,0,0,,{tags}{ass_text(text)}")

    for ln in lines:
        if ln.get("show_subtitle", True):
            add("Sub", ln["o_start"], ln["o_end"], ln["text"])
    for p in tl.pieces:
        if p["kind"] == "card":
            add("Card", p["out_s"], p["out_s"] + p["d"], p["card"].get("text", ""), 1, "{\\fad(300,300)}")
        if p["kind"] == "freeze" and p["fx"].get("caption"):
            add("Caption", p["out_s"], p["out_s"] + p["d"], p["fx"]["caption"], 2, "{\\fad(150,150)}")
        if p["kind"] == "src" and p.get("fx", {}).get("caption"):
            add("Caption", p["out_s"], p["out_s"] + p["out_d"], p["fx"]["caption"], 2)
    for e in spec["effects"]:
        if e["type"] == "caption":
            pos = {"top": 8, "center": 5, "bottom": 2}.get(e.get("position", "top"), 8)
            add("Caption", tl.map(e["start"]), tl.map(e["end"]), e["text"], 2, f"{{\\an{pos}\\fad(120,120)}}")
    if spec.get("label"):
        add("Label", 0, tl.total + 1, spec["label"], 3)
    path.write_text("\n".join(head + ev) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- final mix
def between_sum(ranges):
    return "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in ranges) or "0"


def build_final(base, ass, spec, info, tl, lines, work, out, enc, fontsdir):
    W, H = info["w"], info["h"]
    inputs = ["-i", base]
    fc = []
    # ---- video
    v = "[0:v]"
    chain = []
    flt = FILTERS.get(spec["filter"], spec["filter"])
    if flt:
        chain.append(flt)
    shakes = [(tl.map(e["start"]), tl.map(e["end"])) for e in spec["effects"] if e["type"] == "shake"]
    if shakes:
        cond = f"gt({between_sum(shakes)},0)"
        chain.append(f"crop=w=iw*0.92:h=ih*0.92:x='(iw-ow)/2+if({cond},(iw-ow)/2*sin(t*47),0)'"
                     f":y='(ih-oh)/2+if({cond},(ih-oh)/2*cos(t*59),0)',scale={W}:{H}")
    if chain:
        fc.append(f"{v}{','.join(chain)}[vf]")
        v = "[vf]"
    for i, e in enumerate(e for e in spec["effects"] if e["type"] == "zoom"):
        s = float(e.get("scale", 1.6))
        cx, cy = float(e.get("x", 0.5)), float(e.get("y", 0.4))
        a, b = tl.map(e["start"]), tl.map(e["end"])
        fc.append(f"{v}split[za{i}][zb{i}]")
        fc.append(f"[zb{i}]crop=w=iw/{s}:h=ih/{s}:x='max(0,min(iw-ow,iw*{cx}-ow/2))'"
                  f":y='max(0,min(ih-oh,ih*{cy}-oh/2))',scale={W}:{H},setsar=1[zc{i}]")
        fc.append(f"[za{i}][zc{i}]overlay=enable='between(t,{a:.3f},{b:.3f})'[zo{i}]")
        v = f"[zo{i}]"
    sub = f"ass='{esc_filter_path(ass)}'"
    if fontsdir:
        sub += f":fontsdir='{esc_filter_path(fontsdir)}'"
    fc.append(f"{v}{sub}[vout]")

    # ---- audio
    voiced = [(l["o_start"], l["o_start"] + l["voice_d"]) for l in lines if l["clip"]]
    mutes = [(tl.map(e["start"]), tl.map(e["end"])) for e in spec["effects"] if e["type"] in ("mute", "bleep")]
    mode = spec["original_audio"]
    if mode == "mute":
        vol = "0"
    else:
        duck = spec["duck_volume"] if mode == "duck" else 1
        vol = f"if(gt({between_sum(mutes)},0),0,if(gt({between_sum(voiced)},0),{duck},1))"
    fc.append(f"[0:a]volume='{vol}':eval=frame[orig]")
    mix = ["[orig]"]
    idx = 1
    for n, l in enumerate(lines):
        if not l["clip"]:
            continue
        inputs += ["-i", l["clip"]]
        ms = int(l["o_start"] * 1000)
        tempo = f"{atempo_chain(l['tempo'])}," if abs(l["tempo"] - 1) > 0.01 else ""
        fc.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,{tempo}"
                  f"volume={spec['voice_volume']},adelay={ms}|{ms}[vo{n}]")
        mix.append(f"[vo{n}]")
        idx += 1
    sfx_events = [e for e in spec["effects"] if e["type"] in ("sfx", "bleep")]
    for n, e in enumerate(sfx_events):
        name = e.get("sound", "beep")
        at = tl.map(e.get("at", e.get("start")))
        if name in SFX:
            src, d = SFX[name]
            if e["type"] == "bleep":
                d = tl.map(e["end"]) - at
            inputs += ["-f", "lavfi", "-t", f"{d:.3f}", "-i", src]
        else:
            inputs += ["-i", Path(name).expanduser()]
        ms = int(at * 1000)
        fc.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,"
                  f"volume={e.get('volume', 0.8)},adelay={ms}|{ms}[sx{n}]")
        mix.append(f"[sx{n}]")
        idx += 1
    bgm = spec.get("bgm")
    if bgm:
        inputs += ["-stream_loop", "-1", "-i", Path(bgm["file"]).expanduser()]
        fc.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,volume={bgm.get('volume', 0.2)},"
                  f"atrim=0:{tl.total:.3f},afade=t=out:st={max(0, tl.total - 2):.3f}:d=2[bgm]")
        mix.append("[bgm]")
        idx += 1
    fc.append(f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=first,alimiter=limit=0.95[aout]")

    script = work / "filter_complex.txt"
    script.write_text(";\n".join(fc), encoding="utf-8")
    run(["ffmpeg", "-y", *inputs, "-filter_complex_script", script, "-map", "[vout]", "-map", "[aout]",
         "-c:v", "libx264", "-preset", enc["preset"], "-crf", enc["crf"], "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-t", f"{tl.total:.3f}", out])


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--source", help="default: <workdir>/source.mp4")
    ap.add_argument("--out", help="default: spec.output or <workdir>/parody.mp4")
    ap.add_argument("--preview", action="store_true", help="fast low-res draft (480p)")
    ap.add_argument("--fontsdir", help="directory with .ttf/.otf fonts for subtitles")
    args = ap.parse_args()

    work = Path(args.workdir)
    raw = yaml.safe_load(Path(args.spec).read_text(encoding="utf-8")) or {}
    spec = {**DEFAULTS, **raw}
    if spec["mode"] == "subtitles" and "original_audio" not in raw:
        spec["original_audio"] = "keep"
    spec["effects"] = spec.get("effects") or []
    spec["lines"] = spec.get("lines") or []
    source = Path(args.source or work / "source.mp4")
    if not source.exists():
        raise SystemExit(f"{source} missing: run fetch.py first")
    out = Path(args.out or spec.get("output") or work / "parody.mp4")
    if not out.is_absolute() and not args.out and spec.get("output"):
        out = work / out

    info = probe(source)
    enc = {"preset": "medium", "crf": "20"}
    tmp = work / "_render"
    tmp.mkdir(exist_ok=True)
    if args.preview:
        enc = {"preset": "ultrafast", "crf": "28"}
        if info["h"] > 480:
            info["w"], info["h"] = int(info["w"] * 480 / info["h"]) // 2 * 2, 480
        out = out.with_name(out.stem + "_preview" + out.suffix)

    tl = Timeline(spec, info["duration"])
    print(f"[1/4] timeline: {len(tl.pieces)} pieces, {tl.total:.1f}s", file=sys.stderr)
    base = render_pieces(tl, source, info, tmp, enc)
    print(f"[2/4] voices: {len(spec['lines'])} lines ({spec['mode']}, tts={spec['tts']})", file=sys.stderr)
    lines = resolve_lines(spec, work, tl)
    for l in lines:
        if l["tempo"] > 1.01:
            print(f"   line @{l['start']:.1f}s sped up x{l['tempo']:.2f} to fit -- consider shortening: "
                  f"{l['text'][:40]}", file=sys.stderr)
    print("[3/4] subtitles", file=sys.stderr)
    ass = tmp / "subs.ass"
    write_ass(ass, spec, info, tl, lines)
    print("[4/4] final mix", file=sys.stderr)
    build_final(base, ass, spec, info, tl, lines, tmp, out, enc, args.fontsdir)
    print(json.dumps({"output": str(out), "duration": round(duration(out), 2),
                      "lines": len(lines), "effects": len(spec["effects"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
