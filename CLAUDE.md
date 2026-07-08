# Ttoto (사무실 월드컵 토토) — 프로젝트 규칙

2026 FIFA 월드컵 스코어 토토. FastAPI + SQLite(단일 파일 `main.py`), 프론트는 순수 HTML/JS 단일 파일들. Railway 배포.

## 배포 규칙 (중요)

- **코드 수정 후**: 로컬 `git commit` → 즉시 `railway up --service Ttoto` 자동 실행 (별도 요청 불필요)
- **GitHub push는 사용자가 명시적으로 요청할 때만** ("push 해줘"). 그 전엔 로컬 커밋만.
- push 전 반드시 `git fetch` 후 원격 선행 커밋 확인 — 다른 세션/도구(Codex 등)가 커밋했을 수 있음. 충돌 시 양쪽 의도 보존해 병합.
- 배포 반영 확인: `until curl -s https://ttoto-production.up.railway.app/ | grep -q "<이번 변경의 고유 문자열>"; do sleep 8; done`
- 커밋 메시지: 한국어, `fix:`/`feat:`/`docs:` 접두사, 한 줄 요약

## 페이지 구조

| 경로 | 파일 | 설명 |
|------|------|------|
| `/` | **v2.html** | 메인 (Apple Sports 스타일 브래킷). **메인 수정 = v2.html 수정** |
| `/v2` | ttoto.html | 구버전 백업 (특별 요청 없으면 건드리지 않음) |
| `/admin` | admin.html | 관리자 |

- 저장소: SQLite (`$DATA_DIR/ttoto.db`, Railway 볼륨 `/data`). 각 행의 `data`(JSON)가 원본, 조회 컬럼은 GENERATED 가상 컬럼. 기존 `read_json`/`get_*` API 유지됨.
- 외부: worldcup26.ir 경기 임포트(느림, 타임아웃 40s), Anthropic API(AI 예측, `ANTHROPIC_API_KEY`)

## 검증 절차 (수정 후 필수)

1. JS: `node -e "const f=require('fs').readFileSync('v2.html','utf8');const m=f.match(/<script>([\s\S]*)<\/script>/);new Function(m[1]);console.log('JS OK');"`
2. Python: `python3 -c "import ast;ast.parse(open('main.py').read());print('PY OK')"`
3. 배포 → curl 마커 확인 → 가능하면 Chrome MCP로 라이브 동작 검증
4. **iOS 사파리 이슈는 데스크탑 크롬에서 재현 안 됨** — 최종 확인은 사용자가 아이폰으로. 추측 수정 금지, 원인 측정 후 수정.

## 절대 규칙 / 과거 삽질 교훈

- **CSS 클래스명에 `ad`, `adv`, `banner`, `sponsor` 금지** — iOS 광고차단기가 요소를 숨겨버림 (조별 1·2위 미표시 버그의 원인이었음. `adv`→`qual`로 해결)
- `body{overflow:hidden}` 잠금 금지 — iOS 사파리 뷰포트/하단바를 흔들어 회색 공백 유발. 시트는 오버레이+`overscroll-behavior:contain`으로 격리
- `-webkit-overflow-scrolling:touch` — 긴 스크롤 컨테이너에서 absolute 자식을 클리핑함, 사용 주의
- window native smooth scroll(`behavior:'smooth'`)은 이 페이지에서 신뢰 불가 — 즉시 스크롤 + 카드 top 트랜지션 조합 사용
- 32개 절대배치 카드의 `transition:top`이 전환 중 layout thrashing 유발 — 뷰 전환 시 `body.bk-snap`으로 트랜지션 임시 해제
- 입력 필드 font-size는 16px 유지 (iOS 포커스 확대 방지)
- 보안: 출력 시 `esc()`(escapeHtml) 필수, 피드백 입력은 한글/영문/숫자만·길이 제한·이름 필수

## v2.html 핵심 사양

**게임 상태 `gState(g)`**: `ended`(결과 있음 or status=ended) / `closed`(마감) / `pending`(미등록) / `open`(베팅중)

**카드 보더색 `gameBorderClr(g)`** (전체뷰/스크롤뷰/리스트 공통 기준):
- ended 당일 → blue, ended 이전 → gold
- closed → pink
- open + 시작시각 있음 + 양팀 확정 → green
- 양팀 확정인데 open 아님 → white
- 그 외 → transparent

**흰 배경 규칙 (스크롤뷰 카드·조별/베팅중 리스트 행)**: **베팅중(open) 또는 베팅 1건 이상**이면 배경 흰색+어두운 글자.
- open → 초록 반짝 보더(`greenBlink`): `.bk-card.bettable`, `.grp-game.gg-open` (전체뷰는 `.ovvbet`, open만 흰 배경)
- 비open+베팅≥1 → 흰 배경만(`.bk-card.betbg`, `.grp-game.gg-bet`), 보더는 상태색(gameBorderClr: 골드/블루/핑크) 유지 — 초록 반짝 금지

**선택 표시**: 흰색 반짝 보더(`selBlink`). 선택 id는 `selGid`에 저장해 재렌더에도 유지

**베팅중 탭(플로팅 + 메뉴)**: ① open+closed 전부(임박순) ② ended 중 베팅≥1건(최근 종료순). 행 색상은 위 공통 기준.

**베팅 상세**: 기본 뷰 = 매트릭스(0건이면 안내문·open일 때만), 하단 상태문구 open→베팅버튼 / closed→마감 / ended→종료 / pending→준비중

**시트 화살표 플로트**: ⌄⌄⌄ **세로 쉐브론 캐스케이드**(확정 디자인, 가로 ↓↓↓ 아님). 게임 상세(`#g-hint`)와 리스트 팝업(`#grp-hint`, 베팅중·조별) 공통 — 시트 아래 안 보이는 내용이 12px 이상 남으면 표시, 맨 아래 도달 시 숨김, 탭=맨 아래로 스크롤(smooth+즉시 폴백). 로직은 `updateSheetHint(bodyId,hintId)` 공용

**전체뷰(ALL)**: 진입 기본 화면. 카드 클릭 → 해당 라운드 스크롤뷰 + 그 카드 위치로 스크롤 + 흰 반짝 선택. GS 카드는 `exitOvvAndGo('GS',null,'<조>')`로 조 순위표(`stand-<조>`)로 이동.

## 알려진 미해결 이슈

- iOS: 팝업 열었다 닫으면 사파리 하단 바 뒤 회색 잔존 (탐구 이력: theme-color/overflow잠금/visibility 시도·원복 반복 — git log 참고)
- iOS: 전체뷰→카드 이동 시 nav 버튼 깜빡임 잔존 가능성
