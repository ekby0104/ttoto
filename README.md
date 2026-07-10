# ⚽ 또또 — 사무실 월드컵 토토

2026 FIFA 월드컵 스코어 예측(토토) 웹 서비스. FastAPI + PostgreSQL, Railway 배포 (무중단 · 2 replicas).

## 페이지

| 경로 | 파일 | 설명 |
|------|------|------|
| `/` | `v2.html` | **메인** — Apple Sports 스타일 FIFA 2026 브래킷(전체뷰 + 라운드 스크롤뷰), 경기별 베팅·스코어 매트릭스 |
| `/v2` | `ttoto.html` | 기존(레거시) 페이지 백업 |
| `/admin` | `admin.html` | 관리자 콘솔 |

## 주요 기능

**사용자 (메인 `/`)**
- 전체 토너먼트 한눈에 보기(ALL) ↔ 라운드별 스크롤뷰(GS·R32·R16·QF·SF·결승)
- 조별리그 순위표 + 토너먼트 브래킷 트리 (32강 진출팀 강조)
- 경기 선택 → 스코어 예측 베팅, 베팅 현황을 **스코어 매트릭스**(홈×원정, 칸=예측 인원) 또는 리스트로 확인
- **이월 판돈**: 당첨자 없이 종료된 판돈이 다음 경기에 자동 누적·표시, 당첨 경기에서 소진
- 플로팅(+) 메뉴 — 피드백 작성 / 베팅중인 경기 리스트
- 게임 시작 24시간 전 무지개 공지 바, 관리자 지정 팝업 공지

**관리자 (`/admin`)**
- worldcup26.ir 에서 경기 일정 가져오기(임포트 미리보기 → 확정), 브래킷 데이터 보강(자동 10분 주기 + 수동)
- 경기 결과 등록/삭제 (결과 등록 시 이월 판돈 자동 정산 + 게임 자동 종료 처리)
- 베팅 입금확인·배당 처리, 경기 소프트 삭제 + 복구/영구삭제, 상태 변경
- 피드백 관리(답글), 팝업 공지 등록·게시일시 지정
- Claude 기반 AI 스코어 예측
- **SQL 콘솔** (읽기 전용, SELECT/WITH만) + **전체 데이터 JSON 백업** 다운로드

## 아키텍처

```
사용자 → (Railway LB) → Ttoto 앱 ×2 replicas (무상태, FastAPI)
                              │ DATABASE_URL (private network)
                          PostgreSQL ×1
```

- **저장소**: PostgreSQL. 각 행의 `data`(JSONB)가 원본이고 조회 컬럼은 `GENERATED ALWAYS AS ... STORED`로 자동 파생 (컬럼·JSON 불일치가 구조적으로 불가능한 하이브리드 설계)
- **테이블**: `games` · `bets` · `feedback` (리스트형, seq PK) / `results` · `ai_predictions` (game_id PK) / `config` · `auth` (key-value) / `meta` / `documents` (레거시 백업)
- `DATABASE_URL` 미설정 시 SQLite(`$DATA_DIR/ttoto.db`) 폴백 — 로컬 개발은 설정 없이 바로 실행 가능
- 부팅 시 자가 치유 이관: 비어 있는 PG 테이블을 SQLite 원본에서 자동 채움 (데이터 있는 테이블은 불변)

## 로컬 실행

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
# http://localhost:8000  (관리자: /admin) — DATABASE_URL 없으면 SQLite로 동작
```

## 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `DATABASE_URL` | — | PostgreSQL 접속 URL (Railway `Postgres.DATABASE_URL` 참조변수). 미설정 시 SQLite 폴백 |
| `DATA_DIR` | `./data` | SQLite 폴백/이관 원본 경로 (로컬 개발용) |
| `ANTHROPIC_API_KEY` | — | AI 스코어 예측용 (미설정 시 해당 기능만 비활성) |
| `PORT` | — | 서버 포트 (Railway 가 주입) |

## 관리자 인증

- 토큰은 `auth` 테이블에 저장되며, 설정된 경우 관리자 API는 `X-Admin-Token: <토큰>` 헤더 필요.
- 토큰 설정: `POST /api/admin/auth/token`. (토큰 미설정 시 관리자 API 개방)

## API 요약

**공개**
| Method | Path | 설명 |
|--------|------|------|
| GET | `/health` | 헬스체크 (DB 연결 확인, Railway 배포 게이트) |
| GET | `/api/config` | 설정(베팅금액·카카오페이·이월 판돈·팝업) |
| GET | `/api/games` | 경기 목록 |
| GET / POST | `/api/bets` | 베팅 조회 / 제출 |
| GET | `/api/results` | 경기 결과 |
| GET | `/api/ai-predictions` | AI 예측 결과 |
| GET / POST | `/api/feedback` | 피드백 조회 / 작성 |
| GET | `/api/auth/status` | 관리자 토큰 설정 여부 |

**관리자** (`X-Admin-Token` 필요)
| Method | Path | 설명 |
|--------|------|------|
| PATCH | `/api/admin/config` | 설정 수정 (이월 판돈 수동 보정 포함) |
| POST · DELETE | `/api/admin/games` · `/api/admin/games/{id}` | 경기 생성 / 소프트 삭제 |
| POST | `/api/admin/games/{id}/restore` · `/permanent` | 복구 / 영구삭제 |
| GET · POST | `/api/admin/games/import/preview` · `/import/confirm` | 외부 일정 가져오기 |
| POST | `/api/admin/games/enrich-bracket` | 브래킷(라운드·라벨) 보강 + 결과 자동 반영 |
| PUT · DELETE | `/api/admin/results/{game_id}` | 결과 등록(이월 자동 정산) / 삭제 |
| GET · DELETE | `/api/admin/bets` · `/api/admin/bets/{id}` | 베팅 조회 / 삭제 |
| GET · DELETE · POST | `/api/admin/feedback…` | 피드백 조회 / 삭제 / 답글 |
| POST | `/api/admin/ai-predict` | AI 예측 생성 |
| POST | `/api/admin/db/query` | 읽기 전용 SQL 콘솔 (SELECT/WITH만, read-only 연결) |
| GET | `/api/admin/db/info` | 저장소 진단 (백엔드·테이블 건수) |
| GET | `/api/admin/db/export` | 전체 데이터 JSON 백업 |
| POST | `/api/admin/auth/token` | 관리자 토큰 설정 |

## 외부 연동

- **worldcup26.ir** (무료·키 불필요) — 경기 일정 임포트
- **Anthropic Claude** (`claude-haiku-4-5-20251001`) — AI 스코어 예측

## 배포 (Railway)

- **`git push origin main`** → GitHub 연동 자동 빌드·배포
- **무중단**: 새 컨테이너가 `/health` 통과 후 트래픽 전환 (`railway.json`의 `healthcheckPath`)
- **이원화**: Ttoto 앱 replicas 2 (무상태라 수평 확장 안전), Postgres는 단일
- 빌드: NIXPACKS · 시작: `uvicorn main:app --host 0.0.0.0 --port $PORT` (`Procfile` / `railway.json`)
