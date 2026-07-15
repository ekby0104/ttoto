"""
월드컵 토토 - FastAPI 서버
실행: uvicorn main:app --reload --port 8000
"""
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Optional
import json, os, time, httpx, re, asyncio, sqlite3, threading
from datetime import datetime, timedelta

app = FastAPI(title="사무실 월드컵 토토 API")

@app.on_event("startup")
async def auto_enrich_bracket_task():
    async def _loop():
        first = True
        while True:
            # 부팅 20초 후 1회 즉시 실행(관리자 수정 등으로 깨진 stage/label 조기 복구), 이후 10분마다
            await asyncio.sleep(20 if first else 600)
            first = False
            try:
                parsed = await _fetch_and_parse_games(False)
                games = get_games()
                _, results_patch = _apply_enrich(games, parsed)
                write_json(GAMES_FILE, games)
                if results_patch:
                    results = get_results()
                    now = int(time.time())
                    new_gids = []
                    for gid, res in results_patch.items():
                        if gid not in results:
                            results[gid] = {**res, "registered_at": now}
                            new_gids.append(gid)
                    write_json(RESULTS_FILE, results)
                    _settle_carryover(new_gids)
            except Exception:
                pass
    asyncio.create_task(_loop())

@app.on_event("startup")
async def auto_close_games():
    async def _loop():
        while True:
            try:
                now_kst = datetime.utcnow() + timedelta(hours=9)
                games = get_games()
                changed = False
                for g in games:
                    if g.get("status") == "open":
                        try:
                            dt = datetime.strptime(f"{g['date']} {g['time']}", "%Y.%m.%d %H:%M")
                        except Exception:
                            continue
                        if now_kst >= dt:
                            g["status"] = "closed"
                            changed = True
                if changed:
                    write_json(GAMES_FILE, games)
            except Exception:
                pass
            await asyncio.sleep(60)
    asyncio.create_task(_loop())

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_DIR          = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
BETS_FILE         = os.path.join(DATA_DIR, "bets.json")
CONFIG_FILE       = os.path.join(DATA_DIR, "config.json")
RESULTS_FILE      = os.path.join(DATA_DIR, "results.json")
AUTH_FILE         = os.path.join(DATA_DIR, "auth.json")
GAMES_FILE        = os.path.join(DATA_DIR, "games.json")
AI_PREDICTIONS_FILE = os.path.join(DATA_DIR, "ai_predictions.json")
FEEDBACK_FILE     = os.path.join(DATA_DIR, "feedback.json")

os.makedirs(DATA_DIR, exist_ok=True)

# ── 저장소: SQLite 정규화 테이블 ─────────────────────────────────────
# v2(documents 문서 통짜 저장) → v3(도메인별 테이블 정규화).
# 설계: 각 행의 data(JSON)가 원본(single source of truth)이고, 조회용 컬럼은
# GENERATED ALWAYS AS json_extract(...) 가상 컬럼으로 DB가 자동 파생.
#   → 컬럼과 JSON이 어긋나는 것이 구조적으로 불가능 + 실제 SQL 조회/집계/인덱스 사용 가능.
#   (Postgres의 jsonb + expression index와 같은 하이브리드 패턴)
# 기존 read_json/write_json/get_* API는 그대로 유지 → 호출부 60곳 변경 없음.
DB_FILE  = os.path.join(DATA_DIR, "ttoto.db")
_db_lock = threading.Lock()

def _db_conn():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def _doc_key(path):
    # bets.json → "bets" 처럼 파일명을 키로 사용 (기존 호출부 변경 불필요)
    return os.path.splitext(os.path.basename(path))[0]

# 리스트형(순서 보존, seq가 PK) / 딕셔너리형(game_id가 PK) / 키-값형
_LIST_TABLES = ("bets", "games", "feedback")
_DICT_TABLES = ("results", "ai_predictions")
_KV_TABLES   = ("config", "auth")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS games(
  seq        INTEGER PRIMARY KEY,
  data       TEXT NOT NULL,
  id         TEXT    GENERATED ALWAYS AS (json_extract(data,'$.id'))         VIRTUAL,
  stage      TEXT    GENERATED ALWAYS AS (json_extract(data,'$.stage'))      VIRTUAL,
  grp        TEXT    GENERATED ALWAYS AS (json_extract(data,'$.group'))      VIRTUAL,
  date       TEXT    GENERATED ALWAYS AS (json_extract(data,'$.date'))       VIRTUAL,
  time       TEXT    GENERATED ALWAYS AS (json_extract(data,'$.time'))       VIRTUAL,
  venue      TEXT    GENERATED ALWAYS AS (json_extract(data,'$.venue'))      VIRTUAL,
  status     TEXT    GENERATED ALWAYS AS (json_extract(data,'$.status'))     VIRTUAL,
  home_name  TEXT    GENERATED ALWAYS AS (json_extract(data,'$.home.name'))  VIRTUAL,
  home_short TEXT    GENERATED ALWAYS AS (json_extract(data,'$.home.short')) VIRTUAL,
  away_name  TEXT    GENERATED ALWAYS AS (json_extract(data,'$.away.name'))  VIRTUAL,
  away_short TEXT    GENERATED ALWAYS AS (json_extract(data,'$.away.short')) VIRTUAL,
  ended_at   INTEGER GENERATED ALWAYS AS (json_extract(data,'$.ended_at'))   VIRTUAL,
  deleted    INTEGER GENERATED ALWAYS AS (coalesce(json_extract(data,'$.deleted'),0)) VIRTUAL
);
CREATE INDEX IF NOT EXISTS idx_games_id     ON games(id);
CREATE INDEX IF NOT EXISTS idx_games_status ON games(status);
CREATE INDEX IF NOT EXISTS idx_games_stage  ON games(stage);

CREATE TABLE IF NOT EXISTS bets(
  seq        INTEGER PRIMARY KEY,
  data       TEXT NOT NULL,
  id         TEXT    GENERATED ALWAYS AS (json_extract(data,'$.id'))         VIRTUAL,
  game_id    TEXT    GENERATED ALWAYS AS (json_extract(data,'$.game_id'))    VIRTUAL,
  name       TEXT    GENERATED ALWAYS AS (json_extract(data,'$.name'))       VIRTUAL,
  h          INTEGER GENERATED ALWAYS AS (json_extract(data,'$.h'))          VIRTUAL,
  a          INTEGER GENERATED ALWAYS AS (json_extract(data,'$.a'))          VIRTUAL,
  amount     INTEGER GENERATED ALWAYS AS (json_extract(data,'$.amount'))     VIRTUAL,
  paid       INTEGER GENERATED ALWAYS AS (json_extract(data,'$.paid'))       VIRTUAL,
  paid_out   INTEGER GENERATED ALWAYS AS (json_extract(data,'$.paid_out'))   VIRTUAL,
  created_at INTEGER GENERATED ALWAYS AS (json_extract(data,'$.created_at')) VIRTUAL
);
CREATE INDEX IF NOT EXISTS idx_bets_game_id ON bets(game_id);
CREATE INDEX IF NOT EXISTS idx_bets_name    ON bets(name);

CREATE TABLE IF NOT EXISTS feedback(
  seq        INTEGER PRIMARY KEY,
  data       TEXT NOT NULL,
  id         TEXT    GENERATED ALWAYS AS (json_extract(data,'$.id'))         VIRTUAL,
  name       TEXT    GENERATED ALWAYS AS (json_extract(data,'$.name'))       VIRTUAL,
  message    TEXT    GENERATED ALWAYS AS (json_extract(data,'$.message'))    VIRTUAL,
  created_at INTEGER GENERATED ALWAYS AS (json_extract(data,'$.created_at')) VIRTUAL
);
CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at);

CREATE TABLE IF NOT EXISTS results(
  game_id       TEXT PRIMARY KEY,
  data          TEXT NOT NULL,
  h             INTEGER GENERATED ALWAYS AS (json_extract(data,'$.h'))             VIRTUAL,
  a             INTEGER GENERATED ALWAYS AS (json_extract(data,'$.a'))             VIRTUAL,
  registered_at INTEGER GENERATED ALWAYS AS (json_extract(data,'$.registered_at')) VIRTUAL
);

