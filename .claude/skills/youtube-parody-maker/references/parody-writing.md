# Writing the parody

The renderer can only be as funny as the spec. Spend the effort here.

## 1. Find the angle (pick one, state it to the user in one line)

| Angle | What it does | Works well for |
|---|---|---|
| **Mundane swap** | Epic/serious visuals, painfully ordinary dialogue (회의, 야근, 월급날, 엑셀, 배달) | trailers, speeches, sports, nature docs |
| **Workplace mirror** | The scene becomes the user's job — for a clinical-trial team: CRF 쿼리, 모니터링 방문, DB lock 전날, IRB 서류, SAE 보고 | anything with confrontation or panic |
| **Over-dramatisation** | Trivial footage narrated like a blockbuster: freeze + `dramatic`, slow-mo, "그때는 몰랐다…" | vlogs, cooking, pets, office footage |
| **Literal narration** | The voice describes exactly what is on screen, deadpan | music videos, ads |
| **Fake subtitles** | Original audio kept, subtitles say something totally different (works best with a foreign language the audience doesn't speak) | heated arguments, interviews in another language |
| **Inner monologue** | What the person is *really* thinking, between their real lines | interviews, presentations, press conferences |

Parody should comment on or play with the original (its tone, its genre, its clichés). That is what
makes it a parody rather than just reuse — and it is usually the funnier choice anyway.

## 2. Structure

- **Setup → escalation → payoff.** First 1–2 lines establish the premise straight; middle lines
  escalate; the last line (before the outro card) is the punchline. Put a `freeze` or `sfx` right
  on the punchline, not before it.
- **Callbacks**: reuse a word from line 1 in the final line.
- **Rule of three**: two normal items, third absurd.
- **Silence is a beat.** Not every transcript segment needs a line; leaving one segment with the
  original audio (`tts: false`, or omit it) before the payoff makes the payoff land.
- Short clips win: 20–60 s with 5–12 lines is the sweet spot.

## 3. Timing math

- Slot length = `end - start` of the transcript segment.
- Korean TTS ≈ 6–7 syllables/s at `rate: "+0%"`; English ≈ 2.5–3 words/s.
- Aim for 80–90 % of the slot so the voice finishes before the next mouth movement.
- A punchline can deliberately run long over a `freeze` — anchor the line `start` at the freeze
  `at` time; the freeze adds output time, so the line has `duration` extra seconds of room.
- In `subtitles` mode subtitles may be a bit longer than speech but keep them ≤ ~2 lines
  (≈ 28 Korean characters per line at 720p).

## 4. Voices as characters

| Character | Settings |
|---|---|
| Narrator / trailer voice | `ko-KR-InJoonNeural`, `rate: "-10%"`, `pitch: "-15Hz"` |
| Panicking junior | `ko-KR-SunHiNeural`, `rate: "+25%"`, `pitch: "+30Hz"` |
| Calm robot boss | `ko-KR-HyunsuNeural`, `rate: "-5%"` |
| Chipmunk | any voice, `pitch: "+80Hz"`, `rate: "+30%"` |
| English announcer | `en-US-GuyNeural` / `en-US-AriaNeural` |

List more voices with `edge-tts --list-voices | grep ko-KR`.

## 5. Effect placement cheatsheet

| Moment | Effect |
|---|---|
| Realisation / plot twist | `freeze` (1–2 s, `zoom: 1.2`) + `caption` + `sfx: dramatic` |
| Failure | `sfx: sad_trombone` right after the line, maybe `filter: noir` for the whole clip |
| Success / reveal | `sfx: tada` or `ding` |
| Something absurd said | `zoom` on the speaker's face for 1–1.5 s (`x`,`y` = face centre as 0–1 fractions) |
| Rage / panic | `shake` 0.5–1.5 s, `sfx: boing` |
| Swear word / censored joke | `bleep` over the word |
| Build-up | `slowmo` 0.5× + `caption: "슬로우 모션으로 다시 보기"` + `sfx: drumroll` |
| "Meanwhile…" / time skip | `caption` top + `sfx: whoosh` |

Don't stack more than ~one effect per 5 seconds — over-editing kills the timing.

## 6. Content limits (non-negotiable)

- No line should read as a real person genuinely saying something damaging (crimes, slurs,
  medical/financial claims, political statements they did not make). Mock situations, not people's
  identities.
- No content sexualising real people, no targeting private individuals or minors.
- Keep the `PARODY` label and an outro card naming the original title/channel.
- Keep company/clinical data out of jokes: no real subject IDs, site names or study codes.
