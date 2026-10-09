"""LINE 餐廳推薦機器人
流程:選類型 -> 傳送位置 -> 顯示附近店家,並標示今天是否營業 / 公休。
"""
import base64
import hashlib
import hmac
import math
import os
import time
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from flask import Flask, abort, request

app = Flask(__name__)

# ---------- 設定(從 Render 的環境變數讀取) ----------
CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
GOOGLE_KEY = os.environ["GOOGLE_MAPS_API_KEY"]
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Taipei"))
RADIUS_M = int(os.environ.get("SEARCH_RADIUS_M", "1500"))  # 搜尋半徑(公尺)
MAX_RESULTS = 10  # 最多顯示幾間(LINE 卡片輪播上限是 12)

# 類型名稱 -> (表情符號, 實際拿去搜尋的關鍵字)
CATEGORIES = {
    "壽司": ("🍣", "壽司"),
    "海鮮": ("🦐", "海鮮餐廳"),
    "速食": ("🍔", "速食店"),
    "中式": ("🥢", "中式料理"),
    "火鍋": ("🍲", "火鍋"),
}
OTHER = "其他"
MENU_WORDS = {"選單", "開始", "menu", "hi", "hello", "你好", "哈囉"}

# 狀態 -> (文字, 顏色, 排序;數字越小越前面)
STATUS = {
    "open": ("營業中", "#06A84F", 0),
    "soon": ("即將打烊", "#E08A00", 1),
    "later": ("今天稍晚營業", "#2B7BD6", 2),
    "ended": ("今天已打烊", "#8A8F98", 3),
    "closed": ("今天公休", "#D64545", 4),
    "temp": ("暫停營業", "#D64545", 5),
    "unknown": ("營業時間不明", "#8A8F98", 6),
}

PLACE_FIELDS = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.rating",
        "places.userRatingCount",
        "places.googleMapsUri",
        "places.location",
        "places.businessStatus",
        "places.regularOpeningHours",
        "places.currentOpeningHours",
    ]
)

# ---------- 使用者暫存狀態(放在記憶體,30 分鐘沒動作就清掉) ----------
STATE = {}


def get_state(uid):
    now = time.time()
    for k in [k for k, v in STATE.items() if now - v["ts"] > 1800]:
        del STATE[k]
    st = STATE.setdefault(
        uid, {"cat": None, "wait_custom": False, "loc": None, "loc_ts": 0, "ts": now}
    )
    st["ts"] = now
    return st


# ---------- LINE 回覆工具 ----------
def line_reply(token, messages):
    r = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Authorization": f"Bearer {CHANNEL_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"replyToken": token, "messages": messages},
        timeout=10,
    )
    if r.status_code != 200:
        app.logger.error("LINE reply failed: %s %s", r.status_code, r.text)


def qr_text(label, text):
    return {"type": "action", "action": {"type": "message", "label": label, "text": text}}


def qr_location(label="📍 傳送我的位置"):
    return {"type": "action", "action": {"type": "location", "label": label}}


def text_msg(text, quick=None):
    msg = {"type": "text", "text": text}
    if quick:
        msg["quickReply"] = {"items": quick}
    return msg


def menu_msg(prefix="今天想吃什麼?點下面的選項 👇"):
    items = [qr_text(f"{emoji} {name}", name) for name, (emoji, _) in CATEGORIES.items()]
    items.append(qr_text("✏️ 其他", OTHER))
    return text_msg(prefix, items)


def ask_location_msg():
    return text_msg("請傳送你的位置,我幫你找附近的店 📍", [qr_location()])


# ---------- 營業狀態判斷 ----------
def fmt(minutes):
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}"


def periods_today(hours, now):
    """回傳今天的營業時段 [(開始分鐘, 結束分鐘或None)],結束超過 1440 代表跨午夜。"""
    ps = hours.get("periods", [])
    # Google 用「只有 open、沒有 close」表示 24 小時營業
    if len(ps) == 1 and "close" not in ps[0]:
        return [(0, None)]
    today = (now.year, now.month, now.day)
    gday = (now.weekday() + 1) % 7  # Google:0=週日 ... 6=週六
    out = []
    for p in ps:
        o, c = p.get("open"), p.get("close")
        if not o:
            continue
        d = o.get("date")
        if d:  # currentOpeningHours 會帶日期,已包含國定假日等特殊日
            if (d["year"], d["month"], d["day"]) != today:
                continue
        elif o.get("day") != gday:
            continue
        om = o.get("hour", 0) * 60 + o.get("minute", 0)
        if c:
            cm = c.get("hour", 0) * 60 + c.get("minute", 0)
            if cm <= om:
                cm += 1440
        else:
            cm = None
        out.append((om, cm))
    return sorted(out)


