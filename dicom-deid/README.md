# dicom-deid — DICOM 비식별화 (Phase 1 MVP)

DCM 파일이나 ZIP을 올리면 헤더 태그로 리댁션 템플릿을 자동으로 고르고, 영상에 새겨진(burned-in) 개인정보 영역을 검게 칠한 뒤, 헤더를 DICOM PS3.15 Basic Profile 기준으로 비식별화합니다.
맞는 템플릿이 없거나 후보가 모호하면 자동으로 통과시키지 않고 검토 대기(Quarantine)로 보냅니다.

설계 문서: "DICOM 리댁션 Phase 1 MVP 상세 설계" (Claude Docs)

## 빠른 시작

```bash
cd dicom-deid
pip install -e ".[test]"
export DEID_DATA_DIR=./data          # 원본·재식별 키가 저장됨: 저장소 밖 경로 권장
export DEID_HMAC_KEY=<운영 비밀값>    # 없으면 data/reid/hmac.key 를 생성해 씀 (개발용)

# 1) 템플릿 등록 → 샘플 3건 드라이런 → 제출 → 다른 사용자가 승인
deid --user author1 template create templates/US-GE-LOGIQE9-960x720.json
deid --user author1 template dry-run US-GE-LOGIQE9-960x720 s1.dcm s2.dcm s3.dcm
deid --user author1 template submit US-GE-LOGIQE9-960x720
deid --user approver1 template approve US-GE-LOGIQE9-960x720

# 2) 처리
deid --user site1 process exam.zip --study STUDY001 --site S01 --subject S-01-0001

# 3) API 서버
uvicorn deid.api:app --port 8000     # /docs 에 OpenAPI 화면
```

테스트: `python -m pytest` (74건). 테스트는 코드로 만든 합성 DICOM과 카나리 문자열만 사용합니다.

## 구성

| 모듈 | 역할 |
| --- | --- |
| `templates.py` | 템플릿 JSON 검증, 조건 연산자(eq, in, icontains, regex 등), 매칭 점수, 동점 시 AMBIGUOUS |
| `pixel.py` | 압축 해제 → 영역 채움 → Explicit VR Little Endian 저장. 색공간별 검정 값, 멀티프레임, Overlay/Icon 제거 |
| `profile.py` | 헤더 비식별화(Basic + Retain Device Identity + Clean Descriptors), 날짜 이동, UID 재발급, 잔존 PHI 검사 |
| `pipeline.py` | 파일 한 건 처리 9단계, 상태(DONE/QUARANTINED/REJECTED/FAILED)와 사유 코드 |
| `service.py` | Job, 템플릿 승인 흐름, Quarantine, 역할 검사 (API와 CLI가 공용) |
| `store.py` | SQLite: `deid.sqlite`(업무·감사추적), `reid/reid.sqlite`(Subject·UID 매핑) |
| `api.py`, `cli.py` | REST API, 명령줄 |

## 동작 규칙

