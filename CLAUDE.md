# Ttoto (사무실 월드컵 토토) — 프로젝트 규칙

2026 FIFA 월드컵 스코어 토토. FastAPI + PostgreSQL(단일 파일 `main.py`), 프론트는 순수 HTML/JS 단일 파일들. Railway 배포.

## 배포 규칙 (중요)

- **배포 = `git push origin main`** → Railway가 GitHub 연동으로 자동 빌드·배포 (무중단: `/health` 헬스체크 통과 후 트래픽 전환)
- push 전 반드시 `git fetch` 후 원격 선행 커밋 확인 — 다른 세션/도구(Codex 등)가 커밋했을 수 있음. 충돌 시 양쪽 의도 보존해 병합.
- 배포 반영 확인: `until curl -s https://ttoto-production.up.railway.app/ | grep -q "<이번 변경의 고유 문자열>"; do sleep 8; done`
- 커밋 메시지: 한국어, `fix:`/`feat:`/`docs:` 접두사, 한 줄 요약

## 인프라 (Railway)

- **Ttoto 앱**: 무상태, **replicas 2** (로드밸런싱). 볼륨 없음 → 무중단 배포 가능
- **Postgres**: 1대 (stateful). 앱은 `DATABASE_URL` 참조변수로 접속 (private 네트워크)
- Railway private 네트워크는 부팅 직후 수 초간 미준비 → PG 첫 연결은 60초 재시도 로직 있음 (제거 금지)
- **PG 풀 max 8/replica + 획득 timeout 10s — 절대 늘리지 말 것**: Railway Postgres `max_connections`가 낮아(~22), replica 2 × max_size가 이를 넘으면 커넥션 대기가 무한 행 → /health 실패 → 재시작 폭풍 (2026-07-10 2차 장애: 20×2=40으로 직접 겪음). 캐시 덕에 8이면 충분.
- **공개 GET 마이크로캐시**(`_pub_cached`, TTL 3s): config/games/bets/results/feedback 기본 변형만, `write_json`이 같은 프로세스 캐시 전체 무효화. 다른 replica는 최대 3초 늦게 보임(허용). 결과 등록 직후 동시 접속 스파이크로 풀이 포화돼 접속 불가였던 1차 장애(2026-07-10)의 대책 — 제거 금지

## 페이지 구조

| 경로 | 파일 | 설명 |
|------|------|------|
| `/` | **v2.html** | 메인 (Apple Sports 스타일 브래킷). **메인 수정 = v2.html 수정** |
| `/v2` | ttoto.html | 구버전 백업 (특별 요청 없으면 건드리지 않음) |
| `/admin` | admin.html | 관리자 |

## 저장소

- **Postgres가 주 저장소** (`DATABASE_URL` 있으면), 없으면 SQLite 폴백(`$DATA_DIR/ttoto.db`, 로컬 개발용). 두 백엔드 동일 설계.
- 테이블: `games` `bets` `feedback`(리스트형, seq PK) · `results` `ai_predictions`(game_id PK) · `config` `auth`(key-value) · `meta` · `documents`(레거시 백업)
- **각 행의 `data`(JSONB)가 원본**, 조회 컬럼(bets.name, games.status 등)은 `GENERATED ALWAYS AS ... STORED`로 DB가 자동 파생 — 컬럼·JSON 불일치가 구조적으로 불가능
- 애플리케이션 API는 `read_json`/`write_json`/`get_*` 유지 — **비즈니스 로직에서 DB 직접 접근 금지**, 이 계층만 사용
- 이관은 자가 치유식: 부팅마다 "PG 테이블 비어있음 + SQLite 원본 있음 → 채움". 데이터 있는 PG 테이블은 절대 안 덮음
- 백업: 관리자 SQL 콘솔의 **📦 전체 백업(JSON)** 버튼 (`GET /api/admin/db/export`)
- 진단: `GET /api/admin/db/info` (백엔드·테이블 건수), 읽기 전용 SQL 콘솔 `POST /api/admin/db/query` (SELECT/WITH만, read-only 연결 이중 방어 — 완화 금지)

## 이월 판돈 (carryover)

- `config.carryover`가 원본. **결과 최초 등록 시 서버가 자동 정산** (`_settle_carryover`): 베팅 있는 경기가 무당첨 종료 → 판돈 누적 / 이월 대상 경기가 당첨 종료 → 0으로 소진. 정정(재등록)은 정산 제외.
- 정산 시 결과에 표시용 기록 남김: 대상 경기 당첨 소진 → `carryover_used` / 대상 경기 무당첨 재이월 → `carryover_in`(그 경기에 걸려 있던 이월). 프론트 종료 경기 표시는 `used || in` 사용.
- **이월 대상 = 결과 미등록 경기 중 시작시각 최빠른 경기.** 서버(`_settle_carryover`)와 프론트(`carryTargetId`)가 같은 기준을 써야 함 — 한쪽만 바꾸지 말 것
- 당첨 판정도 서버(`_bet_hits`)·프론트(`hitRes`) 동일: exact=정확 스코어, wdl=승무패 방향
- **경기별 이월 여부 `carry_mode`** (`carry` 기본 | `refund`): 무당첨 종료 시 refund면 그 경기 판돈은 이월하지 않고 반환(`res.refunded_pot` 기록, 실제 반환은 오프라인). 걸려 있던 이월분(carryover_in)은 모드와 무관하게 재이월. 관리자 게임 관리 탭 "이월 여부" 컬럼(베팅 방식 오른쪽)에서 변경.
- 관리자 설정의 "이월 판돈" 필드는 수동 보정용