def hours_text(periods):
    if not periods:
        return "休息"
    if any(c is None for _, c in periods):
        return "24 小時營業"
    return "、".join(f"{fmt(o)}–{fmt(c)}" for o, c in periods)


def evaluate(place, now):
    """回傳 {"status", "label", "hours"};永久歇業的店回傳 None(直接略過)。"""
    bs = place.get("businessStatus", "OPERATIONAL")
    if bs == "CLOSED_PERMANENTLY":
        return None
    if bs == "CLOSED_TEMPORARILY":
        return {"status": "temp", "label": STATUS["temp"][0], "hours": "暫停營業"}

    hours = place.get("currentOpeningHours") or place.get("regularOpeningHours")
    if not hours or not hours.get("periods"):
        return {"status": "unknown", "label": STATUS["unknown"][0], "hours": "Google 地圖未提供"}

    periods = periods_today(hours, now)
    nm = now.hour * 60 + now.minute
    open_now = hours.get("openNow")
    if open_now is None:
        open_now = any(o <= nm < (c if c is not None else 10**6) for o, c in periods)
    text = hours_text(periods) if periods else "延續昨日營業時段"

    if open_now:
        remain, close_label = None, None
        t = hours.get("nextCloseTime")
        if t:
            try:
                close_dt = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(TZ)
                remain = (close_dt - now).total_seconds() / 60
                close_label = close_dt.strftime("%H:%M")
            except ValueError:
                pass
        if remain is None:
            for o, c in periods:
                if c is not None and o <= nm < c:
                    remain, close_label = c - nm, fmt(c)
        if remain is not None and 0 <= remain <= 60:
            return {"status": "soon", "label": f"即將打烊({close_label})", "hours": text}
        return {"status": "open", "label": STATUS["open"][0], "hours": text}

    if not periods:
        return {"status": "closed", "label": STATUS["closed"][0], "hours": "休息"}
    upcoming = [o for o, _ in periods if o > nm]
    if upcoming:
        return {
            "status": "later",
            "label": f"今天稍晚營業({fmt(min(upcoming))} 開)",
            "hours": text,
        }
    return {"status": "ended", "label": STATUS["ended"][0], "hours": text}


# ---------- Google Places 搜尋 ----------
def search_places(query, lat, lng):
    r = requests.post(
        "https://places.googleapis.com/v1/places:searchText",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_KEY,
            "X-Goog-FieldMask": PLACE_FIELDS,
        },
        json={
            "textQuery": query,
            "languageCode": "zh-TW",
            "maxResultCount": 20,
            "locationBias": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": float(RADIUS_M),
                }
            },
        },
        timeout=10,
    )
    r.raise_for_status()
    return r.json().get("places", [])


def distance_m(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


def fmt_dist(m):
    return f"{int(round(m, -1))} m" if m < 1000 else f"{m / 1000:.1f} km"


# ---------- 卡片 ----------
def bubble(place, dist, info):
    status = info["status"]
    color = STATUS[status][1]
    dim = status in ("closed", "temp", "ended", "unknown")
    name = place.get("displayName", {}).get("text", "(未命名)")
    meta = fmt_dist(dist)
    if place.get("rating"):
        meta = f"★ {place['rating']}({place.get('userRatingCount', 0)})· {meta}"
    uri = place.get("googleMapsUri") or f"https://www.google.com/maps/place/?q=place_id:{place['id']}"
    return {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {
                    "type": "text",
                    "text": name,
                    "weight": "bold",
                    "size": "md",
                    "wrap": True,
                    "color": "#999999" if dim else "#111111",
                },
                {"type": "text", "text": meta, "size": "xs", "color": "#666666"},
                {"type": "text", "text": f"今天:{info['hours']}", "size": "xs", "color": "#666666", "wrap": True},
                {"type": "text", "text": info["label"], "size": "sm", "weight": "bold", "color": color, "wrap": True},
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "link",
                    "height": "sm",
                    "action": {"type": "uri", "label": "在 Google 地圖開啟", "uri": uri},
                }
            ],
        },
    }


