# youtube-parody-maker (Claude skill)

유튜브 링크(또는 로컬 영상)를 받아 **패러디 영상**으로 바꿔주는 Claude Code 스킬입니다.

```
URL ─ fetch.py ─▶ source.mp4 ─ transcribe.py ─▶ transcript.json
                                   │
                    Claude가 패러디 대본 작성 (parody.yaml)
                                   │
                         render.py ─▶ parody.mp4
```

## 사용법
Claude Code에서 이 저장소를 열고 그냥 말하면 됩니다.

> 이 영상 0:30~1:20 구간을 우리 팀 DB Lock 전날 상황으로 더빙 패러디 만들어줘 https://youtu.be/XXXX

Claude가 다운로드 → 자막/음성인식 → 대본 작성 → 480p 미리보기 → 피드백 반영 → 최종 렌더까지 진행합니다.

## 설치
```bash
pip install -r .claude/skills/youtube-parody-maker/requirements.txt
# ffmpeg(libass 포함) + 한글 폰트
#   macOS : brew install ffmpeg && brew install --cask font-noto-sans-cjk-kr
#   Ubuntu: sudo apt install ffmpeg fonts-noto-cjk
python .claude/skills/youtube-parody-maker/scripts/check_env.py
```

## 지원 기능
- 모드: `dub`(TTS 재더빙, 원본 음성 덕킹) / `subtitles`(원본 음성 유지 + 가짜 자막 밈)
- 음성: edge-tts 한국어/영어 음성, 캐릭터별 속도·피치, 본인 녹음 파일 삽입
- 효과: 정지화면+자막, 슬로모션/배속, 줌, 화면 흔들림, 삐- 처리, 효과음(직접 합성), 상단 자막
- 필터: vintage, noir, vhs, deepfried, warm, cold
- 인트로/아웃트로 카드, 상시 `PARODY` 라벨

## 주의
- 본인 영상, CC 라이선스, 또는 원작을 풍자·논평하는 **짧은 발췌**에만 사용하세요.
- 실존 인물의 목소리 복제·허위 발언처럼 보이는 연출은 지원하지 않습니다.
- 업로드 시 PARODY 라벨과 원작 출처(아웃트로 카드)를 유지하세요.
