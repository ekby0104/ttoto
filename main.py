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
import json, os, time, httpx, re
from datetime import datetime, timedelta

app = FastAPI(title="사무실 월드컵 토토 API")

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

def read_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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
    # 멕시코 (DST 미적용, 도시별 상이)
    ("Azteca", -6), ("Mexico City", -6), ("Ciudad de Mexico", -6),
    ("BBVA", -6), ("Monterrey", -6),
    ("Akron", -7), ("Guadalajara", -7),
]

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
    bet_amount:  Optional[int] = None
    kp_link:     Optional[str] = None
    site_title:  Optional[str] = None
    carryover:   Optional[int] = None

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
    return {"bet_amount": cfg.get("bet_amount", 3000), "kp_link": cfg.get("kp_link", ""), "site_title": cfg.get("site_title", "토토"), "carryover": cfg.get("carryover", 0)}

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
    results[str(game_id)] = {"h": body.h, "a": body.a}
    write_json(RESULTS_FILE, results)
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

@app.patch("/api/admin/games/{game_id}/status")
def admin_set_game_status(game_id: int, status: str, auth=Depends(admin_required)):
    games = get_games()
    for g in games:
        if str(g["id"]) == str(game_id):
            g["status"] = status
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
        date_str, time_str, converted = local_to_kst(date_str, time_str, venue_raw)

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
            results[gid] = p["result"]
        added += 1
    write_json(GAMES_FILE, existing)
    write_json(RESULTS_FILE, results)
    return {"ok": True, "added": added, "total": len(existing)}

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