- **매칭**: 승인(APPROVED)된 버전만 씁니다. 점수는 `시험 전용이면 1000 + priority + 10 × 참인 조건 수`입니다. 동점인데 하는 일(영역)이 다르면 AMBIGUOUS로 보냅니다.
- **템플릿 제약**: REDACT 템플릿은 `Rows`, `Columns`를 `eq`로 고정해야 하고, 영역이 그 크기를 넘으면 등록되지 않습니다. 식별 태그(PatientName 등)는 조건에 쓸 수 없습니다. 매칭 추적 기록에 값이 남기 때문입니다.
- **승인**: 일치하는 드라이런 샘플이 3건 이상이어야 제출·승인할 수 있고, 작성자는 자기 버전을 승인할 수 없습니다(DB CHECK 제약도 있음). 승인된 버전은 수정할 수 없으며(DB 트리거), 새 버전이 승인되면 이전 버전은 RETIRED가 됩니다.
- **픽셀**: 손실 압축 원본은 다시 압축하지 않고 `LossyImageCompression=01`을 표시합니다. MONOCHROME1은 최댓값, 부호 있는 데이터는 최솟값으로 채웁니다.
- **헤더**: PatientName → `시험^Subject`, PatientID → Subject, 생년월일·Accession 비움, 기관·검사자·장비 일련번호 삭제, Private 태그와 모든 PN 값 삭제, 제조사·모델·소프트웨어 버전 유지. 날짜는 대상자별 1~365일 오프셋으로 이동합니다.
- **출력 검증**: 원본 헤더의 식별 문자열이 결과 헤더에 하나라도 남으면 저장하지 않고 `RESIDUAL_PHI`로 Quarantine합니다. 값은 기록하지 않고 개수만 남깁니다.
- **저장 분리**: 원본과 처리 전 미리보기는 `data/raw`, 비식별본은 `data/deid`, 재식별 키는 `data/reid`(권한 700)에 둡니다. PatientID는 HMAC으로만 저장합니다.
- **감사추적**: `audit_log`는 INSERT만 가능하고(트리거), 각 행이 이전 행의 해시를 포함합니다. `deid audit-verify`로 체인을 검증합니다.

## 사유 코드

| 상태 | 코드 | 의미 |
| --- | --- | --- |
| REJECTED | `INVALID_DICOM` | DICM 마커 없음, 파싱 실패 |
| REJECTED | `UNSUPPORTED_SOP` | SR, PDF 등 영상이 아닌 SOP Class |
| REJECTED | `NO_PIXEL_DATA`, `FILE_TOO_LARGE`, `REJECTED_BY_TEMPLATE`, `REJECTED_BY_REVIEWER` | |
| QUARANTINED | `NO_TEMPLATE`, `AMBIGUOUS` | 템플릿 없음 / 동점 |
| QUARANTINED | `UNSUPPORTED_TS`, `UNSUPPORTED_PHOTOMETRIC` | MPEG/H.264, Big Endian, PALETTE COLOR 등 |
| QUARANTINED | `SUBJECT_CONFLICT` | 같은 PatientID가 이미 다른 Subject에 연결됨 |
| QUARANTINED | `RESIDUAL_PHI` | 출력 헤더에 원본 식별 문자열 잔존 |
| FAILED | `ERROR:<예외>` | 3회 시도 후 실패 |

## 포함된 템플릿

`templates/US-GE-LOGIQE9-960x720.json`: GE LOGIQ E9(소프트웨어 R2.0.3) 초음파 960×720. 상단 정보 바 y=0~66 중 왼쪽 두 칸(x=0~668: 기관명·검사 일시·검사자 / 환자 이름·번호)을 지웁니다. 오른쪽의 MI/TIs, 프로브·프리셋 칸은 장비 설정값이라 남깁니다.
경계는 샘플 37건(RGB 32, MONOCHROME2 5)에서 측정했고 모든 샘플에서 같았습니다.

## 설계 문서와 다른 점 (MVP 한정)

| 설계 | 구현 | 운영 전 할 일 |
| --- | --- | --- |
| PostgreSQL `deid`/`reid` 스키마 | SQLite 파일 두 개 (테이블 구조는 동일) | PostgreSQL로 이전 |
| 객체 저장소 + presigned 업로드 | 로컬 디렉터리, API로 직접 업로드 | 버킷·KMS 키 분리 |
| Job 큐 + Worker | API 프로세스 안의 백그라운드 작업 | 큐와 별도 Worker |
| SSO 인증, 승인 시 재인증 | `X-User`, `X-Roles` 헤더 (개발용) | `api.current_actor` 교체 |
| PatientID HMAC + KMS 암호문 | HMAC만 저장 | 재식별 요건이 있으면 암호문 추가 |
| RE2 정규식 | Python `re` + 200자 제한 | RE2 적용 |
| ReferringPhysicianName 삭제(X) | 비움(Z): Type 2 속성이라 삭제하면 IOD 위반 | 설계 문서 반영 |
