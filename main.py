"""
월드컵 토토 - FastAPI 서버
실행: uvicorn main:app --reload --port 8000
"""
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import json, os, time, httpx

app = FastAPI(title="사무실 월드컵 토토 API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_DIR     = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
BETS_FILE    = os.path.join(DATA_DIR, "bets.json")
CONFIG_FILE  = os.path.join(DATA_DIR, "config.json")
RESULTS_FILE = os.path.join(DATA_DIR, "results.json")
AUTH_FILE    = os.path.join(DATA_DIR, "auth.json")
GAMES_FILE   = os.path.join(DATA_DIR, "games.json")

os.makedirs(DATA_DIR, exist_ok=True)

def read_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_bets():    return read_json(BETS_FILE,    [])
def get_config():  return read_json(CONFIG_FILE,  {"bet_amount": 3000, "kp_link": "", "site_title": "토토", "carryover": 0})
def get_results(): return read_json(RESULTS_FILE, {})
def get_auth():    return read_json(AUTH_FILE,     {"token": ""})
def get_games():   return read_json(GAMES_FILE,    [])

def admin_required(x_admin_token: Optional[str] = Header(None)):
    auth   = get_auth()
    stored = auth.get("token", "")
    if stored and x_admin_token != stored:
        raise HTTPException(status_code=401, detail="관리자 권한이 필요합니다")
    return True

# ── 모델 ──────────────────────────────────────────────────────
class BetIn(BaseModel):
    game_id: int
    name:    str
    h:       int
    a:       int

class ConfigUpdate(BaseModel):
    bet_amount:  Optional[int] = None
    kp_link:     Optional[str] = None
    site_title:  Optional[str] = None
    carryover:   Optional[int] = None

class ResultIn(BaseModel):
    h: int
    a: int

class PaymentUpdate(BaseModel):
    paid: bool

class PayoutUpdate(BaseModel):
    paid_out: bool

class TokenUpdate(BaseModel):
    new_token:     str
    current_token: Optional[str] = ""

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

# ══════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════

@app.get("/api/config")
def public_config():
    cfg = get_config()
    return {"bet_amount": cfg.get("bet_amount", 3000), "kp_link": cfg.get("kp_link", ""), "site_title": cfg.get("site_title", "토토"), "carryover": cfg.get("carryover", 0)}

@app.get("/api/games")
def list_games():
    return get_games()

@app.post("/api/bets")
def submit_bet(bet: BetIn):
    games = get_games()
    if not any(str(g["id"]) == str(bet.game_id) for g in games):
        raise HTTPException(status_code=404, detail="게임을 찾을 수 없습니다")
    bets  = get_bets()
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
    if game_id is not None:
        bets = [b for b in bets if b["game_id"] == game_id]
    return bets

@app.get("/api/results")
def list_results():
    return get_results()

@app.get("/api/auth/status")
def auth_status():
    return {"has_token": bool(get_auth().get("token", ""))}

# ══════════════════════════════════════════════════════════════
# ADMIN API
# ══════════════════════════════════════════════════════════════

@app.get("/api/admin/config")
def admin_get_config(auth=Depends(admin_required)):
    return get_config()

@app.patch("/api/admin/config")
def admin_update_config(body: ConfigUpdate, auth=Depends(admin_required)):
    cfg = get_config()
    if body.bet_amount  is not None: cfg["bet_amount"]  = body.bet_amount
    if body.kp_link     is not None: cfg["kp_link"]     = body.kp_link
    if body.site_title  is not None: cfg["site_title"]  = body.site_title
    if body.carryover   is not None: cfg["carryover"]   = body.carryover
    write_json(CONFIG_FILE, cfg)
    return cfg

@app.get("/api/admin/bets")
def admin_list_bets(game_id: Optional[int] = None, auth=Depends(admin_required)):
    bets = get_bets()
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
    results[str(game_id)] = {"h": body.h, "a": body.a}
    write_json(RESULTS_FILE, results)
    return results[str(game_id)]

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
    games = get_games()
    for i, g in enumerate(games):
        if str(g["id"]) == str(game_id):
            games[i] = {
                "id":    game_id,
                "group": body.group or g["group"],
                "home":  {"name": body.home_name or g["home"]["name"],
                          "short": body.home_short or g["home"]["short"],
                          "flag":  body.home_flag  or g["home"]["flag"]},
                "away":  {"name": body.away_name or g["away"]["name"],
                          "short": body.away_short or g["away"]["short"],
                          "flag":  body.away_flag  or g["away"]["flag"]},
                "date":   body.date   or g["date"],
                "time":   body.time   or g["time"],
                "venue":  body.venue  or g["venue"],
                "status":   body.status   or g["status"],
                "bet_type": body.bet_type or g.get("bet_type", "exact"),
            }
            write_json(GAMES_FILE, games)
            return games[i]
    raise HTTPException(404, "게임을 찾을 수 없습니다")

@app.patch("/api/admin/games/{game_id}/status")
def admin_set_game_status(game_id: int, status: str, auth=Depends(admin_required)):
    games = get_games()
    for g in games:
        if str(g["id"]) == str(game_id):
            g["status"] = status
            write_json(GAMES_FILE, games)
            return g
    raise HTTPException(404, "게임을 찾을 수 없습니다")

@app.delete("/api/admin/games/all")
def admin_delete_all_games(auth=Depends(admin_required)):
    write_json(GAMES_FILE, [])
    write_json(RESULTS_FILE, {})
    return {"ok": True}

@app.delete("/api/admin/games/{game_id}")
def admin_delete_game(game_id: int, auth=Depends(admin_required)):
    games = [g for g in get_games() if str(g["id"]) != str(game_id)]
    write_json(GAMES_FILE, games)
    results = get_results()
    results.pop(str(game_id), None)
    write_json(RESULTS_FILE, results)
    return {"ok": True}

# ── 외부 API에서 경기 가져오기 (worldcup26.ir — 무료·키 불필요) ──
@app.post("/api/admin/games/import")
async def admin_import_games(korea_only: bool = False, auth=Depends(admin_required)):
    """
    worldcup26.ir 에서 전체 경기 일정을 가져와 games.json 에 병합.
    이미 id 가 같은 경기가 있으면 건너뜀.
    """
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
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://worldcup26.ir/get/games")
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        raise HTTPException(502, f"외부 API 오류: {e}")

    raw_matches = data if isinstance(data, list) else data.get("games", data.get("matches", []))
    existing    = get_games()
    existing_ids = {str(g["id"]) for g in existing}
    added = 0

    for m in raw_matches:
        ext_id = str(m.get("id") or m.get("match_number") or m.get("matchNumber") or "")
        if not ext_id or ext_id in existing_ids:
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

        # local_date: "06/11/2026 13:00" → date "2026.06.11", time "13:00"
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

        group_raw = str(m.get("group", "") or "")
        group_str = f"{group_raw}조" if group_raw and not group_raw.endswith("조") else group_raw

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
            "home":  {"name": h_name, "short": h_code, "flag": FLAG_MAP.get(h_name, "🏳️")},
            "away":  {"name": a_name, "short": a_code, "flag": FLAG_MAP.get(a_name, "🏳️")},
            "date":  date_str,
            "time":  time_str,
            "venue": str(m.get("stadium") or m.get("venue") or m.get("ground") or ""),
            "status": "closed" if has_score else "pending",
        }
        existing.append(game)
        existing_ids.add(ext_id)

        if has_score:
            results = get_results()
            results[ext_id] = {"h": h_score_int, "a": a_score_int}
            write_json(RESULTS_FILE, results)

        added += 1

    write_json(GAMES_FILE, existing)
    return {"ok": True, "added": added, "total": len(existing)}

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

@app.get("/")
def serve_index():
    return FileResponse(os.path.join(STATIC_DIR, "ttoto.html"))

@app.get("/admin")
def serve_admin():
    return FileResponse(os.path.join(STATIC_DIR, "admin.html"))