CREATE TABLE IF NOT EXISTS ai_predictions(
  game_id TEXT PRIMARY KEY,
  data    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS auth  (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta  (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

def _write_table(conn, key, data):
    """해당 도메인 테이블을 통째로 교체 (호출측에서 트랜잭션/락 관리)"""
    if key in _LIST_TABLES:
        conn.execute(f"DELETE FROM {key}")
        conn.executemany(
            f"INSERT INTO {key}(seq, data) VALUES(?, ?)",
            [(i, json.dumps(item, ensure_ascii=False)) for i, item in enumerate(data)])
    elif key in _DICT_TABLES:
        conn.execute(f"DELETE FROM {key}")
        conn.executemany(
            f"INSERT INTO {key}(game_id, data) VALUES(?, ?)",
            [(str(k), json.dumps(v, ensure_ascii=False)) for k, v in data.items()])
    elif key in _KV_TABLES:
        conn.execute(f"DELETE FROM {key}")
        conn.executemany(
            f"INSERT INTO {key}(key, value) VALUES(?, ?)",
            [(k, json.dumps(v, ensure_ascii=False)) for k, v in data.items()])
    else:
        raise ValueError(f"unknown storage key: {key}")

def _init_db():
    conn = _db_conn()
    try:
        # v2 documents 테이블(하위 호환·백업용) + v3 정규화 스키마
        conn.execute("CREATE TABLE IF NOT EXISTS documents (key TEXT PRIMARY KEY, data TEXT NOT NULL)")
        conn.executescript(_SCHEMA)
        # 1단계: 레거시 JSON 파일 → documents (DB에 없는 키만, 원본 보존)
        for path in (BETS_FILE, CONFIG_FILE, RESULTS_FILE, AUTH_FILE,
                     GAMES_FILE, AI_PREDICTIONS_FILE, FEEDBACK_FILE):
            key = _doc_key(path)
            if conn.execute("SELECT 1 FROM documents WHERE key=?", (key,)).fetchone():
                continue
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        raw = f.read()
                    json.loads(raw)  # 유효성 검증 후 저장
                    conn.execute("INSERT INTO documents(key, data) VALUES(?, ?)", (key, raw))
                except Exception:
                    pass
        # 2단계: documents → 정규화 테이블 (최초 1회만, meta 플래그로 멱등 보장)
        if not conn.execute("SELECT 1 FROM meta WHERE key='migrated_v3'").fetchone():
            for row in conn.execute("SELECT key, data FROM documents").fetchall():
                key, raw = row
                if key in _LIST_TABLES + _DICT_TABLES + _KV_TABLES:
                    try:
                        _write_table(conn, key, json.loads(raw))
                    except Exception:
                        pass
            conn.execute("INSERT INTO meta(key, value) VALUES('migrated_v3', ?)", (str(int(time.time())),))
        conn.commit()
    finally:
        conn.close()

def _sqlite_read_json(path, default):
    key = _doc_key(path)
    conn = _db_conn()
    try:
        if key in _LIST_TABLES:
            rows = conn.execute(f"SELECT data FROM {key} ORDER BY seq").fetchall()
            return [json.loads(r[0]) for r in rows] if rows else default
        if key in _DICT_TABLES:
            rows = conn.execute(f"SELECT game_id, data FROM {key}").fetchall()
            return {r[0]: json.loads(r[1]) for r in rows} if rows else default
        if key in _KV_TABLES:
            rows = conn.execute(f"SELECT key, value FROM {key}").fetchall()
            return {r[0]: json.loads(r[1]) for r in rows} if rows else default
        row = conn.execute("SELECT data FROM documents WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])
    finally:
        conn.close()

def _sqlite_write_json(path, data):
    key = _doc_key(path)
    with _db_lock:
        conn = _db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _write_table(conn, key, data)
            except ValueError:
                conn.execute(
                    "INSERT INTO documents(key, data) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                    (key, json.dumps(data, ensure_ascii=False)))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

_init_db()

# ── Postgres 어댑터 ──────────────────────────────────────────────
# DATABASE_URL이 있으면 Postgres 사용(무중단 배포·replicas 가능), 없으면 SQLite 폴백(로컬 개발).
# 스키마는 SQLite와 동일 설계: data(JSONB)가 원본, 조회 컬럼은 GENERATED ALWAYS AS ... STORED.
DATABASE_URL = os.environ.get("DATABASE_URL", "")

_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS games(
  seq        INTEGER PRIMARY KEY,
  data       JSONB NOT NULL,
  id         TEXT    GENERATED ALWAYS AS (data->>'id')                 STORED,
  stage      TEXT    GENERATED ALWAYS AS (data->>'stage')              STORED,
  grp        TEXT    GENERATED ALWAYS AS (data->>'group')              STORED,
  date       TEXT    GENERATED ALWAYS AS (data->>'date')               STORED,
  time       TEXT    GENERATED ALWAYS AS (data->>'time')               STORED,
  venue      TEXT    GENERATED ALWAYS AS (data->>'venue')              STORED,
  status     TEXT    GENERATED ALWAYS AS (data->>'status')             STORED,
  home_name  TEXT    GENERATED ALWAYS AS (data->'home'->>'name')       STORED,
  home_short TEXT    GENERATED ALWAYS AS (data->'home'->>'short')      STORED,
  away_name  TEXT    GENERATED ALWAYS AS (data->'away'->>'name')       STORED,
  away_short TEXT    GENERATED ALWAYS AS (data->'away'->>'short')      STORED,
  ended_at   BIGINT  GENERATED ALWAYS AS ((data->>'ended_at')::bigint) STORED,
  deleted    BOOLEAN GENERATED ALWAYS AS (coalesce((data->>'deleted')::boolean, false)) STORED
);
CREATE INDEX IF NOT EXISTS idx_games_id     ON games(id);
CREATE INDEX IF NOT EXISTS idx_games_status ON games(status);
CREATE INDEX IF NOT EXISTS idx_games_stage  ON games(stage);

CREATE TABLE IF NOT EXISTS bets(
  seq        INTEGER PRIMARY KEY,
  data       JSONB NOT NULL,
  id         TEXT    GENERATED ALWAYS AS (data->>'id')                   STORED,
  game_id    TEXT    GENERATED ALWAYS AS (data->>'game_id')              STORED,
  name       TEXT    GENERATED ALWAYS AS (data->>'name')                 STORED,
  h          INTEGER GENERATED ALWAYS AS ((data->>'h')::int)             STORED,
  a          INTEGER GENERATED ALWAYS AS ((data->>'a')::int)             STORED,
  amount     INTEGER GENERATED ALWAYS AS ((data->>'amount')::int)        STORED,
  paid       BOOLEAN GENERATED ALWAYS AS ((data->>'paid')::boolean)      STORED,
  paid_out   BOOLEAN GENERATED ALWAYS AS ((data->>'paid_out')::boolean)  STORED,
  created_at BIGINT  GENERATED ALWAYS AS ((data->>'created_at')::bigint) STORED
);
CREATE INDEX IF NOT EXISTS idx_bets_game_id ON bets(game_id);
CREATE INDEX IF NOT EXISTS idx_bets_name    ON bets(name);

CREATE TABLE IF NOT EXISTS feedback(
  seq        INTEGER PRIMARY KEY,
  data       JSONB NOT NULL,
  id         TEXT   GENERATED ALWAYS AS (data->>'id')                   STORED,
  name       TEXT   GENERATED ALWAYS AS (data->>'name')                 STORED,
  message    TEXT   GENERATED ALWAYS AS (data->>'message')              STORED,
  created_at BIGINT GENERATED ALWAYS AS ((data->>'created_at')::bigint) STORED
);
CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at);

CREATE TABLE IF NOT EXISTS results(
  game_id       TEXT PRIMARY KEY,
  data          JSONB NOT NULL,
  h             INTEGER GENERATED ALWAYS AS ((data->>'h')::int)             STORED,
  a             INTEGER GENERATED ALWAYS AS ((data->>'a')::int)             STORED,
  registered_at BIGINT  GENERATED ALWAYS AS ((data->>'registered_at')::bigint) STORED
);

CREATE TABLE IF NOT EXISTS ai_predictions(game_id TEXT PRIMARY KEY, data JSONB NOT NULL);
CREATE TABLE IF NOT EXISTS documents(key TEXT PRIMARY KEY, data JSONB NOT NULL);
CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value JSONB NOT NULL);
CREATE TABLE IF NOT EXISTS auth  (key TEXT PRIMARY KEY, value JSONB NOT NULL);
CREATE TABLE IF NOT EXISTS meta  (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

if DATABASE_URL:
    import psycopg
    from psycopg.types.json import Jsonb
    from psycopg_pool import ConnectionPool
    _pg_pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=5, open=True)

    def _pg_write_table(conn, key, data):
        cur = conn.cursor()
        if key in _LIST_TABLES:
            cur.execute(f"DELETE FROM {key}")
            cur.executemany(f"INSERT INTO {key}(seq, data) VALUES(%s, %s)",
                            [(i, Jsonb(item)) for i, item in enumerate(data)])
        elif key in _DICT_TABLES:
            cur.execute(f"DELETE FROM {key}")
            cur.executemany(f"INSERT INTO {key}(game_id, data) VALUES(%s, %s)",
                            [(str(k), Jsonb(v)) for k, v in data.items()])
        elif key in _KV_TABLES:
            cur.execute(f"DELETE FROM {key}")
            cur.executemany(f"INSERT INTO {key}(key, value) VALUES(%s, %s)",
                            [(k, Jsonb(v)) for k, v in data.items()])
        else:
            raise ValueError(f"unknown storage key: {key}")

    def read_json(path, default):
        key = _doc_key(path)
        with _pg_pool.connection() as conn:
            cur = conn.cursor()
            if key in _LIST_TABLES:
                rows = cur.execute(f"SELECT data FROM {key} ORDER BY seq").fetchall()
                return [r[0] for r in rows] if rows else default
            if key in _DICT_TABLES:
                rows = cur.execute(f"SELECT game_id, data FROM {key}").fetchall()
                return {r[0]: r[1] for r in rows} if rows else default
            if key in _KV_TABLES:
                rows = cur.execute(f"SELECT key, value FROM {key}").fetchall()
                return {r[0]: r[1] for r in rows} if rows else default
            row = cur.execute("SELECT data FROM documents WHERE key=%s", (key,)).fetchone()
            return default if row is None else row[0]

    def write_json(path, data):
        key = _doc_key(path)
        with _db_lock:
            with _pg_pool.connection() as conn:   # 컨텍스트 종료 시 commit, 예외 시 rollback
                try:
                    _pg_write_table(conn, key, data)
                except ValueError:
                    conn.execute(
                        "INSERT INTO documents(key, data) VALUES(%s, %s) "
                        "ON CONFLICT (key) DO UPDATE SET data=EXCLUDED.data",
                        (key, Jsonb(data)))

    def _init_pg():
        """스키마 생성 + SQLite → PG 자가 치유 이관.
        one-shot 플래그 대신 매 부팅마다 'PG 테이블이 비어 있고 SQLite에 데이터가 있으면 채움'.
        → 볼륨 미마운트 등으로 빈 이관이 발생해도 다음 부팅에서 자동 복구되고,
          데이터가 있는 PG 테이블은 절대 건드리지 않음. (볼륨 제거 후에는 자연히 no-op)"""
        # Railway 프라이빗 네트워크는 부팅 직후 몇 초간 준비되지 않을 수 있음 → 최대 60초 재시도
        deadline, last_err = time.time() + 60, None
        while time.time() < deadline:
            try:
                with _pg_pool.connection() as conn:
                    conn.execute("SELECT 1")
                last_err = None
                break
            except Exception as e:
                last_err = e
                print(f"[storage] Postgres 연결 대기 중... ({e})", flush=True)
                time.sleep(2)
        if last_err is not None:
            raise RuntimeError(f"Postgres 연결 실패(60초 초과): {last_err}")
        with _pg_pool.connection() as conn:
            conn.execute(_PG_SCHEMA)
            migrated = []
            for path in (BETS_FILE, CONFIG_FILE, RESULTS_FILE, AUTH_FILE,
                         GAMES_FILE, AI_PREDICTIONS_FILE, FEEDBACK_FILE):
                key = _doc_key(path)
                if conn.execute(f"SELECT 1 FROM {key} LIMIT 1").fetchone():
                    continue   # PG에 데이터 있음 → 보호
                data = _sqlite_read_json(path, None)
                if data:       # SQLite에 실데이터가 있을 때만 복사
                    _pg_write_table(conn, key, data)
                    migrated.append(key)
            if migrated:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('migrated_from_sqlite', %s) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                    (json.dumps({"at": int(time.time()), "tables": migrated}),))
            print(f"[storage] backend=postgres 준비 완료, 이관: {migrated or '없음(이미 데이터 있음/원본 없음)'}", flush=True)

    print("[storage] DATABASE_URL 감지 → Postgres 모드로 부팅", flush=True)
    _init_pg()
else:
    print("[storage] DATABASE_URL 없음 → SQLite 모드로 부팅", flush=True)
    read_json  = _sqlite_read_json
    write_json = _sqlite_write_json

def get_bets():           return read_json(BETS_FILE,           [])
def get_config():         return read_json(CONFIG_FILE,         {"bet_amount": 3000, "kp_link": "", "site_title": "토토", "carryover": 0})
def get_results():        return read_json(RESULTS_FILE,        {})
def get_auth():           return read_json(AUTH_FILE,           {"token": ""})
def get_games():          return read_json(GAMES_FILE,          [])
def get_ai_predictions(): return read_json(AI_PREDICTIONS_FILE, {})
def get_feedback():       return read_json(FEEDBACK_FILE,        [])

def active_game_ids():
    """삭제되지 않은 경기 id 집합 (문자열)"""
    return {str(g["id"]) for g in get_games() if not g.get("deleted")}

# ── 이월 판돈 자동 정산 ──────────────────────────────────────────
def _game_start_key(g):
    try:
        return datetime.strptime(f"{g.get('date','')} {g.get('time','')}", "%Y.%m.%d %H:%M")
    except Exception:
        return datetime.max

def _bet_hits(bet_type, bh, ba, rh, ra):
    """프론트(v2.html hitRes)와 동일한 당첨 판정. exact=정확 스코어, wdl=승무패 방향"""
    try:
        bh, ba, rh, ra = int(bh), int(ba), int(rh), int(ra)
    except (TypeError, ValueError):
        return False
    if bet_type == "wdl":
        sign = lambda x: (x > 0) - (x < 0)
        return sign(bh - ba) == sign(rh - ra)
    return bh == rh and ba == ra

def _settle_carryover(new_gids):
    """새로 등록된 결과에 대해 이월 판돈 자동 누적/소진.
    - 베팅이 있는 경기가 당첨자 없이 종료 → 그 경기 판돈을 carryover에 누적
    - 이월 대상 경기(결과 미등록 경기 중 시작시각 최빠름 = 프론트 carryTargetId와 동일 기준)가
      당첨자와 함께 종료 → carryover 소진(0)
    ※ 최초 결과 등록 시에만 호출할 것 (결과 정정 시 중복 정산 방지)
    """
    new_gids = [str(g) for g in new_gids]
    if not new_gids:
        return
    games   = [g for g in get_games() if not g.get("deleted")]
    results = get_results()
    bets    = get_bets()
    cfg     = get_config()
    co0 = co = int(cfg.get("carryover") or 0)
    by_id   = {str(g["id"]): g for g in games}
    decided = set(results.keys()) - set(new_gids)   # 이번 배치 이전에 이미 결정된 경기
    results_dirty = False
    for g in sorted((by_id[gid] for gid in new_gids if gid in by_id), key=_game_start_key):
        gid = str(g["id"])
        res = results.get(gid)
        if not res:
            decided.add(gid)
            continue
        gbets = [b for b in bets if str(b.get("game_id")) == gid]
        undecided = [x for x in games if str(x["id"]) not in decided]
        target = min(undecided, key=_game_start_key) if undecided else None
        is_target = target is not None and str(target["id"]) == gid
        winner = any(_bet_hits(g.get("bet_type", "exact"), b.get("h"), b.get("a"),
                               res.get("h"), res.get("a")) for b in gbets)
        if gbets and not winner:
            if is_target and co > 0:
                res["carryover_in"] = co     # 이 경기에 걸려 있던 이월 (무당첨 재이월 표시용)
                results_dirty = True
            co += sum(int(b.get("amount") or 0) for b in gbets)
        elif winner and is_target:
            if co > 0:
                res["carryover_used"] = co   # 종료 후에도 당첨금 표시에 이월 포함할 수 있도록 기록
                results_dirty = True
            co = 0
        decided.add(gid)
    if results_dirty:
        write_json(RESULTS_FILE, results)
    if co != co0:
        cfg["carryover"] = co
        write_json(CONFIG_FILE, cfg)

# 2026 FIFA 월드컵 경기장 → UTC 오프셋 (6월 기준, DST 적용)
_VENUE_UTC_MAP = [
    # 미국 동부 (EDT = UTC-4)
    ("MetLife", -4), ("New Jersey", -4), ("East Rutherford", -4),
    ("Gillette", -4), ("Foxborough", -4), ("Boston", -4),
    ("Hard Rock", -4), ("Miami", -4),
    ("Mercedes-Benz", -4), ("Atlanta", -4),
    ("Lincoln Financial", -4), ("Philadelphia", -4),
    # 캐나다 동부 (EDT = UTC-4)
    ("BMO", -4), ("Toronto", -4),
    # 미국 중부 (CDT = UTC-5)
    ("AT&T", -5), ("Arlington", -5), ("Dallas", -5),
    ("Arrowhead", -5), ("Kansas City", -5),
    ("NRG", -5), ("Houston", -5),
    # 미국 서부 (PDT = UTC-7)
    ("SoFi", -7), ("Inglewood", -7), ("Los Angeles", -7),
    ("Levi", -7), ("Santa Clara", -7), ("San Francisco", -7),
    ("Lumen", -7), ("Seattle", -7),
    ("BC Place", -7), ("Vancouver", -7),
    # 멕시코 (2022년 DST 폐지 — 연중 표준시, 세 도시 모두 UTC-6)
    ("Azteca", -6), ("Mexico City", -6), ("Ciudad de Mexico", -6),
    ("BBVA", -6), ("Monterrey", -6),
    ("Akron", -6), ("Guadalajara", -6),
]

# worldcup26.ir 스타디움 목록 (id → "이름 도시") — 경기 응답에 경기장 이름이 없고 stadium_id만 있어
# 이 맵으로 경기장(=시간대)을 찾는다. 24시간 캐시, 실패 시 기존 캐시 유지.
_STADIUM_CACHE = {"map": {}, "ts": 0.0}

async def _fetch_stadium_map(client) -> dict:
    if _STADIUM_CACHE["map"] and time.time() - _STADIUM_CACHE["ts"] < 86400:
        return _STADIUM_CACHE["map"]
    try:
        r = await client.get("https://worldcup26.ir/get/stadiums")
        r.raise_for_status()
        data = r.json()
        arr = data.get("stadiums", data if isinstance(data, list) else [])
        smap = {str(s.get("id", "")): f"{s.get('name_en', '')} {s.get('city_en', '')}".strip()
                for s in arr if s.get("id")}
        if smap:
            _STADIUM_CACHE["map"] = smap
            _STADIUM_CACHE["ts"] = time.time()
    except Exception:
        pass
    return _STADIUM_CACHE["map"]

def _venue_utc_offset(venue: str):
    """경기장 이름으로 UTC 오프셋 반환. 알 수 없으면 None."""
    v = venue.lower()
    for name, offset in _VENUE_UTC_MAP:
        if name.lower() in v:
            return offset
    return None

def local_to_kst(date_str: str, time_str: str, venue: str):
    """경기장 현지 시간 → KST. 반환: (date_str, time_str, 변환성공여부)"""
    offset = _venue_utc_offset(venue)
    if offset is None:
        return date_str, time_str, False
    try:
        dt = datetime.strptime(f"{date_str.replace('.', '-')} {time_str}", "%Y-%m-%d %H:%M")
        kst = dt + timedelta(hours=(9 - offset))
        return kst.strftime("%Y.%m.%d"), kst.strftime("%H:%M"), True
    except Exception:
        return date_str, time_str, False

def admin_required(x_admin_token: Optional[str] = Header(None)):
    auth   = get_auth()
    stored = auth.get("token", "")
    if stored and x_admin_token != stored:
        raise HTTPException(status_code=401, detail="관리자 권한이 필요합니다")
    return True

# ── 모델 ──────────────────────────────────────────────────────
class BetIn(BaseModel):
    game_id: int
    name:    str = Field(..., min_length=1, max_length=5)
    h:       int
    a:       int

class ConfigUpdate(BaseModel):
    bet_amount:    Optional[int] = None
    kp_link:       Optional[str] = None
    site_title:    Optional[str] = None
    carryover:     Optional[int] = None
    popup_enabled: Optional[bool] = None
    popup_message: Optional[str]  = None
    popup_publish_at: Optional[str] = None   # "2026-06-25T10:00" (KST 기준)

class ResultIn(BaseModel):
    h: int
    a: int

class FeedbackIn(BaseModel):
    # 보안: 길이 20자 제한 + 특수문자 차단(한글/영문/숫자/공백만 허용). 이름 필수.
    message: str = Field(..., min_length=1, max_length=100)
    name:    str = Field(..., min_length=1, max_length=5)

class PaymentUpdate(BaseModel):
    paid: bool

class PayoutUpdate(BaseModel):
    paid_out: bool

class TokenUpdate(BaseModel):
    new_token:     str
    current_token: Optional[str] = ""

class CommentIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=200)

class GameIn(BaseModel):
    id:        Optional[int]  = None   # 미지정시 자동 부여
    group:     Optional[str] = ""      # 불필요, 하위호환용
    home_name: str
    home_short:str
    home_flag: str
    away_name: str
    away_short:str
    away_flag: str
    date:      str                     # "2026.06.15"
    time:      str                     # "21:00"
    venue:     Optional[str] = ""
    status:    Optional[str] = "pending"  # pending | open | closed
    bet_type:  Optional[str] = "exact"   # exact | wdl
    deleted:   Optional[bool] = False    # 소프트 삭제 여부

# ══════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════

@app.get("/api/config")
def public_config():
    cfg = get_config()
    return {"bet_amount": cfg.get("bet_amount", 3000), "kp_link": cfg.get("kp_link", ""), "site_title": cfg.get("site_title", "토토"), "carryover": cfg.get("carryover", 0),
            "popup_enabled": cfg.get("popup_enabled", False), "popup_message": cfg.get("popup_message", ""), "popup_publish_at": cfg.get("popup_publish_at", "")}

@app.get("/api/games")
def list_games(include_deleted: bool = False):
    games = get_games()
    if not include_deleted:
        games = [g for g in games if not g.get("deleted")]
    return games

@app.post("/api/bets")
def submit_bet(bet: BetIn):
    if not re.match(r'^[가-힣ㄱ-ㅎㅏ-ㅣa-zA-Z0-9]+$', bet.name):
        raise HTTPException(status_code=400, detail="이름은 한글·영문·숫자만 사용 가능합니다")
    games = get_games()
    if not any(str(g["id"]) == str(bet.game_id) for g in games):
        raise HTTPException(status_code=404, detail="게임을 찾을 수 없습니다")
    bets  = get_bets()
    dup = next((b for b in bets
                if str(b["game_id"]) == str(bet.game_id)
                and b["name"] == bet.name
                and b["h"] == bet.h
                and b["a"] == bet.a), None)
    if dup:
        raise HTTPException(status_code=409, detail="동일한 경기에 같은 이름·같은 득점으로 이미 베팅하셨습니다")
    entry = {
        "id":         int(time.time() * 1000),
        "game_id":    bet.game_id,
        "name":       bet.name,
        "h":          bet.h,
        "a":          bet.a,
        "amount":     get_config().get("bet_amount", 3000),
        "paid":       False,
        "paid_out":   False,
        "created_at": int(time.time()),
    }
    bets.append(entry)
    write_json(BETS_FILE, bets)
    return {"ok": True, "bet_id": entry["id"]}

@app.get("/api/bets")
def list_bets(game_id: Optional[int] = None):
    bets = get_bets()
    active = active_game_ids()
    bets = [b for b in bets if str(b["game_id"]) in active]   # 삭제된 경기의 베팅 숨김(FK)
    if game_id is not None:
        bets = [b for b in bets if b["game_id"] == game_id]
    return bets

@app.get("/api/results")
def list_results():
    return get_results()

@app.get("/api/ai-predictions")
def list_ai_predictions():
    return get_ai_predictions()

@app.get("/api/auth/status")
def auth_status():
    return {"has_token": bool(get_auth().get("token", ""))}

_FB_ALLOWED = re.compile(r"^[가-힣ㄱ-ㅎㅏ-ㅣa-zA-Z0-9 ]*$")  # 한글/영문/숫자/공백만

@app.post("/api/feedback")
def submit_feedback(fb: FeedbackIn):
    # 보안: 특수문자 차단 + 20자 제한. 원문 저장(JSON 인젝션 안전), 출력 시 escapeHtml(XSS 방어)
    msg  = fb.message.strip()[:100]
    name = (fb.name or "").strip()[:20]
    if not msg:
        raise HTTPException(400, "내용을 입력해주세요")
    if not name:
        raise HTTPException(400, "이름을 입력해주세요")
    if not _FB_ALLOWED.match(msg) or not _FB_ALLOWED.match(name):
        raise HTTPException(400, "특수문자는 사용할 수 없습니다 (한글/영문/숫자만)")
    items = get_feedback()
    # 보안: 저장 개수 상한 (스토리지 남용 방지)
    if len(items) >= 5000:
        items = items[-4999:]
    entry = {
        "id":         int(time.time() * 1000),
        "name":       name,
        "message":    msg,
        "created_at": int(time.time()),
    }
    items.append(entry)
    write_json(FEEDBACK_FILE, items)
    return {"ok": True}

@app.get("/api/feedback")
def list_feedback():
    # 공개 목록: 최신순, 최근 200건
    items = sorted(get_feedback(), key=lambda x: x.get("created_at", 0), reverse=True)
    return items[:200]

# ══════════════════════════════════════════════════════════════
# ADMIN API
# ══════════════════════════════════════════════════════════════

@app.get("/api/admin/config")
def admin_get_config(auth=Depends(admin_required)):
    return get_config()

@app.get("/api/admin/feedback")
def admin_list_feedback(auth=Depends(admin_required)):
    # 최신순
    return sorted(get_feedback(), key=lambda x: x.get("created_at", 0), reverse=True)

@app.delete("/api/admin/feedback/all")
def admin_delete_all_feedback(auth=Depends(admin_required)):
    write_json(FEEDBACK_FILE, [])
    return {"ok": True}

@app.delete("/api/admin/feedback/{fb_id}")
def admin_delete_feedback(fb_id: int, auth=Depends(admin_required)):
    items = [f for f in get_feedback() if f.get("id") != fb_id]
    write_json(FEEDBACK_FILE, items)
    return {"ok": True}

@app.post("/api/admin/feedback/{fb_id}/comments")
def admin_add_feedback_comment(fb_id: int, body: CommentIn, auth=Depends(admin_required)):
    items = get_feedback()
    for item in items:
        if item.get("id") == fb_id:
            if "comments" not in item:
                item["comments"] = []
            comment = {
                "id": int(time.time() * 1000),
                "text": body.text.strip()[:200],
                "created_at": int(time.time()),
            }
            item["comments"].append(comment)
            write_json(FEEDBACK_FILE, items)
            return {"ok": True, "comment": comment}
    raise HTTPException(404, "피드백을 찾을 수 없습니다")

@app.patch("/api/admin/feedback/{fb_id}/comments/{comment_id}")
def admin_edit_feedback_comment(fb_id: int, comment_id: int, body: CommentIn, auth=Depends(admin_required)):
    items = get_feedback()
    for item in items:
        if item.get("id") == fb_id:
            for comment in item.get("comments", []):
                if comment.get("id") == comment_id:
                    comment["text"] = body.text.strip()[:200]
                    write_json(FEEDBACK_FILE, items)
                    return {"ok": True}
            raise HTTPException(404, "댓글을 찾을 수 없습니다")
    raise HTTPException(404, "피드백을 찾을 수 없습니다")

@app.delete("/api/admin/feedback/{fb_id}/comments/{comment_id}")
def admin_delete_feedback_comment(fb_id: int, comment_id: int, auth=Depends(admin_required)):
    items = get_feedback()
    for item in items:
        if item.get("id") == fb_id:
            item["comments"] = [c for c in item.get("comments", []) if c.get("id") != comment_id]
            write_json(FEEDBACK_FILE, items)
            return {"ok": True}
    raise HTTPException(404, "피드백을 찾을 수 없습니다")

@app.patch("/api/admin/config")
def admin_update_config(body: ConfigUpdate, auth=Depends(admin_required)):
    cfg = get_config()
    if body.bet_amount  is not None: cfg["bet_amount"]  = body.bet_amount
    if body.kp_link     is not None: cfg["kp_link"]     = body.kp_link
    if body.site_title  is not None: cfg["site_title"]  = body.site_title
    if body.carryover   is not None: cfg["carryover"]   = body.carryover
    if body.popup_enabled    is not None: cfg["popup_enabled"]    = body.popup_enabled
    if body.popup_message    is not None: cfg["popup_message"]    = body.popup_message[:500]
    if body.popup_publish_at is not None: cfg["popup_publish_at"] = body.popup_publish_at
    write_json(CONFIG_FILE, cfg)
    return cfg

@app.get("/api/admin/bets")
def admin_list_bets(game_id: Optional[int] = None, auth=Depends(admin_required)):
    bets = get_bets()
    active = active_game_ids()
    bets = [b for b in bets if str(b["game_id"]) in active]   # 삭제된 경기의 베팅 숨김(FK)
    if game_id is not None:
        bets = [b for b in bets if b["game_id"] == game_id]
    return bets

@app.patch("/api/admin/bets/{bet_id}/payment")
def admin_update_payment(bet_id: int, body: PaymentUpdate, auth=Depends(admin_required)):
    bets = get_bets()
    for b in bets:
        if b["id"] == bet_id:
            b["paid"] = body.paid
            write_json(BETS_FILE, bets)
            return b
    raise HTTPException(404, "베팅을 찾을 수 없습니다")

@app.patch("/api/admin/bets/{bet_id}/payout")
def admin_update_payout(bet_id: int, body: PayoutUpdate, auth=Depends(admin_required)):
    bets = get_bets()
    for b in bets:
        if b["id"] == bet_id:
            b["paid_out"] = body.paid_out
            write_json(BETS_FILE, bets)
            return b
    raise HTTPException(404, "베팅을 찾을 수 없습니다")

@app.delete("/api/admin/bets/all")
def admin_delete_all_bets(auth=Depends(admin_required)):
    write_json(BETS_FILE, [])
    return {"ok": True}

@app.delete("/api/admin/bets/{bet_id}")
def admin_delete_bet(bet_id: int, auth=Depends(admin_required)):
    bets = [b for b in get_bets() if b["id"] != bet_id]
    write_json(BETS_FILE, bets)
    return {"ok": True}

@app.put("/api/admin/results/{game_id}")
def admin_set_result(game_id: int, body: ResultIn, auth=Depends(admin_required)):
    results = get_results()
    is_new = str(game_id) not in results   # 정정(재등록)은 이월 정산 제외
    results[str(game_id)] = {"h": body.h, "a": body.a, "registered_at": int(time.time())}
    write_json(RESULTS_FILE, results)
    if is_new:
        _settle_carryover([game_id])
    return results[str(game_id)]

@app.post("/api/admin/ai-predict")
async def generate_ai_predictions(auth=Depends(admin_required)):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(500, "ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다")

    try:
        import anthropic as _anthropic
    except ImportError:
        raise HTTPException(500, "anthropic 패키지가 설치되지 않았습니다")

    games = get_games()
    results = get_results()
    # 결과가 이미 나온(지난) 경기는 예측 불필요 → 제외
    target_games = [
        g for g in games
        if g.get("status") in ("open", "closed") and str(g["id"]) not in results
    ]
    predictions = get_ai_predictions()
    client = _anthropic.Anthropic(api_key=api_key)
    generated = 0
    errors = []

    for game in target_games:
        game_id = str(game["id"])
        home = game["home"]["name"]
        away = game["away"]["name"]
        date = game.get("date", "")
        try:
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": (
                        f"FIFA 월드컵 2026 경기: {home} vs {away} ({date})\n"
                        "두 팀의 최근 전력, FIFA 랭킹, 역대 전적을 고려하여 예상 스코어를 분석해줘.\n"
                        "reason은 30자 이내의 완결된 한 문장으로 작성해줘 (문장이 잘리지 않게).\n"
                        "반드시 아래 JSON 형식으로만 답해줘:\n"
                        '{"home_score": 숫자, "away_score": 숫자, "reason": "예측 근거 한 문장"}'
                    )
                }]
            )
            text = msg.content[0].text.strip()
            # JSON 블록 추출
            m = re.search(r'\{[^}]+\}', text, re.DOTALL)
            if not m:
                raise ValueError("JSON 없음")
            pred = json.loads(m.group())
            predictions[game_id] = {
                "home_score": int(pred["home_score"]),
                "away_score": int(pred["away_score"]),
                "reason":     str(pred.get("reason", ""))[:80],
                "generated_at": int(time.time()),
            }
            generated += 1
        except Exception as e:
            errors.append({"game_id": game_id, "error": str(e)})

    write_json(AI_PREDICTIONS_FILE, predictions)
    return {"ok": True, "generated": generated, "errors": errors, "predictions": predictions}

