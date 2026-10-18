---
name: youtube-parody-maker
description: >
  Turn a YouTube video link (or a local video file) into a parody video: download the clip with
  yt-dlp, transcribe it (existing subtitles or faster-whisper), write a funny parody script on the
  original timestamps, then render a new MP4 with ffmpeg — TTS re-dub (edge-tts) or fake-subtitle
  parody, freeze-frames with captions, slow-motion, zoom, shake, bleeps, built-in sound effects,
  colour filters, title/credit cards and a PARODY label. Use this skill whenever the user pastes a
  YouTube/Shorts URL and wants a parody, meme, 더빙/자막 패러디, 밈 영상, "웃기게 바꿔줘", re-dub,
  fake subtitles, or asks to download a YouTube clip and edit it into something comedic — even if
  they just say "이 영상 패러디로 만들어줘".
---

# YouTube parody maker

**URL → `fetch.py` → `transcribe.py` → (you write `parody.yaml`) → `render.py` → parody.mp4**

The scripts do the mechanical work; *your* job is the comedy: reading the transcript, finding the
joke, and writing a spec whose lines fit the original timing. Answer the user in their language
(Korean here); keep YAML keys in English.

## 0. Setup (once per machine)

```bash
pip install -U yt-dlp edge-tts faster-whisper pyyaml     # gTTS optional
# ffmpeg with libass + a Korean font:
#   macOS:  brew install ffmpeg && brew install --cask font-noto-sans-cjk-kr
#   Ubuntu: sudo apt install ffmpeg fonts-noto-cjk
python scripts/check_env.py      # verifies tools, fonts, network to YouTube / TTS
```

Scripts live in this skill's `scripts/` folder; run them with that path. Use one work directory
per video (e.g. `parody_work/<video-id>/`); everything intermediate stays there.

## 1. Before downloading — rights check (always)

Ask/confirm briefly, don't lecture:
- Is the user allowed to use this video? (own channel, CC licence, or parody/commentary use of a
  *short* excerpt). Parody that comments on or mocks the original is the strongest fair-use /
  공정이용 case; re-uploading most of someone else's video with jokes on top is not.
- Prefer a **short excerpt** (`--start/--end`, ideally under ~60 s) over the whole video.
- `fetch.py` prints `license` from the metadata — mention it if it is Creative Commons.

Refuse or steer away from: making a real person appear to say things in a way viewers could take as
genuine (no voice cloning of real people — the built-in TTS voices are generic, keep it that way),
defamatory claims, sexual content involving real people, harassment of private individuals, or
anything aimed at dodging Content ID. Always keep the visible `label` (default `PARODY`) and an
outro card crediting the original.

## 2. Fetch

```bash
python scripts/fetch.py "https://www.youtube.com/watch?v=XXXX" --workdir W --start 0:30 --end 1:20
python scripts/fetch.py --local clip.mp4 --workdir W          # user supplied a file instead
```
Outputs `W/source.mp4`, `source.wav`, `source.info.json`, any `source.*.vtt` subtitles, and a JSON
summary (title, uploader, duration, licence). Age-gated / sign-in videos:
`--cookies-from-browser chrome`. If YouTube refuses (HTTP 403, "Sign in to confirm"), run
`pip install -U yt-dlp` first — it is almost always an outdated yt-dlp — then retry. In a sandbox
with no access to youtube.com, ask the user to download the clip themselves and use `--local`.

## 3. Transcribe

```bash
python scripts/transcribe.py --workdir W                     # uses subtitles if present, else whisper
python scripts/transcribe.py --workdir W --whisper --language ko --model small
```
Writes `W/transcript.json` (`id`, `start`, `end`, `text`) and prints it. Read it fully. Model
choice: `small` is a good default on CPU; `medium`/`large-v3` for noisy audio if a GPU exists.
For music-only or silent clips there is nothing to transcribe — write lines on scene timings you
pick by looking at frames (`ffmpeg -ss T -i W/source.mp4 -frames:v 1 W/peek_T.png`, then view them).

## 4. Write the parody spec — the creative step

Read `references/parody-writing.md` before writing. Then copy `assets/example_parody.yaml` to
`W/parody.yaml` and fill it in. Key decisions:

| Choice | Options |
|---|---|
| `mode` | `dub` (new voice via TTS, original ducked) · `subtitles` (keep original audio, fake subtitles — the classic "Downfall"-style meme, often funnier and safest for timing) |
| `tts` / `voice` | `edge` + `ko-KR-InJoonNeural` (male), `ko-KR-SunHiNeural` (female), `ko-KR-HyunsuNeural`; `en-US-GuyNeural` … · `gtts` · `none`. Per-line `voice`, `rate: "+25%"`, `pitch: "+40Hz"` give characters. Per-line `audio: take.wav` uses the user's own recording. |
| `original_audio` | `duck` (default in dub) · `keep` (default in subtitles) · `mute` |
| `filter` | `none vintage noir vhs deepfried warm cold` (or any raw ffmpeg filter string) |
| effects | `freeze` (+`caption`, `zoom`), `slowmo`/`speed` (+`caption`), `zoom`, `shake`, `caption`, `sfx` (`beep boing ding whoosh drumroll sad_trombone dramatic tada` or a file path), `bleep`, `mute` |
| cards | `intro_card`, `outro_card` (`text`, `duration`, `background`) — outro must credit the original |

Rules that keep the render clean:
- Every time is a **source time** from `transcript.json` (seconds or `MM:SS`). `render.py` shifts
  everything after freezes, speed changes and the intro card automatically.
- A line with only `id:` takes that transcript segment's `start`/`end`.
- Korean TTS speaks roughly 6–7 syllables per second. Keep each dub line within
  `(end - start) × 6` syllables; longer lines are sped up (max `max_tempo`, default 1.5) and the
  renderer prints a warning — shorten those lines instead of accepting the speed-up.
- `freeze`/`slowmo`/`speed` ranges must not overlap each other.

## 5. Render, review, iterate

```bash
python scripts/render.py --workdir W --spec W/parody.yaml --preview   # ~seconds, 480p draft
python scripts/render.py --workdir W --spec W/parody.yaml             # final → W/parody.mp4
```
After the preview, check it yourself before showing the user: grab frames at the punchlines
(`ffmpeg -ss T -i W/parody_preview.mp4 -frames:v 1 W/check_T.png`) and view them — subtitles
readable and not covering faces/captions, freeze captions present, label visible. Read any
"sped up" warnings and fix those lines. Then give the user the preview, ask what to punch up, edit
the YAML and re-render. TTS clips are cached by text in `W/tts/`, so re-renders are fast.

Deliver: the final MP4 path, a 3–5 line summary of the joke structure, and a reminder to keep the
PARODY label and credit when uploading.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Korean shows as boxes | install Noto Sans CJK KR / NanumGothic, or `--fontsdir /path/to/fonts` and set `subtitle.font` to that family name |
| `TTS produced no audio` / edge-tts DNS error | no network to `speech.platform.bing.com`; use `tts: gtts`, per-line `audio:` recordings, or `mode: subtitles` |
| yt-dlp 403 / "Sign in to confirm you're not a bot" | `pip install -U yt-dlp`; then `--cookies-from-browser chrome`; else `--local` |
| `overlapping/invalid timing effect` | freeze/slowmo/speed ranges overlap or `end <= start` |
| Subtitles collide with burned-in original captions | `subtitle: {margin_v: 140}` or move jokes to `caption` effects at the top |
| Render slow | use `--preview` while iterating; source is capped at 720p by `fetch.py --max-height` |
