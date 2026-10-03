#!/usr/bin/env python3
"""המהדורה — אוסף כתבות, תוצאות כדורגל ומזג אוויר, ומייצר data/news.json.

רץ ב-GitHub Actions. קורא config.json, כותב:
  data/news.json   — מה שהדף מציג
  data/cache.json  — כתבות שכבר עובדו (כדי לא לתרגם פעמיים)

מפתחות (GitHub Secrets, לא חובה — בלעדיהם החלק המתאים פשוט מדולג ומסומן במצב המקורות):
  ANTHROPIC_API_KEY   — סינון, תרגום ותקציר בעברית
  FOOTBALL_DATA_KEY   — פרמייר ליג ובונדסליגה (football-data.org)
  API_FOOTBALL_KEY    — ליגת העל (api-football.com)
"""
import calendar
import difflib
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
TZ_IL = timezone(timedelta(hours=3))

# כתובות בסיס — ניתנות להחלפה במשתני סביבה לצורך בדיקות יבשות בלבד
ANTHROPIC_URL = os.environ.get("ANTHROPIC_URL", "https://api.anthropic.com/v1/messages")
FD_URL = os.environ.get("FD_URL", "https://api.football-data.org/v4")
AF_URL = os.environ.get("AF_URL", "https://v3.football.api-sports.io")
METEO_URL = os.environ.get("METEO_URL", "https://api.open-meteo.com/v1/forecast")
FX_URL = os.environ.get("FX_URL", "https://api.frankfurter.app/latest?from=USD&to=ILS,EUR")
MODEL = os.environ.get("HM_MODEL", "claude-haiku-4-5-20251001")

NOW = datetime.now(timezone.utc)


def log(*a):
    print(*a, flush=True)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def strip_html(s, limit=600):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


# ───────────────────────── RSS ─────────────────────────

def find_image(e):
    """תמונה מהפיד: media:content / media:thumbnail / enclosure / <img> בתיאור."""
    for key in ("media_content", "media_thumbnail"):
        for m in e.get(key) or []:
            u = m.get("url")
            if u and not u.lower().endswith((".mp4", ".mp3", ".m3u8")):
                return u
    for l in e.get("links") or []:
        if l.get("rel") == "enclosure" and (l.get("type") or "").startswith("image"):
            return l.get("href")
    for enc in e.get("enclosures") or []:
        if (enc.get("type") or "").startswith("image"):
            return enc.get("href") or enc.get("url")
    blob = e.get("summary", "") or ""
    for c in e.get("content") or []:
        blob += c.get("value", "")
    m = re.search(r"<img[^>]+src=['\"]([^'\"]+)['\"]", blob)
    return m.group(1) if m else None


def find_credit(e, src_name):
    for key in ("media_credit", "credit"):
        v = e.get(key)
        if isinstance(v, list) and v:
            v = v[0].get("content") if isinstance(v[0], dict) else v[0]
        if isinstance(v, dict):
            v = v.get("content")
        if v and isinstance(v, str) and v.strip():
            return v.strip()[:80]
    return src_name


def entry_time(e):
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            try:
                return datetime.fromtimestamp(calendar.timegm(t), timezone.utc)
            except Exception:
                pass
    return None


def fetch_source(src, hours_back, per_source):
    t0 = time.time()
    h = {"id": src["id"], "name": src["name"], "ok": False, "fresh": 0, "error": None}
    try:
        url = src["url"] if "://" in src["url"] else "https://" + src["url"]
        r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"}, timeout=20)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
        if not feed.entries and b"<html" in r.content[:3000].lower():
            # כתובת אתר ולא פיד — מחפשים את הפיד שהאתר מצהיר עליו
            m = re.search(rb'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', r.content, re.I)
            href = re.search(rb'href=["\']([^"\']+)["\']', m.group(0)) if m else None
            if href:
                feed_url = requests.compat.urljoin(r.url, html.unescape(href.group(1).decode("utf-8", "ignore")))
                r = requests.get(feed_url, headers={"User-Agent": UA}, timeout=20)
                r.raise_for_status()
                feed = feedparser.parse(r.content)
                h["feed"] = feed_url
        if not feed.entries:
            raise ValueError("הפיד ריק או לא תקין")
        cutoff = NOW - timedelta(hours=hours_back)
        items = []
        for e in feed.entries:
            link = e.get("link") or ""
            title = strip_html(e.get("title", ""), 300)
            if not link or not title:
                continue
            ts = entry_time(e)
            if ts and ts < cutoff:
                continue
            if ts and ts > NOW + timedelta(hours=2):
                ts = NOW
            items.append({
                "id": hashlib.sha1(link.encode("utf-8")).hexdigest()[:14],
                "src": src["id"], "src_name": src["name"],
                "lang": "he" if re.search(r"[֐-׿]", title) else "en",
                "url": link, "title": title,
                "desc": strip_html(e.get("summary", ""), 600),
                "img": find_image(e), "credit": find_credit(e, src["name"]),
                "ts": iso(ts) if ts else iso(NOW),
            })
            if len(items) >= per_source:
                break
        h.update(ok=True, fresh=len(items))
        return items, h
    except Exception as ex:
        h["error"] = str(ex)[:160]
        return [], h
    finally:
        h["ms"] = int((time.time() - t0) * 1000)