@app.delete("/api/admin/results/{game_id}")
def admin_delete_result(game_id: int, auth=Depends(admin_required)):
    results = get_results()
    results.pop(str(game_id), None)
    write_json(RESULTS_FILE, results)
    return {"ok": True}

# ── 게임(경기) 관리 ───────────────────────────────────────────

@app.post("/api/admin/games")
def admin_add_game(body: GameIn, auth=Depends(admin_required)):
    games  = get_games()
    try:
        max_id = max((int(g["id"]) for g in games), default=0)
    except (ValueError, TypeError):
        max_id = len(games)
    new_id = body.id if body.id else (max_id + 1)
    if any(g["id"] == new_id for g in games):
        raise HTTPException(400, f"id={new_id} 는 이미 존재합니다")
    game = {
        "id":    new_id,
        "group": body.group,
        "home":  {"name": body.home_name, "short": body.home_short, "flag": body.home_flag},
        "away":  {"name": body.away_name, "short": body.away_short, "flag": body.away_flag},
        "date":  body.date,
        "time":  body.time,
        "venue": body.venue,
        "status":   body.status   or "pending",
        "bet_type": body.bet_type or "exact",
    }
    games.append(game)
    write_json(GAMES_FILE, games)
    return game

@app.patch("/api/admin/games/{game_id}")
def admin_update_game(game_id: int, body: GameIn, auth=Depends(admin_required)):
    # merge 방식: 지정된 필드만 갱신하고 나머지(stage/home_label/away_label/ended_at/deleted 등)는 보존.
    # 재조립(dict 새로 생성)하면 브래킷 트리 필수 필드가 날아가 전체 브래킷이 깨진다.
    games = get_games()
    for g in games:
        if str(g["id"]) == str(game_id):
            g.update({
                "group": body.group or g.get("group", ""),
                "home":  {"name": body.home_name or g["home"]["name"],
                          "short": body.home_short or g["home"]["short"],
                          "flag":  body.home_flag  or g["home"]["flag"]},
                "away":  {"name": body.away_name or g["away"]["name"],
                          "short": body.away_short or g["away"]["short"],
                          "flag":  body.away_flag  or g["away"]["flag"]},
                "date":   body.date   or g["date"],
                "time":   body.time   or g["time"],
                "venue":  body.venue  or g.get("venue", ""),
                "status":   body.status   or g["status"],
                "bet_type": body.bet_type or g.get("bet_type", "exact"),
            })
            write_json(GAMES_FILE, games)
            return g
    raise HTTPException(404, "게임을 찾을 수 없습니다")