def do_search(token, st):
    cat = st["cat"]
    lat, lng = st["loc"]
    query = CATEGORIES[cat][1] if cat in CATEGORIES else cat
    try:
        places = search_places(query, lat, lng)
    except Exception:
        app.logger.exception("Places search failed")
        return line_reply(token, [menu_msg("查詢時出了點問題,請稍後再試一次 🙏")])

    now = datetime.now(TZ)
    rows = []
    for p in places:
        info = evaluate(p, now)
        loc = p.get("location")
        if info is None or not loc:
            continue
        dist = distance_m(lat, lng, loc["latitude"], loc["longitude"])
        if dist > RADIUS_M * 1.5:
            continue
        rows.append((STATUS[info["status"]][2], dist, p, info))
    rows.sort(key=lambda r: (r[0], r[1]))
    rows = rows[:MAX_RESULTS]

    if not rows:
        return line_reply(token, [menu_msg(f"附近找不到「{cat}」😢 換一種試試?")])

    cnt = Counter(r[3]["status"] for r in rows)
    lines = [f"附近找到 {len(rows)} 間「{cat}」"]
    a = cnt["open"] + cnt["soon"] + cnt["later"]
    b = cnt["closed"] + cnt["temp"]
    if a:
        lines.append(f"✅ 今天有營業:{a} 間")
    if b:
        lines.append(f"🚫 今天公休/暫停營業:{b} 間")
    if cnt["ended"]:
        lines.append(f"🌙 今天已打烊:{cnt['ended']} 間")
    if cnt["unknown"]:
        lines.append(f"❓ 營業時間不明:{cnt['unknown']} 間")
    lines.append("營業時間來自 Google 地圖,臨時公休請以店家公告為準。")

    flex = {
        "type": "flex",
        "altText": f"附近的{cat}推薦",
        "contents": {"type": "carousel", "contents": [bubble(p, d, i) for _, d, p, i in rows]},
        "quickReply": {"items": [qr_text("🔄 換一種類型", "選單"), qr_location("📍 換個位置")]},
    }
    line_reply(token, [text_msg("\n".join(lines)), flex])


# ---------- 事件處理 ----------
def handle(ev):
    token = ev.get("replyToken")
    if not token:
        return
    uid = ev.get("source", {}).get("userId", "anon")
    st = get_state(uid)

    if ev["type"] == "follow":
        return line_reply(token, [menu_msg("嗨!我可以幫你找附近「今天有開」的餐廳 🍴\n今天想吃什麼?")])
    if ev["type"] != "message":
        return

    m = ev["message"]
    if m["type"] == "location":
        st["loc"] = (m["latitude"], m["longitude"])
        st["loc_ts"] = time.time()
        if st["cat"]:
            return do_search(token, st)
        return line_reply(token, [menu_msg("收到位置了!想吃什麼?")])
    if m["type"] != "text":
        return line_reply(token, [menu_msg()])

    text = m["text"].strip()
    if text.lower() in MENU_WORDS:
        st["cat"], st["wait_custom"] = None, False
        return line_reply(token, [menu_msg()])
    if text == OTHER:
        st["wait_custom"] = True
        return line_reply(token, [text_msg("想吃什麼?直接輸入就好,例如:拉麵、燒肉、咖啡")])
    if text in CATEGORIES or st["wait_custom"]:
        st["cat"], st["wait_custom"] = text, False
        # 10 分鐘內傳過位置就直接用,不用再傳一次
        if st["loc"] and time.time() - st["loc_ts"] < 600:
            return do_search(token, st)
        return line_reply(token, [ask_location_msg()])
    return line_reply(token, [menu_msg()])


# ---------- Webhook ----------
@app.get("/")
def health():
    return "OK"


@app.post("/callback")
def callback():
    body = request.get_data()
    sig = request.headers.get("X-Line-Signature", "")
    expected = base64.b64encode(
        hmac.new(CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    ).decode()
    if not hmac.compare_digest(sig, expected):
        abort(400)
    for ev in (request.get_json(silent=True) or {}).get("events", []):
        try:
            handle(ev)
        except Exception:
            app.logger.exception("handle error")
    return "OK"