## 검증 절차 (수정 후 필수)

1. JS: `node -e "const f=require('fs').readFileSync('v2.html','utf8');const m=f.match(/<script>([\s\S]*)<\/script>/);new Function(m[1]);console.log('JS OK');"`
2. Python: `python3 -c "import ast;ast.parse(open('main.py').read());print('PY OK')"`
3. 저장 계층 수정 시: 임베디드 PG(`pip install pgserver`)로 PG 경로까지 테스트 (이 레포는 SQLite·PG 둘 다 지원해야 함)
4. 배포 → curl 마커 확인 → 가능하면 Chrome MCP로 라이브 동작 검증
5. **iOS 사파리 이슈는 데스크탑 크롬에서 재현 안 됨** — 최종 확인은 사용자가 아이폰으로. 추측 수정 금지, 원인 측정 후 수정.

## 외부 연동

- worldcup26.ir 경기 임포트(느림, 타임아웃 40s), Anthropic API(AI 예측, `ANTHROPIC_API_KEY`)
- **경기 일시 KST 자동 변환**: 경기 응답엔 `stadium_id`만 있음(경기장 이름 없음) → `/get/stadiums` 맵(24h 캐시)으로 경기장 확정 → `local_to_kst`가 경기장 시간대 기준 현지→KST 변환(`kst_v2` 마킹). **enrich가 kst_v2 성공 소스의 date/time/venue를 기존 경기에 자동 동기화**(부팅 20초 후+10분마다) — 수동 fix-kst 버튼은 폴백용. 멕시코 3개 도시는 DST 폐지로 연중 UTC-6.

## 절대 규칙 / 과거 삽질 교훈

- **CSS 클래스명에 `ad`, `adv`, `banner`, `sponsor` 금지** — iOS 광고차단기가 요소를 숨겨버림 (조별 1·2위 미표시 버그의 원인이었음. `adv`→`qual`로 해결)
- `body{overflow:hidden}` 잠금 금지 — iOS 사파리 뷰포트/하단바를 흔들어 회색 공백 유발. 시트는 오버레이+`overscroll-behavior:contain`으로 격리
- `-webkit-overflow-scrolling:touch` — 긴 스크롤 컨테이너에서 absolute 자식을 클리핑함, 사용 주의
- window native smooth scroll(`behavior:'smooth'`)은 이 페이지에서 신뢰 불가 — 즉시 스크롤 + 카드 top 트랜지션 조합 사용
- 32개 절대배치 카드의 `transition:top`이 전환 중 layout thrashing 유발 — 뷰 전환 시 `body.bk-snap`으로 트랜지션 임시 해제
- 입력 필드 font-size는 16px 유지 (iOS 포커스 확대 방지)
- **관리자 게임 PATCH는 merge만** (dict 재조립 금지) — `stage`/`home_label`/`away_label`이 날아가면 브래킷 트리 전체가 깨짐 (승무패 변경 사고). enrich 태스크(부팅 20초 후 1회 + 10분마다)가 id 매칭으로 자동 복구하지만, 애초에 날리지 말 것
- 보안: 출력 시 `esc()`(escapeHtml) 필수, 피드백 입력은 한글/영문/숫자만·길이 제한·이름 필수
- **베팅 API(`POST /api/bets`) 서버측 게이트 필수** — 화면 버튼 숨김만으론 콘솔에서 API 직접 호출 가능(종료 경기에 정답 베팅 부정 등록됨, 2026-07 실제 발견). status==open + 결과 미등록 + 시작시각 이전만 허용. 정정·복원은 관리자 인증 `POST /api/admin/bets`로만. 이 검증 완화 금지.

## v2.html 핵심 사양

**게임 상태 `gState(g)`**: `ended`(결과 있음 or status=ended) / `closed`(마감) / `pending`(미등록) / `open`(베팅중)

**카드 보더색 `gameBorderClr(g)`** (전체뷰/스크롤뷰/리스트 공통 기준):
- ended 당일 → blue, ended 이전 → gold
- closed → pink
- open + 시작시각 있음 + 양팀 확정 → green
- 양팀 확정인데 open 아님 → white
- 그 외 → transparent