@app.patch("/api/admin/games/{game_id}/datetime")
def admin_update_game_datetime(game_id: int, date: str, time: str, auth=Depends(admin_required)):
    """경기 날짜/시간만 수정."""
    games = get_games()
    for g in games:
        if str(g["id"]) == str(game_id):
            g["date"] = date
            g["time"] = time
            g.pop("kst", None)
            g.pop("kst_v2", None)
            write_json(GAMES_FILE, games)
            return {"ok": True}
    raise HTTPException(404, "게임을 찾을 수 없습니다")

class TeamSide(BaseModel):
    flag: str = ""
    short: str = ""
    name: str = ""

class TeamUpdateRequest(BaseModel):
    home: TeamSide
    away: TeamSide

@app.patch("/api/admin/games/{game_id}/teams")
def admin_update_game_teams(game_id: int, req: TeamUpdateRequest, auth=Depends(admin_required)):
    games = get_games()
    for g in games:
        if str(g["id"]) == str(game_id):
            if req.home.name:
                g["home"] = {"flag": req.home.flag, "short": req.home.short, "name": req.home.name}
            if req.away.name:
                g["away"] = {"flag": req.away.flag, "short": req.away.short, "name": req.away.name}
            write_json(GAMES_FILE, games)
            return g
    raise HTTPException(404, "게임을 찾을 수 없습니다")

