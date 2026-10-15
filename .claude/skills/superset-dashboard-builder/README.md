# superset-dashboard-builder (Claude 스킬)

Apache Superset 7.0 REST API로 **데이터셋 → 차트 → 대시보드(탭/레이아웃) → 네이티브 필터**를
한 번에 만들어 주는 Claude Code 스킬입니다. 차트 종류를 말로 지정하거나, Tableau 화면 캡처를
전달하면 동일한 형태·색·정렬·필터로 Superset에 재구성합니다.

## 설치

```bash
# 프로젝트 스킬 (이 저장소 그대로): .claude/skills/superset-dashboard-builder/
# 개인 스킬: 아래처럼 복사하면 모든 프로젝트에서 사용
cp -r .claude/skills/superset-dashboard-builder ~/.claude/skills/
pip install requests pyyaml
export SUPERSET_URL=https://superset.example.com SUPERSET_USERNAME=... SUPERSET_PASSWORD=...
```

## 사용 예

```
이 Tableau 캡처(ae_dashboard.png)를 Superset 대시보드로 만들어줘.
데이터는 clinical DB의 subjects, adverse_events 테이블이야. 사이트/국가/기간 필터가 전체 차트에 걸려야 해.
```

Claude는 ① 이미지를 분석해 차트별 카드(마크 타입, 축, 집계, 색, 정렬, 필터)를 적고 → ② 데이터셋 컬럼을
조회해 매핑을 확인한 뒤 → ③ `spec.yaml`을 작성하고 → ④ `scripts/build_dashboard.py spec.yaml --validate`
로 생성/검증 → ⑤ 대시보드 URL과 비교 포인트를 알려줍니다. spec은 멱등(idempotent)이라 수정 후
재실행하면 같은 객체가 갱신됩니다.

## 구성

```
SKILL.md                       스킬 본문 (워크플로, 주의점)
scripts/superset_client.py     인증(JWT+CSRF) + CLI (조회/생성/검증/export/import/screenshot)
scripts/chart_builder.py       간단 스펙 → 차트 params(form_data) / query_context
scripts/dashboard_builder.py   레이아웃(탭/행) → position_json, 필터 → native_filter_configuration
scripts/build_dashboard.py     전체 적용 오케스트레이션 (--dry-run / --validate / --state)
scripts/screenshot_dashboard.py 로컬 headless Chromium으로 대시보드/탭별 스크린샷 (Tableau 이미지와 비교용)
references/*.md                API·차트 파라미터·Tableau 매핑·레이아웃·필터·색상 레퍼런스
assets/spec.example.yaml       임상시험 예제 (데이터셋 2, 차트 7, 탭 3, 필터 5)
```

## 로컬 검증

`assets/spec.example.yaml`은 **Superset 7.0.0rc1 공식 Docker 이미지**(`apache/superset:7.0.0rc1`)와
6.1.0(pip)에서 실제로 적용·검증되었습니다 (데이터셋 2, 차트 7 쿼리 검증, 탭 3, 네이티브 필터 6,
export/import, 브라우저 렌더링 확인). 7.0은 아직 PyPI에 올라오지 않아(최신 6.1.0) Docker 이미지로
검증했습니다. 7.0 API 차이(`owners`→`editors`/`viewers`, Swagger 기본 비활성 등)는
`references/api-reference.md`에 정리했습니다.