# ───────────────────────── AI ─────────────────────────

def env_key(name):
    """מפתח מ-Secrets, בלי רווחים ושורות ריקות שנדבקו בהעתקה."""
    return (os.environ.get(name) or "").strip()


def claude(prompt, max_tokens=8000):
    key = env_key("ANTHROPIC_API_KEY")
    r = requests.post(ANTHROPIC_URL, timeout=180, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    data = r.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"), data.get("usage", {})


def parse_json_array(text):
    a, b = text.find("["), text.rfind("]")
    if a < 0 or b <= a:
        raise ValueError("no JSON array in reply")
    return json.loads(text[a:b + 1])


def edit_batch(batch, sections, topics):
    sec_lines = "\n".join(f'- "{s["id"]}": {s["name"]} — {s.get("about", "")}' for s in sections)
    items = [{"id": it["id"], "source": it["src_name"], "lang": it["lang"],
              "title": it["title"], "text": it["desc"][:450]} for it in batch]
    prompt = f"""אתה העורך של "המהדורה", מגזין חדשות אישי בעברית לקורא ישראלי.

המדורים הפעילים:
{sec_lines}

תחומי עניין מיוחדים של הקורא (העלה להם ציון): {", ".join(topics) if topics else "אין"}

לכל כתבה ברשימה החלט:
- keep: true אם היא שייכת לאחד המדורים ומעניינת, false אם לא (ספורט שאינו כדורגל, רכילות זניחה, פרסומות, תוכן מקומי שולי, הורוסקופ וכד׳).
- section: מזהה המדור המתאים ביותר מתוך הרשימה למעלה (לא "sport" ולא "wx" — אלה מתמלאים אוטומטית). כללי שיבוץ מחייבים:
  • קולנוע, סדרות, טלוויזיה, מוזיקה, שחקנים, במאים, פסטיבלים וסלבס → המדור של קולנוע ובידור, גם אם יש בכתבה עסקים או חברת טכנולוגיה.
  • חקירות, פלילים, פוליטיקה, ביטחון, משפט וחדשות כלליות בישראל → המדור של ישראל, גם אם המקור הוא אתר כלכלי.
  • בינה מלאכותית → המדור של AI. כל השאר בטכנולוגיה, גאדג׳טים, סייבר, שוק ההון, חברות וכלכלה → טכנולוגיה, כלכלה ועסקים.
  • חדשות עולם שאין להן קשר לישראל ולא לאחד המדורים → keep: false, אלא אם הן אירוע עולמי מרכזי במיוחד (אז למדור של ישראל עם ציון נמוך).
- score: 0–100, כמה הכתבה חשובה ומעניינת לקורא הזה.
- title_he: כותרת בעברית. לכתבה בעברית — השאר את הכותרת המקורית. לכתבה בשפה זרה — תרגום עברי טבעי, קצר וקולע.
- summary_he: תקציר של 1–2 משפטים בעברית, בניסוח שלך בלבד (לא העתקה ולא תרגום מילולי של הטקסט), עד 230 תווים.
- kicker: תגית נושא קצרה של מילה או שתיים בעברית (למשל: ביטחון, בחירות, מודלים, שבבים, קופות).

החזר אך ורק מערך JSON, בלי שום טקסט נוסף, בפורמט:
[{{"id":"...","keep":true,"section":"il","score":70,"title_he":"...","summary_he":"...","kicker":"..."}}]

הכתבות:
{json.dumps(items, ensure_ascii=False)}"""
    text, usage = claude(prompt)
    out = {}
    for row in parse_json_array(text):
        if isinstance(row, dict) and row.get("id"):
            out[row["id"]] = row
    return out, usage


def fallback_edit(it, sections):
    """בלי מפתח AI: רק כתבות בעברית, במדור לפי מילות מפתח פשוטות."""
    if it["lang"] != "he":
        return {"keep": False}
    ids = [s["id"] for s in sections]
    t = it["title"] + " " + it["desc"]
    sec = "il"
    if "ai" in ids and re.search(r"בינה מלאכותית|\bAI\b|ChatGPT|OpenAI|Claude|Gemini", t):
        sec = "ai"
    elif "tech" in ids and it["src"] in ("globes", "calcalist", "geektime"):
        sec = "tech"
    elif "film" in ids and it["src"] == "ynet-cult":
        sec = "film"
    if sec not in ids:
        return {"keep": False}
    return {"keep": True, "section": sec, "score": 50, "title_he": it["title"],
            "summary_he": it["desc"][:230], "kicker": ""}


# ───────────────────────── כדורגל ─────────────────────────

TEAMS_HE = {
    # England
    "Arsenal": "ארסנל", "Aston Villa": "אסטון וילה", "Bournemouth": "בורנמות׳", "Brentford": "ברנטפורד",
    "Brighton": "ברייטון", "Chelsea": "צ׳לסי", "Coventry": "קובנטרי", "Crystal Palace": "קריסטל פאלאס",
    "Everton": "אברטון", "Fulham": "פולהאם", "Hull": "האל סיטי", "Ipswich": "איפסוויץ׳", "Leeds": "לידס",
    "Liverpool": "ליברפול", "Manchester City": "מנצ׳סטר סיטי", "Man City": "מנצ׳סטר סיטי",
    "Manchester United": "מנצ׳סטר יונייטד", "Man United": "מנצ׳סטר יונייטד", "Newcastle": "ניוקאסל",
    "Nottingham": "נוטינגהאם פורסט", "Sunderland": "סנדרלנד", "Tottenham": "טוטנהאם", "West Ham": "ווסטהאם",
    "Wolverhampton": "וולבס", "Wolves": "וולבס", "Burnley": "ברנלי", "Leicester": "לסטר", "Southampton": "סאות׳המפטון",
    # Germany
    "Bayern": "באיירן מינכן", "Dortmund": "דורטמונד", "Leverkusen": "לברקוזן", "Leipzig": "לייפציג",
    "Frankfurt": "פרנקפורט", "Freiburg": "פרייבורג", "Mönchengladbach": "גלדבאך", "Monchengladbach": "גלדבאך",
    "Gladbach": "גלדבאך", "Mainz": "מיינץ", "Hamburger": "המבורג", "HSV": "המבורג", "Köln": "קלן", "Koln": "קלן",
    "Cologne": "קלן", "Bremen": "ורדר ברמן", "Augsburg": "אאוגסבורג", "Stuttgart": "שטוטגרט",
    "Union Berlin": "אוניון ברלין", "Elversberg": "אלברסברג", "Schalke": "שאלקה", "Paderborn": "פאדרבורן",
    "Hoffenheim": "הופנהיים", "Wolfsburg": "וולפסבורג", "Heidenheim": "היידנהיים", "St. Pauli": "סנט פאולי",
    "Bochum": "בוכום", "Kiel": "קיל", "Hertha": "הרטה ברלין",
    # Israel
    "Maccabi Haifa": "מכבי חיפה", "Maccabi Tel Aviv": "מכבי תל אביב", "Hapoel Beer Sheva": "הפועל באר שבע",
    "Hapoel Be'er Sheva": "הפועל באר שבע", "Beitar Jerusalem": "בית״ר ירושלים", "Hapoel Tel Aviv": "הפועל תל אביב",
    "Maccabi Netanya": "מכבי נתניה", "Hapoel Haifa": "הפועל חיפה", "Bnei Sakhnin": "בני סכנין",
    "Hapoel Jerusalem": "הפועל ירושלים", "Ashdod": "מ.ס. אשדוד", "Hapoel Petah Tikva": "הפועל פתח תקווה",
    "Maccabi Petah Tikva": "מכבי פתח תקווה", "Bnei Reineh": "מכבי בני ריינה", "Bnei Raina": "מכבי בני ריינה",
    "Kiryat Shmona": "עירוני קריית שמונה", "Tiberias": "עירוני טבריה", "Hapoel Hadera": "הפועל חדרה",
    "Bnei Yehuda": "בני יהודה", "Kfar Saba": "הפועל כפר סבא", "Hapoel Kfar Saba": "הפועל כפר סבא",
    "Maccabi Bnei Raina": "מכבי בני ריינה", "Ironi Kiryat Shmona": "עירוני קריית שמונה",
}


def team_he(name):
    if not name:
        return ""
    for k in sorted(TEAMS_HE, key=len, reverse=True):
        if k.lower() in name.lower():
            return TEAMS_HE[k]
    return name


def team_short(name):
    letters = re.sub(r"[^A-Za-z ]", "", name or "").split()
    if not letters:
        return (name or "?")[:3]
    if len(letters) == 1:
        return letters[0][:3].upper()
    return "".join(w[0] for w in letters[:3]).upper()


def football_data(league, key):
    d_from = (NOW - timedelta(days=12)).strftime("%Y-%m-%d")
    d_to = (NOW + timedelta(days=12)).strftime("%Y-%m-%d")
    r = requests.get(f'{FD_URL}/competitions/{league["code"]}/matches', timeout=20,
                     headers={"X-Auth-Token": key}, params={"dateFrom": d_from, "dateTo": d_to})
    r.raise_for_status()
    out = []
    for m in r.json().get("matches", []):
        st = m.get("status")
        ft = (m.get("score") or {}).get("fullTime") or {}
        out.append({
            "ts": m.get("utcDate"), "status": "done" if st == "FINISHED" else ("live" if st in ("IN_PLAY", "PAUSED") else "next"),
            "home": team_he(m["homeTeam"].get("shortName") or m["homeTeam"].get("name")),
            "away": team_he(m["awayTeam"].get("shortName") or m["awayTeam"].get("name")),
            "home_s": m["homeTeam"].get("tla") or team_short(m["homeTeam"].get("name")),
            "away_s": m["awayTeam"].get("tla") or team_short(m["awayTeam"].get("name")),
            "hg": ft.get("home"), "ag": ft.get("away"),
        })
    return out


def api_football(league, key):
    season = NOW.year if NOW.month >= 7 else NOW.year - 1
    out = []
    for param in ({"last": 8}, {"next": 4}):
        r = requests.get(f"{AF_URL}/fixtures", timeout=20, headers={"x-apisports-key": key},
                         params={"league": league["code"], "season": season, **param})
        r.raise_for_status()
        j = r.json()
        if j.get("errors"):
            raise ValueError(str(j["errors"])[:150])
        for f in j.get("response", []):
            st = f["fixture"]["status"]["short"]
            out.append({
                "ts": f["fixture"]["date"],
                "status": "done" if st in ("FT", "AET", "PEN") else ("live" if st in ("1H", "2H", "HT", "ET", "P") else "next"),
                "home": team_he(f["teams"]["home"]["name"]), "away": team_he(f["teams"]["away"]["name"]),
                "home_s": team_short(f["teams"]["home"]["name"]), "away_s": team_short(f["teams"]["away"]["name"]),
                "hg": f["goals"]["home"], "ag": f["goals"]["away"],
            })
    return out


def collect_sport(cfg, health):
    res = []
    for lg in cfg.get("leagues", []):
        if not lg.get("on"):
            continue
        h = {"id": "sport-" + lg["id"], "name": lg["name"], "ok": False, "fresh": 0, "error": None}
        try:
            if lg["provider"] == "football-data":
                key = env_key("FOOTBALL_DATA_KEY")
                if not key:
                    raise ValueError("חסר מפתח FOOTBALL_DATA_KEY")
                matches = football_data(lg, key)
            else:
                key = env_key("API_FOOTBALL_KEY")
                if not key:
                    raise ValueError("חסר מפתח API_FOOTBALL_KEY")
                matches = api_football(lg, key)
            done = sorted([m for m in matches if m["status"] != "next"], key=lambda m: m["ts"])[-8:]
            nxt = sorted([m for m in matches if m["status"] == "next"], key=lambda m: m["ts"])[:4]
            res.append({"id": lg["id"], "name": lg["name"], "flag": lg.get("flag", ""),
                        "matches": list(reversed(done)) + nxt})
            h.update(ok=True, fresh=len(done) + len(nxt))
        except Exception as ex:
            h["error"] = str(ex)[:160]
        health.append(h)
    return res


# ───────────────────────── מזג אוויר ─────────────────────────

WMO = {0: ("בהיר", "☀️"), 1: ("בהיר ברובו", "🌤"), 2: ("מעונן חלקית", "⛅"), 3: ("מעונן", "☁️"),
       45: ("ערפל", "🌫"), 48: ("ערפל", "🌫"), 51: ("טפטוף", "🌦"), 53: ("טפטוף", "🌦"), 55: ("טפטוף", "🌦"),
       61: ("גשם קל", "🌧"), 63: ("גשם", "🌧"), 65: ("גשם חזק", "🌧"), 66: ("גשם קפוא", "🌧"), 67: ("גשם קפוא", "🌧"),
       71: ("שלג", "🌨"), 73: ("שלג", "🌨"), 75: ("שלג כבד", "🌨"), 80: ("ממטרים", "🌦"), 81: ("ממטרים", "🌧"),
       82: ("ממטרים עזים", "⛈"), 95: ("סופת רעמים", "⛈"), 96: ("סופת רעמים", "⛈"), 99: ("סופת רעמים", "⛈")}


def collect_weather(cfg, health):
    cities = cfg.get("cities", [])
    h = {"id": "wx", "name": "Open-Meteo", "ok": False, "fresh": 0, "error": None}
    out = []
    if not cities:
        h.update(ok=True)
        health.append(h)
        return out
    try:
        r = requests.get(METEO_URL, timeout=25, params={
            "latitude": ",".join(str(c["lat"]) for c in cities),
            "longitude": ",".join(str(c["lon"]) for c in cities),
            "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max",
            "timezone": "Asia/Jerusalem", "forecast_days": 3})
        r.raise_for_status()
        j = r.json()
        rows = j if isinstance(j, list) else [j]
        for c, w in zip(cities, rows):
            cur, d = w.get("current", {}), w.get("daily", {})
            code = cur.get("weather_code", 0)
            txt, ic = WMO.get(code, ("", "🌡"))
            days = []
            for i in range(len(d.get("time", []))):
                dc = d["weather_code"][i]
                days.append({"date": d["time"][i], "max": round(d["temperature_2m_max"][i]),
                             "min": round(d["temperature_2m_min"][i]), "ic": WMO.get(dc, ("", "🌡"))[1],
                             "rain": d.get("precipitation_probability_max", [None] * 9)[i]})
            out.append({"name": c["name"], "t": round(cur.get("temperature_2m", 0)), "txt": txt, "ic": ic,
                        "code": code, "wind": round(cur.get("wind_speed_10m", 0)),
                        "hum": cur.get("relative_humidity_2m"), "days": days})
        h.update(ok=True, fresh=len(out))
    except Exception as ex:
        h["error"] = str(ex)[:160]
    health.append(h)
    return out


def collect_fx(health):
    h = {"id": "fx", "name": "שערי מטבע (ECB)", "ok": False, "fresh": 0, "error": None}
    try:
        r = requests.get(FX_URL, timeout=15)
        r.raise_for_status()
        rates = r.json().get("rates", {})
        usd_ils = rates.get("ILS")
        eur_ils = (usd_ils / rates["EUR"]) if usd_ils and rates.get("EUR") else None
        h.update(ok=True, fresh=1)
        health.append(h)
        return {"usd": round(usd_ils, 3) if usd_ils else None, "eur": round(eur_ils, 3) if eur_ils else None}
    except Exception as ex:
        h["error"] = str(ex)[:160]
        health.append(h)
        return None


# ───────────────────────── ריצה ─────────────────────────

def similar(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio() > 0.72


def main():
    os.makedirs(DATA, exist_ok=True)
    cfg = load_json(os.path.join(ROOT, "config.json"), None)
    if not cfg:
        log("config.json missing or invalid")
        sys.exit(1)
    lim = cfg.get("limits", {})
    hours_back = lim.get("hours_back", 36)
    per_source = lim.get("per_source", 12)
    per_section = lim.get("per_section", 14)
    max_new = lim.get("max_new_per_run", 140)

    sections_all = cfg.get("sections", [])
    news_secs = [s for s in sections_all if s.get("on") and s["id"] not in ("sport", "wx")]
    news_ids = {s["id"] for s in news_secs}
    topics = cfg.get("topics", [])

    cache = load_json(os.path.join(DATA, "cache.json"), {})
    cache = {k: v for k, v in cache.items()
             if v.get("ts", "") >= iso(NOW - timedelta(hours=hours_back + 12))}
    health = []

    # 1) מקורות
    pool = []
    for src in cfg.get("sources", []):
        if not src.get("on"):
            continue
        items, h = fetch_source(src, hours_back, per_source)
        health.append(h)
        pool.extend(items)
        log(f'{"✓" if h["ok"] else "✗"} {src["name"]:<24} {h["fresh"]:>3}  {h["error"] or ""}')
    seen = set()
    pool = [it for it in pool if not (it["id"] in seen or seen.add(it["id"]))]

    # 2) עריכה — רק מה שעוד לא עובד, או שעובד תחת הגדרות אחרות
    sig = hashlib.sha1(json.dumps([sorted(news_ids), topics], ensure_ascii=False).encode()).hexdigest()[:10]
    has_ai = bool(env_key("ANTHROPIC_API_KEY"))
    todo = [it for it in pool if it["id"] not in cache or cache[it["id"]].get("sig") != sig]
    todo.sort(key=lambda it: it["ts"], reverse=True)
    todo = todo[:max_new]
    ai_h = {"id": "ai", "name": "תרגום ועריכה (Claude)", "ok": True, "fresh": 0, "error": None}
    usage_in = usage_out = 0
    if has_ai and news_secs:
        for i in range(0, len(todo), 15):
            batch = todo[i:i + 15]
            try:
                res, usage = edit_batch(batch, news_secs, topics)
                usage_in += usage.get("input_tokens", 0)
                usage_out += usage.get("output_tokens", 0)
                for it in batch:
                    r = res.get(it["id"])
                    if r is None:
                        continue  # לא חזר — ינוסה שוב בריצה הבאה
                    cache[it["id"]] = {**it, **{k: r.get(k) for k in ("keep", "section", "score", "title_he", "summary_he", "kicker")}, "sig": sig}
                    ai_h["fresh"] += 1
            except Exception as ex:
                ai_h.update(ok=False, error=str(ex)[:160])
                log("AI batch failed:", ex)
    else:
        if not has_ai:
            ai_h.update(ok=False, error="חסר מפתח ANTHROPIC_API_KEY — מוצגות רק כתבות בעברית, בלי תרגום")
        for it in todo:
            cache[it["id"]] = {**it, **fallback_edit(it, news_secs), "sig": sig if has_ai else "noai"}
    ai_h["tokens"] = {"in": usage_in, "out": usage_out}
    health.append(ai_h)

    # 3) הרכבת המדורים
    pool_ids = {it["id"] for it in pool}
    sections = {sid: [] for sid in news_ids}
    kept = [v for v in cache.values() if v.get("keep") and v.get("section") in news_ids and v["id"] in pool_ids]

    def rank(v):
        age_h = (NOW - datetime.strptime(v["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds() / 3600
        return (v.get("score") or 0) - age_h * 1.6 + (6 if v.get("img") else 0)

    for v in sorted(kept, key=rank, reverse=True):
        lst = sections[v["section"]]
        if len(lst) >= per_section:
            continue
        if any(similar(v.get("title_he") or "", x["title"]) for x in lst):
            continue
        lst.append({"id": v["id"], "title": v.get("title_he") or v["title"], "summary": v.get("summary_he") or "",
                    "kicker": v.get("kicker") or "", "url": v["url"], "src": v["src"], "src_name": v["src_name"],
                    "lang": v["lang"], "img": v.get("img"), "credit": v.get("credit"), "ts": v["ts"],
                    "score": v.get("score")})

    sport = collect_sport(cfg, health) if any(s["id"] == "sport" and s.get("on") for s in sections_all) else []
    weather = collect_weather(cfg, health) if any(s["id"] == "wx" and s.get("on") for s in sections_all) else []
    fx = collect_fx(health)

    out = {"updated": iso(NOW), "sections": sections, "sport": sport, "weather": weather, "fx": fx,
           "health": health, "ai": has_ai}
    save_json(os.path.join(DATA, "news.json"), out)
    save_json(os.path.join(DATA, "cache.json"), cache)
    log(f"done: {sum(len(v) for v in sections.values())} articles, tokens in/out {usage_in}/{usage_out}")


if __name__ == "__main__":
    main()