@app.patch("/api/admin/games/{game_id}/status")
def admin_set_game_status(game_id: int, status: str, auth=Depends(admin_required)):
    games = get_games()
    for g in games:
        if str(g["id"]) == str(game_id):
            g["status"] = status
            if status == "ended":
                g.setdefault("ended_at", int(time.time()))
            write_json(GAMES_FILE, games)
            return g
    raise HTTPException(404, "게임을 찾을 수 없습니다")

@app.post("/api/admin/games/fix-kst")
def admin_fix_kst(auth=Depends(admin_required)):
    """경기장 시간대 기반으로 현지 시간 → KST 정확 변환 (kst_v2 미처리 경기만)."""
    games = get_games()
    fixed = 0
    skipped = 0
    unknown_venues = []
    for g in games:
        if g.get("kst_v2"):
            continue
        venue = g.get("venue", "")
        date_str, time_str, ok = local_to_kst(g["date"], g["time"], venue)
        if ok:
            g["date"] = date_str
            g["time"] = time_str
            g["kst_v2"] = True
            fixed += 1
        else:
            skipped += 1
            if venue and venue not in unknown_venues:
                unknown_venues.append(venue)
    write_json(GAMES_FILE, games)
    empty_count = sum(1 for g in games if not g.get("kst_v2") and not g.get("venue", ""))
    return {"ok": True, "fixed": fixed, "skipped": skipped, "total": len(games),
            "unknown_venues": unknown_venues, "empty_venue_count": empty_count}