**흰 배경 규칙 (전체뷰 미니카드·스크롤뷰 카드·조별/베팅중 리스트 행 공통)**: **베팅중(open) 또는 베팅 1건 이상**이면 배경 흰색+어두운 글자.
- open → 초록 반짝 보더(`greenBlink`): `.bk-card.bettable`, `.grp-game.gg-open`, 전체뷰 `.ovvbet`
- 비open+베팅≥1 → 흰 배경만(`.bk-card.betbg`, `.grp-game.gg-bet`, 전체뷰는 인라인 `wbg`), 보더는 상태색(gameBorderClr) 유지 — 초록 반짝 금지

**선택 표시**: 반짝 보더(`selBlink`) — 색은 카드별 `--selclr` 변수로 **상태 보더색과 동일** (transparent/회색이면 흰색 폴백). 기존 보더색을 덮지 않음. 선택 id는 `selGid`에 저장해 재렌더에도 유지

**KO 카드 팀명**: 모바일·데스크탑 모두 국가명(full) 표기, 23자 초과 시 22자+".." truncation (`bkCardHtml`의 `trunc`)

**전체뷰 GS 카드 강조**: 32강에 실제 배정된 팀(`r32Adv` set)만 — 1·2위 초록, 3위 진출 흰색, 그 외 dim(#555)

**이월 판돈 표시**: 대상 경기(= `carryTargetId()`, 결과 미등록 중 시작 최빠름)에만 — 카드(베팅가능: CTA 위 골드 라인 / 그 외: "판돈 n원 (이월 포함)")와 상세 시트(합산 스탯 + 골드 배너 2줄: 포함 금액 / 줄바꿈 / 베팅+이월 내역)

**베팅중 탭(플로팅 + 메뉴)**: ① open+closed 전부(임박순) ② ended 중 베팅≥1건(최근 종료순). 행 색상은 위 공통 기준.

**베팅 상세**: 기본 뷰 = 매트릭스(0건이면 안내문·open일 때만), 하단 상태문구 open→베팅버튼 / closed→마감 / ended→종료 / pending→준비중

**매트릭스 셀 선택**: 셀 탭 → **노란 보더**(`mx-sel`) + **말풍선 팝오버**(`#mx-pick`, 꼬리가 셀 가리킴, 버튼 가로 배치). 클릭 가능 = 베팅 있는 셀 or open 경기의 모든 셀(빈 셀 포함).
- 📊 상세(베팅 있는 셀만) = 리스트 탭 해당 스코어로 이동 / ⚽ 배팅(open 경기만, 빈 셀 포함) = 베팅 폼에 그 스코어 프리필
- 바깥 탭으로 닫힘, 재렌더 시 자동 소멸(#g-body 내부 요소)

**베팅 상세 금액 표기**: 당첨 판정은 베팅 방식별(`bet_type`: exact=정확 스코어 일치, wdl=승/무/패 방향 일치). **분배 판돈에 이월 포함 필수**.
- 진행중(open/closed) 리스트: 스코어별 "**예상** 인당 n원" = (입금확인 판돈 + 현재 이월 `carryAmt(g)`) ÷ 그 스코어 입금확인 인원
- 종료(ended) 리스트: **당첨 스코어에만** "🏆 당첨 인당 n원" = (입금확인 판돈 + `res.carryover_used`) ÷ 입금확인 당첨자 — 낙첨 스코어엔 금액 미표기. `carryover_used`는 정산 시 서버가 결과에 기록(이월 소진 경기만)
- 종료 + 당첨자 0명: 상단에 "💸 당첨자가 없어 판돈 n원은 다음 경기로 이월됩니다" 골드 안내 박스

**시트 화살표 플로트**: ⌄⌄⌄ **세로 쉐브론 캐스케이드**(확정 디자인, 가로 ↓↓↓ 아님). 게임 상세(`#g-hint`)와 리스트 팝업(`#grp-hint`, 베팅중·조별) 공통 — 시트 아래 안 보이는 내용이 12px 이상 남으면 표시, 맨 아래 도달 시 숨김, 탭=맨 아래로 스크롤(smooth+즉시 폴백). 로직은 `updateSheetHint(bodyId,hintId)` 공용

**전체뷰(ALL)**: 진입 기본 화면. 카드 클릭 → 해당 라운드 스크롤뷰 + 그 카드 위치로 스크롤 + 흰 반짝 선택. GS 카드는 `exitOvvAndGo('GS',null,'<조>')`로 조 순위표(`stand-<조>`)로 이동.

## 알려진 미해결 이슈

- iOS: 팝업 열었다 닫으면 사파리 하단 바 뒤 회색 잔존 (탐구 이력: theme-color/overflow잠금/visibility 시도·원복 반복 — git log 참고)
- iOS: 전체뷰→카드 이동 시 nav 버튼 깜빡임 잔존 가능성
