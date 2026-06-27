# ⚽ 또또 — 사무실 월드컵 토토

2026 FIFA 월드컵 스코어 예측(토토) 웹 서비스. FastAPI + JSON 파일 저장, Railway 배포.

## 페이지

| 경로 | 파일 | 설명 |
|------|------|------|
| `/` | `v2.html` | **메인** — Apple Sports 스타일 FIFA 2026 브래킷(전체뷰 + 라운드 스크롤뷰), 경기별 베팅·스코어 매트릭스 |
| `/v2` | `ttoto.html` | 기존(레거시) 페이지 백업 |
| `/admin` | `admin.html` | 관리자 콘솔 |

## 주요 기능

**사용자 (메인 `/`)**
- 전체 토너먼트 한눈에 보기(ALL) ↔ 라운드별 스크롤뷰(GS·R32·R16·QF·SF·결승)
- 조별리그 순위표 + 토너먼트 브래킷 트리
- 경기 선택 → 스코어 예측 베팅, 베팅 현황을 **스코어 매트릭스**(홈×원정, 칸=예측 인원) 또는 리스트로 확인
- 플로팅(+) 메뉴 — 피드백 작성 / 베팅중인 경기 리스트
- 게임 시작 24시간 전 무지개 공지 바, 관리자 지정 팝업 공지

**관리자 (`/admin`)**
- worldcup26.ir 에서 경기 일정 가져오기(임포트 미리보기 → 확정)
- 경기 결과 등록/삭제, 베팅 입금확인·배당 처리
- 경기 소프트 삭제 + 복구/영구삭제, 상태 변경(미등록/베팅중/마감/종료)
- 피드백 관리(답글), 팝업 공지 등록·게시일시 지정
- Claude 기반 AI 스코어 예측

## 로컬 실행

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
# http://localhost:8000  (관리자: /admin)
```

## 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `DATA_DIR` | `./data` | JSON 데이터 저장 경로 (Railway 볼륨은 `/data`) |
| `ANTHROPIC_API_KEY` | — | AI 스코어 예측용 (미설정 시 해당 기능만 비활성) |
| `PORT` | — | 서버 포트 (Railway 가 주입) |

## 데이터 (`$DATA_DIR/*.json`)

`games.json` 경기 · `bets.json` 베팅 · `results.json` 결과 · `config.json` 설정(베팅금액·카카오페이 링크·팝업) · `feedback.json` 피드백 · `auth.json` 관리자 토큰 · `ai_predictions.json` AI 예측

> `.gitignore` 에 `data/` 포함 — 저장소에 데이터는 커밋되지 않음.

## 관리자 인증

- 토큰은 `auth.json` 에 저장되며, 설정된 경우 관리자 API는 `X-Admin-Token: <토큰>` 헤더 필요.
- 토큰 설정: `POST /api/admin/auth/token`. (토큰 미설정 시 관리자 API 개방)

## API 요약

**공개**
| Method | Path | 설명 |
|--------|------|------|
| GET | `/api/config` | 설정(베팅금액·카카오페이·팝업) |
| GET | `/api/games` | 경기 목록 |
| GET / POST | `/api/bets` | 베팅 조회 / 제출 |
| GET | `/api/results` | 경기 결과 |
| GET | `/api/ai-predictions` | AI 예측 결과 |
| GET / POST | `/api/feedback` | 피드백 조회 / 작성 |
| GET | `/api/auth/status` | 관리자 토큰 설정 여부 |

**관리자** (`X-Admin-Token` 필요)
| Method | Path | 설명 |
|--------|------|------|
| PATCH | `/api/admin/config` | 설정 수정 |
| POST · DELETE | `/api/admin/games` · `/api/admin/games/{id}` | 경기 생성 / 소프트 삭제 |
| POST | `/api/admin/games/{id}/restore` · `/permanent` | 복구 / 영구삭제 |
| GET · POST | `/api/admin/games/import/preview` · `/import/confirm` | 외부 일정 가져오기 |
| POST | `/api/admin/games/enrich-bracket` | 브래킷(라운드·라벨) 보강 |
| PUT · DELETE | `/api/admin/results/{game_id}` | 결과 등록 / 삭제 |
| GET · DELETE | `/api/admin/bets` · `/api/admin/bets/{id}` | 베팅 조회 / 삭제 |
| GET · DELETE · POST | `/api/admin/feedback…` | 피드백 조회 / 삭제 / 답글 |
| POST | `/api/admin/ai-predict` | AI 예측 생성 |
| POST | `/api/admin/auth/token` | 관리자 토큰 설정 |

## 외부 연동

- **worldcup26.ir** (무료·키 불필요) — 경기 일정 임포트
- **Anthropic Claude** (`claude-haiku-4-5-20251001`) — AI 스코어 예측

## 배포 (Railway)

```bash
railway up --service Ttoto
```

- 빌드: NIXPACKS · 시작: `uvicorn main:app --host 0.0.0.0 --port $PORT` (`Procfile` / `railway.json`)
- 데이터 영속화를 위해 Railway 볼륨을 `/data` 에 마운트하고 `DATA_DIR=/data` 설정