@app.post("/api/admin/games/fix-kst-manual")
def admin_fix_kst_manual(hours: int, auth=Depends(admin_required)):
    """venue 정보 없는 경기에 수동으로 시간차(hours)를 적용하여 KST 변환."""
    if not (-24 <= hours <= 24):
        raise HTTPException(400, "hours는 -24~24 사이여야 합니다")
    games = get_games()
    fixed = 0
    for g in games:
        if g.get("kst_v2"):
            continue
        try:
            dt = datetime.strptime(f"{g['date'].replace('.', '-')} {g['time']}", "%Y-%m-%d %H:%M")
            kst = dt + timedelta(hours=hours)
            g["date"] = kst.strftime("%Y.%m.%d")
            g["time"] = kst.strftime("%H:%M")
            g["kst_v2"] = True
            fixed += 1
        except Exception:
            pass
    write_json(GAMES_FILE, games)
    return {"ok": True, "fixed": fixed, "total": len(games)}

@app.post("/api/admin/games/revert-kst")
def admin_revert_kst(auth=Depends(admin_required)):
    """잘못 적용된 +9h KST 변환을 되돌립니다 (kst:true 플래그가 있는 게임만)."""
    games = get_games()
    reverted = 0
    for g in games:
        if not g.get("kst"):
            continue
        try:
            dt = datetime.strptime(f"{g['date'].replace('.', '-')} {g['time']}", "%Y-%m-%d %H:%M")
            original = dt - timedelta(hours=9)
            g["date"] = original.strftime("%Y.%m.%d")
            g["time"] = original.strftime("%H:%M")
            g.pop("kst", None)
            reverted += 1
        except Exception:
            g.pop("kst", None)
    write_json(GAMES_FILE, games)
    return {"ok": True, "reverted": reverted, "total": len(games)}

@app.delete("/api/admin/games/all")
def admin_delete_all_games(auth=Depends(admin_required)):
    # 소프트 삭제: 전체 경기를 deleted 처리 (베팅·결과는 보존, 복구 가능)
    games = get_games()
    for g in games:
        g["deleted"] = True
    write_json(GAMES_FILE, games)
    return {"ok": True}

@app.delete("/api/admin/games/{game_id}")
def admin_delete_game(game_id: int, auth=Depends(admin_required)):
    # 소프트 삭제: deleted=True 로 표시 (연결된 베팅은 자동 숨김, 복구 가능)
    games = get_games()
    found = False
    for g in games:
        if str(g["id"]) == str(game_id):
            g["deleted"] = True
            found = True
    if not found:
        raise HTTPException(404, "게임을 찾을 수 없습니다")
    write_json(GAMES_FILE, games)
    return {"ok": True}

@app.post("/api/admin/games/{game_id}/restore")
def admin_restore_game(game_id: int, auth=Depends(admin_required)):
    # 소프트 삭제 복구
    games = get_games()
    found = False
    for g in games:
        if str(g["id"]) == str(game_id):
            g["deleted"] = False
            found = True
    if not found:
        raise HTTPException(404, "게임을 찾을 수 없습니다")
    write_json(GAMES_FILE, games)
    return {"ok": True}

@app.delete("/api/admin/games/{game_id}/permanent")
def admin_purge_game(game_id: int, auth=Depends(admin_required)):
    # 영구 삭제: 경기 + 결과 + 연결된 베팅 모두 제거 (cascade)
    games = [g for g in get_games() if str(g["id"]) != str(game_id)]
    write_json(GAMES_FILE, games)
    results = get_results()
    results.pop(str(game_id), None)
    write_json(RESULTS_FILE, results)
    bets = [b for b in get_bets() if str(b["game_id"]) != str(game_id)]
    write_json(BETS_FILE, bets)
    return {"ok": True}

# ── 외부 API에서 경기 가져오기 (worldcup26.ir — 무료·키 불필요) ──
async def _fetch_and_parse_games(korea_only: bool):
    """worldcup26.ir 에서 경기를 가져와 파싱한 목록 반환 (저장하지 않음).
    반환: [{"game": {...}, "result": {"h":..,"a":..}|None}, ...]"""
    CODE_MAP = {
        "South Korea":"KOR","Korea Republic":"KOR","Mexico":"MEX","South Africa":"RSA",
        "United States":"USA","USA":"USA","Canada":"CAN","Argentina":"ARG",
        "Brazil":"BRA","Germany":"GER","France":"FRA","England":"ENG",
        "Spain":"ESP","Portugal":"POR","Japan":"JPN","Morocco":"MAR",
        "Netherlands":"NED","Poland":"POL","Czech Republic":"CZE","Belgium":"BEL",
        "Croatia":"CRO","Serbia":"SRB","Switzerland":"SUI","Uruguay":"URU",
        "Colombia":"COL","Ecuador":"ECU","Peru":"PER","Chile":"CHI",
        "Venezuela":"VEN","Paraguay":"PAR","Bolivia":"BOL","Senegal":"SEN",
        "Nigeria":"NGA","Cameroon":"CMR","Ghana":"GHA","Egypt":"EGY",
        "Algeria":"ALG","Tunisia":"TUN","Mali":"MLI","Saudi Arabia":"KSA",
        "Iran":"IRN","Qatar":"QAT","New Zealand":"NZL","Indonesia":"IDN",
        "Denmark":"DEN","Sweden":"SWE","Norway":"NOR","Austria":"AUT",
        "Italy":"ITA","Greece":"GRE","Turkey":"TUR","Ukraine":"UKR",
        "Romania":"ROU","Slovakia":"SVK","Hungary":"HUN","Albania":"ALB",
        "Georgia":"GEO","Slovenia":"SVN","Kosovo":"KVX","Wales":"WAL",
        "Scotland":"SCO","Northern Ireland":"NIR","Ireland":"IRL",
        "Costa Rica":"CRC","Honduras":"HON","Jamaica":"JAM","Panama":"PAN",
        "El Salvador":"SLV","Guatemala":"GUA","Cuba":"CUB","Haiti":"HAI",
        "Trinidad and Tobago":"TRI","Australia":"AUS","New Caledonia":"NCL",
        "Uzbekistan":"UZB","Iraq":"IRQ","Oman":"OMA","Bahrain":"BHR",
    }
    FLAG_MAP = {
        "Mexico":"🇲🇽","South Africa":"🇿🇦","South Korea":"🇰🇷","Korea Republic":"🇰🇷",
        "Czech Republic":"🇨🇿","Poland":"🇵🇱","Netherlands":"🇳🇱","Argentina":"🇦🇷",
        "Australia":"🇦🇺","USA":"🇺🇸","United States":"🇺🇸","Canada":"🇨🇦",
        "Brazil":"🇧🇷","Germany":"🇩🇪","France":"🇫🇷","England":"🏴󠁧󠁢󠁥󠁮󠁧󠁿",
        "Spain":"🇪🇸","Portugal":"🇵🇹","Japan":"🇯🇵","Morocco":"🇲🇦",
        "Belgium":"🇧🇪","Croatia":"🇭🇷","Serbia":"🇷🇸","Switzerland":"🇨🇭",
        "Uruguay":"🇺🇾","Colombia":"🇨🇴","Ecuador":"🇪🇨","Peru":"🇵🇪",
        "Chile":"🇨🇱","Venezuela":"🇻🇪","Paraguay":"🇵🇾","Bolivia":"🇧🇴",
        "Senegal":"🇸🇳","Nigeria":"🇳🇬","Cameroon":"🇨🇲","Ghana":"🇬🇭",
        "Egypt":"🇪🇬","Algeria":"🇩🇿","Tunisia":"🇹🇳","Mali":"🇲🇱",
        "Saudi Arabia":"🇸🇦","Iran":"🇮🇷","Japan":"🇯🇵","Qatar":"🇶🇦",
        "Australia":"🇦🇺","New Zealand":"🇳🇿","Indonesia":"🇮🇩",
        "Denmark":"🇩🇰","Sweden":"🇸🇪","Norway":"🇳🇴","Austria":"🇦🇹",
        "Italy":"🇮🇹","Greece":"🇬🇷","Turkey":"🇹🇷","Ukraine":"🇺🇦",
        "Romania":"🇷🇴","Slovakia":"🇸🇰","Hungary":"🇭🇺","Albania":"🇦🇱",
        "Georgia":"🇬🇪","Slovenia":"🇸🇮","Kosovo":"🇽🇰","Wales":"🏴󠁧󠁢󠁷󠁬󠁳󠁿",
        "Scotland":"🏴󠁧󠁢󠁳󠁣󠁴󠁿","Northern Ireland":"🇬🇧","Ireland":"🇮🇪",
        "Costa Rica":"🇨🇷","Honduras":"🇭🇳","Jamaica":"🇯🇲","Panama":"🇵🇦",
        "El Salvador":"🇸🇻","Guatemala":"🇬🇹","Cuba":"🇨🇺","Haiti":"🇭🇹",
        "Trinidad and Tobago":"🇹🇹",
    }
    try:
        async with httpx.AsyncClient(timeout=40) as client:  # 외부 API 응답이 20초+ 걸림
            r = await client.get("https://worldcup26.ir/get/games")
            r.raise_for_status()
            data = r.json()
            stadium_map = await _fetch_stadium_map(client)   # stadium_id → 경기장(시간대 판별용)
    except Exception as e:
        raise HTTPException(502, f"외부 API 오류: {e}")

    raw_matches = data if isinstance(data, list) else data.get("games", data.get("matches", []))
    parsed = []

    for m in raw_matches:
        ext_id = str(m.get("id") or m.get("match_number") or m.get("matchNumber") or "")
        if not ext_id:
            continue
        if korea_only:
            h = str(m.get("home_team_name_en", ""))
            a = str(m.get("away_team_name_en", ""))
            if "Korea" not in h and "Korea" not in a:
                continue

        # worldcup26.ir 필드: home_team_name_en, away_team_name_en, local_date
        h_name = m.get("home_team_name_en") or ""
        a_name = m.get("away_team_name_en") or ""

        # 일반 구조 fallback
        if not h_name:
            home = m.get("home_team") or m.get("homeTeam") or m.get("team1") or {}
            h_name = (home.get("name") or home.get("code") or "") if isinstance(home, dict) else str(home)
        if not a_name:
            away = m.get("away_team") or m.get("awayTeam") or m.get("team2") or {}
            a_name = (away.get("name") or away.get("code") or "") if isinstance(away, dict) else str(away)

        h_name = str(h_name)
        a_name = str(a_name)
        h_code = CODE_MAP.get(h_name, h_name[:3].upper())
        a_code = CODE_MAP.get(a_name, a_name[:3].upper())

        # local_date: "06/11/2026 13:00" — 경기장 현지 시간(venue local time)
        raw_dt = m.get("local_date") or m.get("datetime") or m.get("kickoff_utc") or m.get("date", "")
        raw_dt = str(raw_dt)
        if "/" in raw_dt:  # MM/DD/YYYY HH:MM
            parts = raw_dt.split(" ")
            d_parts = parts[0].split("/")
            date_str = f"{d_parts[2]}.{d_parts[0]}.{d_parts[1]}" if len(d_parts) == 3 else parts[0]
            time_str = parts[1][:5] if len(parts) > 1 else ""
        elif "-" in raw_dt:  # YYYY-MM-DD
            date_str = raw_dt[:10].replace("-", ".")
            time_str = raw_dt[11:16] if len(raw_dt) > 10 else ""
        else:
            date_str = raw_dt
            time_str = ""

        venue_raw = str(m.get("stadium") or m.get("venue") or m.get("ground") or "")
        if not venue_raw:   # worldcup26은 stadium_id만 제공 → 스타디움 맵으로 경기장 결정
            venue_raw = stadium_map.get(str(m.get("stadium_id") or ""), "")
        date_str, time_str, converted = local_to_kst(date_str, time_str, venue_raw)   # 현지 → KST 자동 변환

        group_raw = str(m.get("group", "") or "")
        group_str = f"{group_raw}조" if group_raw and not group_raw.endswith("조") else group_raw

        # 단계(라운드) + 대진 연결 라벨 (브래킷용)
        type_raw = str(m.get("type", "") or "").lower()
        STAGE_MAP = {"r32": "R32", "r16": "R16", "qf": "QF", "sf": "SF",
                     "final": "F", "third": "3RD", "3rd": "3RD", "third_place": "3RD"}
        stage = STAGE_MAP.get(type_raw, "GS")
        home_label = str(m.get("home_team_label", "") or "")
        away_label = str(m.get("away_team_label", "") or "")

        finished = str(m.get("finished", "")).upper() == "TRUE"
        h_score = m.get("home_score")
        a_score = m.get("away_score")
        has_score = finished and h_score is not None and a_score is not None
        try:
            h_score_int = int(h_score)
            a_score_int = int(a_score)
        except (TypeError, ValueError):
            has_score = False

        game = {
            "id":    ext_id,
            "group": group_str,
            "stage": stage,
            "home_label": home_label,
            "away_label": away_label,
            "home":  {"name": h_name, "short": h_code, "flag": FLAG_MAP.get(h_name, "🏳️")},
            "away":  {"name": a_name, "short": a_code, "flag": FLAG_MAP.get(a_name, "🏳️")},
            "date":  date_str,
            "time":  time_str,
            "venue": venue_raw,
            "status": "closed" if has_score else "pending",
            "kst_v2": converted,
        }
        result = {"h": h_score_int, "a": a_score_int} if has_score else None
        parsed.append({"game": game, "result": result})

    return parsed


class ImportConfirm(BaseModel):
    ids: list[str]


@app.get("/api/admin/games/import/preview")
async def admin_import_preview(korea_only: bool = False, auth=Depends(admin_required)):
    """가져올 경기 목록 미리보기 (저장하지 않음). 이미 등록된 경기는 exists=True."""
    parsed = await _fetch_and_parse_games(korea_only)
    existing_ids = {str(g["id"]) for g in get_games()}
    games = []
    for p in parsed:
        g = p["game"]
        games.append({**g, "result": p["result"], "exists": str(g["id"]) in existing_ids})
    return {"games": games}


@app.post("/api/admin/games/import/confirm")
async def admin_import_confirm(body: ImportConfirm, korea_only: bool = False, auth=Depends(admin_required)):
    """선택된 경기 id 만 등록."""
    parsed = await _fetch_and_parse_games(korea_only)
    sel = {str(i) for i in body.ids}
    existing = get_games()
    existing_ids = {str(g["id"]) for g in existing}
    results = get_results()
    added = 0
    for p in parsed:
        g = p["game"]; gid = str(g["id"])
        if gid not in sel or gid in existing_ids:
            continue
        existing.append(g)
        existing_ids.add(gid)
        if p["result"]:
            results[gid] = {**p["result"], "registered_at": int(time.time())}
        added += 1
    write_json(GAMES_FILE, existing)
    write_json(RESULTS_FILE, results)
    return {"ok": True, "added": added, "total": len(existing)}

def _apply_enrich(games: list, parsed: list) -> tuple[int, dict]:
    """enrich-bracket 공통 로직. (updated 수, 신규 결과 패치 dict) 반환."""
    by_id        = {str(p["game"]["id"]): p["game"]   for p in parsed}
    by_id_result = {str(p["game"]["id"]): p["result"] for p in parsed if p["result"]}
    results_patch = {}
    updated = 0
    for g in games:
        src = by_id.get(str(g["id"]))
        if not src:
            continue
        g["stage"]      = src.get("stage", "GS")
        g["home_label"] = src.get("home_label", "")
        g["away_label"] = src.get("away_label", "")
        # API에 팀명이 있을 때만 갱신 (TBD/빈값이면 기존 데이터 보존)
        if src.get("home") and src["home"].get("name"):
            g["home"] = src["home"]
        if src.get("away") and src["away"].get("name"):
            g["away"] = src["away"]
        # 경기 일시 KST 자동 동기화: 소스가 경기장 시간대 변환에 성공한 경우에만 반영
        if src.get("kst_v2"):
            if src.get("date"): g["date"] = src["date"]
            if src.get("time"): g["time"] = src["time"]
            if src.get("venue"): g["venue"] = src["venue"]
            g["kst_v2"] = True
        # 종료된 경기 스코어 패치 + 상태 자동 종료 처리
        gid = str(g["id"])
        if gid in by_id_result:
            results_patch[gid] = by_id_result[gid]
            if g.get("status") != "ended":
                g["status"] = "ended"
                g.setdefault("ended_at", int(time.time()))
        updated += 1
    return updated, results_patch


@app.post("/api/admin/games/enrich-bracket")
async def admin_enrich_bracket(auth=Depends(admin_required)):
    """기존 등록 경기에 브래킷 메타데이터(stage·대진 라벨·확정 팀·스코어)를 채움. 베팅/상태는 보존."""
    parsed = await _fetch_and_parse_games(False)
    games = get_games()
    updated, results_patch = _apply_enrich(games, parsed)
    write_json(GAMES_FILE, games)
    if results_patch:
        results = get_results()
        now = int(time.time())
        new_gids = []
        for gid, res in results_patch.items():
            if gid not in results:
                results[gid] = {**res, "registered_at": now}
                new_gids.append(gid)
        write_json(RESULTS_FILE, results)
        _settle_carryover(new_gids)
    return {"ok": True, "updated": updated, "total": len(games)}

@app.post("/api/admin/games/to-kst")
def admin_convert_games_to_kst(auth=Depends(admin_required)):
    """기존 게임의 일시를 UTC → KST(+9h)로 일괄 변환. kst:true 플래그로 중복 변환 방지."""
    games = get_games()
    converted = 0
    for g in games:
        if g.get("kst"):
            continue
        new_date, new_time = to_kst(g.get("date", ""), g.get("time", ""))
        g["date"] = new_date
        g["time"] = new_time
        g["kst"] = True
        converted += 1
    write_json(GAMES_FILE, games)
    return {"ok": True, "converted": converted, "total": len(games)}

@app.get("/api/admin/db/export")
def admin_db_export(auth=Depends(admin_required)):
    """전체 데이터 JSON 백업 (auth 토큰 제외). 볼륨/DB와 무관하게 항상 동작하는 백업 수단."""
    return {
        "exported_at": int(time.time()),
        "backend": "postgres" if DATABASE_URL else "sqlite",
        "data": {
            "games":          get_games(),
            "bets":           get_bets(),
            "results":        get_results(),
            "config":         get_config(),
            "feedback":       get_feedback(),
            "ai_predictions": get_ai_predictions(),
        },
    }

@app.get("/api/admin/db/info")
def admin_db_info(auth=Depends(admin_required)):
    """진단: 활성 백엔드·테이블 건수·SQLite 원본 상태"""
    tables = _LIST_TABLES + _DICT_TABLES + _KV_TABLES
    info = {"backend": "postgres" if DATABASE_URL else "sqlite",
            "data_dir": DATA_DIR, "sqlite_file_exists": os.path.exists(DB_FILE)}
    counts = {}
    if DATABASE_URL:
        with _pg_pool.connection() as conn:
            for t in tables:
                counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            row = conn.execute("SELECT value FROM meta WHERE key='migrated_from_sqlite'").fetchone()
            info["migration_flag"] = row[0] if row else None
    info["counts"] = counts
    # 이관 원본(SQLite) 건수 — 볼륨/DATA_DIR 문제 진단용
    sqlite_counts = {}
    if os.path.exists(DB_FILE):
        try:
            conn = _db_conn()
            for t in tables:
                try:
                    sqlite_counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except Exception:
                    sqlite_counts[t] = None
            conn.close()
        except Exception:
            pass
    info["sqlite_counts"] = sqlite_counts
    return info

# ── SQL 콘솔 (읽기 전용, 학습/조회용) ───────────────────────────
class SqlIn(BaseModel):
    sql: str = Field(..., min_length=1, max_length=2000)

@app.post("/api/admin/db/query")
def admin_db_query(body: SqlIn, auth=Depends(admin_required)):
    """관리자용 읽기 전용 SQL 콘솔. SELECT/WITH/EXPLAIN만 허용 + read-only 연결로 이중 방어."""
    sql = body.sql.strip().rstrip(";")
    # SELECT/WITH만 허용. EXPLAIN도 대상이 SELECT/WITH일 때만 (EXPLAIN DELETE 같은 형태 차단)
    if not re.match(r"(?is)^(explain(\s+query\s+plan)?\s+)?(select|with)\b", sql):
        raise HTTPException(400, "SELECT / WITH (및 그에 대한 EXPLAIN) 문만 실행할 수 있습니다")
    if DATABASE_URL:
        try:
            with psycopg.connect(DATABASE_URL, autocommit=False,
                                 options="-c default_transaction_read_only=on -c statement_timeout=5000") as conn:
                cur = conn.execute(sql)
                cols = [d.name for d in cur.description] if cur.description else []
                rows = cur.fetchmany(201)
                return {"columns": cols, "rows": [list(r) for r in rows[:200]], "truncated": len(rows) > 200}
        except psycopg.Error as e:
            raise HTTPException(400, f"SQL 오류: {str(e).strip()}")
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, timeout=5)
    try:
        conn.execute("PRAGMA query_only=ON")
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(201)
        return {"columns": cols, "rows": [list(r) for r in rows[:200]], "truncated": len(rows) > 200}
    except sqlite3.Error as e:
        raise HTTPException(400, f"SQL 오류: {e}")
    finally:
        conn.close()

# ── 토큰 변경 ─────────────────────────────────────────────────
@app.post("/api/admin/auth/token")
def update_token(body: TokenUpdate):
    auth   = get_auth()
    stored = auth.get("token", "")
    if stored and body.current_token != stored:
        raise HTTPException(401, "현재 토큰이 올바르지 않습니다")
    write_json(AUTH_FILE, {"token": body.new_token})
    return {"ok": True}

# ── 정적 파일 서빙 ─────────────────────────────────────────────
STATIC_DIR = os.path.dirname(__file__)

_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}

@app.get("/health")
def health():
    """Railway 헬스체크: DB 접근까지 확인해야 트래픽을 받을 준비가 된 것"""
    try:
        if DATABASE_URL:
            with _pg_pool.connection() as conn:
                conn.execute("SELECT 1")
        else:
            conn = _db_conn()
            conn.execute("SELECT 1")
            conn.close()
        return {"ok": True}
    except Exception:
        raise HTTPException(503, "db not ready")

@app.get("/")
def serve_index():
    # 메인: v2(애플 스포츠 스타일). 기존 페이지는 /v2 에 백업
    return FileResponse(os.path.join(STATIC_DIR, "v2.html"), headers=_NO_CACHE)

@app.get("/admin")
def serve_admin():
    return FileResponse(os.path.join(STATIC_DIR, "admin.html"), headers=_NO_CACHE)

@app.get("/v2")
def serve_v2():
    # 기존 메인 페이지 백업
    return FileResponse(os.path.join(STATIC_DIR, "ttoto.html"), headers=_NO_CACHE)

@app.get("/favicon.svg")
def serve_favicon():
    return FileResponse(os.path.join(STATIC_DIR, "favicon.svg"), media_type="image/svg+xml")

@app.get("/apple-touch-icon.png")
def serve_apple_icon():
    return FileResponse(os.path.join(STATIC_DIR, "apple-touch-icon.png"), media_type="image/png")

@app.get("/apple-touch-icon-precomposed.png")
def serve_apple_icon_precomposed():
    return FileResponse(os.path.join(STATIC_DIR, "apple-touch-icon.png"), media_type="image/png")

@app.get("/icon-192.png")
def serve_icon_192():
    return FileResponse(os.path.join(STATIC_DIR, "icon-192.png"), media_type="image/png")

@app.get("/icon-512.png")
def serve_icon_512():
    return FileResponse(os.path.join(STATIC_DIR, "icon-512.png"), media_type="image/png")
