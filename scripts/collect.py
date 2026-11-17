#!/usr/bin/env python3
"""Reddit-Issue-Korea collector.

1. ApeWisdom       which US tickers Reddit is talking about (mentions now vs 24h ago)
2. Yahoo Finance   latest English headlines + RSS blurb per ticker (RSS feed)
3. Gemini / OpenRouter (free tiers only)  Korean headline titles

Outputs: data/issues.json (page), data/articles.json (translation cache, 10 days), data/names.json
Secrets: GEMINI_API_KEY (Google AI Studio free tier, used first) and/or OPENROUTER_API_KEY (free ":free" models only)
"""
import datetime as dt
import email.utils
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONF = ROOT / "config" / "watchlist.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
APE = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/{page}"
RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={s}&region=US&lang=en-US"
OR_BASE = "https://openrouter.ai/api/v1"
OR_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
GM_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
GM_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
PROVIDER = {"base": OR_BASE, "key": OR_KEY, "name": "openrouter"}
OR_MODEL = os.environ.get("OR_MODEL", "").strip()
# Free models only. OpenRouter free limit: 20 requests/minute, 50 requests/day (below $10 lifetime purchases)
FREE_PREFS = ["google/gemma", "minimax/", "nvidia/nemotron", "qwen/", "deepseek/", "meta-llama/"]
MAX_CALLS = 8           # requests per run (well inside the free daily caps)

NOW = dt.datetime.now(dt.timezone.utc)
NEWS_HOURS = 72          # headlines older than this are ignored
KEEP_DAYS = 10           # translation cache
PER_TICKER = 6           # newest relevant candidates kept per ticker
HOT_TOP = 10             # most-mentioned tickers
HOT_SURGE = 10           # fastest-rising tickers
MAX_TRANSLATE = 150      # new headlines translated per run
BATCH = 40

AMBIGUOUS = set("""
HR CIA IT DTE NYT AI DD CEO CFO USA EV PM AM TV UK EU IRS IPO ATH EOD IMO EPS GDP CPI PE OP RH PT TA DCA IV PSA DC
ER PR ATM OTM ITM FD API USD CAD EUR GBP RSI EMA SMA UI IP ID PC TLDR EDIT YOLO FOMO HODL IRA HSA CA TX FL NY
IQ ICE ON ALL NOW SO A I ARE FOR OR GO BE CAN ANY ONE OUT REAL GOOD WELL BIG NEXT OPEN POST PLAY LOVE LIFE FREE
SAVE CASH TELL VERY MAN FUN HAS BRO WOW DAY KEY NEW OLD TWO SEE RUN LOW PAY JOB CAR EAT FLY HOPE RIDE BEAT MOVE
TURN SELF LINK MAIN PEAK ROLL UNIT FAST EVER ELSE JUST MOST MUCH ONLY SOME THAN THAT THEN THEY THIS WHEN WILL
WITH WHAT WSJ CNBC FDA DOJ FTC SEC FED ETF NFT GPU CPU LLM AGI MOON APE BULL BEAR PUMP DUMP
YOU ME WE US AN AT BY UP DO NO TO OF IN IS AS HE SHE HIM HER OUR WHO WHY HOW
""".split())
INDEX_ETF = set("""
SPY QQQ IWM DIA VOO VTI VT VXUS QQQM TQQQ SQQQ SPXL SPXS SPXU UPRO SOXL SOXS SOXX SMH UVXY VXX SVXY TLT TMF GLD SLV
IAU SCHD JEPI JEPQ XLF XLE XLK XLV XLI XLP XLU XLY ARKK IBIT BITO FXI EEM EFA HYG LQD USO UNG TSLL NVDL CONL MSTU
""".split())
# English words that identify the company in a headline (ticker is always checked too)
EN = {"TSLA": "tesla", "NVDA": "nvidia", "PLTR": "palantir", "AAPL": "apple", "GOOGL": "alphabet|google", "GOOG": "alphabet|google",
      "MSFT": "microsoft", "AMZN": "amazon", "AVGO": "broadcom", "META": "meta|facebook|instagram", "AMD": "amd|advanced micro",
      "MU": "micron", "IONQ": "ionq", "RKLB": "rocket lab", "MSTR": "strategy|microstrategy|saylor", "SPCX": "spacex",
      "TSM": "tsmc|taiwan semiconductor", "ASML": "asml", "AMAT": "applied materials", "LRCX": "lam research", "QCOM": "qualcomm",
      "GM": "general motors|\\bgm\\b", "F": "\\bford\\b", "GEV": "ge vernova|vernova", "ETN": "eaton", "SMR": "nuscale",
      "CEG": "constellation", "OKLO": "oklo", "HII": "huntington ingalls|\\bhii\\b"}
SRC = {"finance.yahoo.com": "Yahoo Finance", "fool.com": "Motley Fool", "stocktwits.com": "Stocktwits", "investors.com": "IBD",
       "barrons.com": "Barron's", "wsj.com": "WSJ", "reuters.com": "Reuters", "bloomberg.com": "Bloomberg", "cnbc.com": "CNBC",
       "marketwatch.com": "MarketWatch", "investing.com": "Investing.com", "benzinga.com": "Benzinga", "zacks.com": "Zacks",
       "seekingalpha.com": "Seeking Alpha", "simplywall.st": "Simply Wall St", "insidermonkey.com": "Insider Monkey"}

# Sources whose links fail or are unreliable for users in Korea (add a domain when a link keeps failing)
BLOCKED_SRC = {"firstformoney.com"}

DIAG, USAGE = [], {"calls": 0, "req": 0, "in": 0, "out": 0}


def log(*a):
    print(*a, flush=True)


def diag(src, msg):
    DIAG.append({"src": src, "msg": str(msg)[:300]})
    log("DIAG", src, msg)


def get(url, timeout=40, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (403, 404):
                return None
        except requests.RequestException:
            pass
        time.sleep(1.5 * (i + 1))
    return None


def to_int(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def load(name, default):
    p = DATA / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            diag("cache", f"{name} unreadable - rebuilt")
    return default


def save(name, obj):
    (DATA / name).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


# ------------------------------------------------------------------ 1. Reddit attention
def reddit():
    rows = {}
    for page in range(1, 6):
        r = get(APE.format(page=page), tries=3 if page == 1 else 2)
        if not r:
            break
        try:
            js = r.json()
        except ValueError:
            break
        for x in js.get("results") or []:
            s = str(x.get("ticker") or "").upper().strip().replace(".", "-")
            if s and s not in rows:
                rows[s] = {"m": to_int(x.get("mentions")) or 0, "m24": to_int(x.get("mentions_24h_ago")) or 0,
                           "r": to_int(x.get("rank")), "r24": to_int(x.get("rank_24h_ago")),
                           "name": re.sub(r"&amp;", "&", str(x.get("name") or ""))[:60]}
        if page >= (to_int(js.get("pages")) or 1):
            break
        time.sleep(0.4)
    if len(rows) < 20:
        diag("reddit", "ApeWisdom did not respond - Reddit ranking skipped this run")
        return {}, 1.0
    tot, tot24 = sum(v["m"] for v in rows.values()), sum(v["m24"] for v in rows.values())
    mkt = min(4.0, max(0.25, tot / tot24)) if tot24 else 1.0
    for v in rows.values():
        v["rel"] = round((v["m"] / max(v["m24"], 5)) / mkt, 3)
    log("reddit tickers", len(rows), "market ratio", round(mkt, 2))
    return rows, mkt


def hot_list(rows):
    ok = {s: v for s, v in rows.items() if s not in AMBIGUOUS and s not in INDEX_ETF}
    top = sorted(ok, key=lambda s: -ok[s]["m"])[:HOT_TOP]
    surge = [s for s in sorted(ok, key=lambda s: -(ok[s]["rel"] * math.sqrt(ok[s]["m"])))
             if ok[s]["m"] >= 15 and ok[s]["rel"] >= 2 and s not in top][:HOT_SURGE]
    return top, surge


# ------------------------------------------------------------------ 2. headlines
def parse_rss(s, text):
    out = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        g = lambda k: (it.findtext(k) or "").strip()
        title, link = g("title"), g("link")
        if not title or not link:
            continue
        try:
            pub = email.utils.parsedate_to_datetime(g("pubDate")).astimezone(dt.timezone.utc)
        except (TypeError, ValueError):
            continue
        if (NOW - pub).total_seconds() > NEWS_HOURS * 3600:
            continue
        link = re.sub(r"[?&]\.tsrc=rss$", "", link)
        dom = re.sub(r"^www\.", "", re.sub(r"^https?://([^/]+).*$", r"\1", link))
        src = next((v for k, v in SRC.items() if dom.endswith(k)), dom)
        aid = g("guid") or link
        out.append({"id": re.sub(r"[^A-Za-z0-9-]", "", aid)[-40:] or str(abs(hash(link))), "t": title,
                    "d": re.sub(r"\s+", " ", g("description"))[:600], "url": link, "src": src,
                    "pub": pub.isoformat(timespec="minutes")})
    return out


def mentions(s, name, a):
    text = (a["t"] + " " + a["d"]).lower()
    if re.search(r"(?<![A-Za-z])\$?" + re.escape(s) + r"(?![A-Za-z])", a["t"] + " " + a["d"]):
        return True
    pat = EN.get(s)
    if not pat and name:
        w = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
        w = [x for x in w if x not in ("inc", "corp", "corporation", "the", "group", "holdings", "co", "ltd", "class", "common", "stock")]
        pat = re.escape(w[0]) if w and len(w[0]) >= 3 else None
    return bool(pat and re.search(pat, text))


def blocked(url):
    dom = re.sub(r"^www\.", "", re.sub(r"^https?://([^/]+).*$", r"\1", url))
    return any(dom == b or dom.endswith("." + b) for b in BLOCKED_SRC)


def news(tickers, names):
    arts, per = {}, {}
    for s in tickers:
        r = get(RSS.format(s=s), tries=2, timeout=30)
        if not r:
            per[s] = []
            continue
        items = [a for a in parse_rss(s, r.text) if mentions(s, names.get(s), a) and not blocked(a["url"])]
        items.sort(key=lambda a: a["pub"], reverse=True)
        per[s] = []
        for a in items[:PER_TICKER]:
            a = arts.setdefault(a["id"], dict(a, tk=[]))
            if s not in a["tk"]:
                a["tk"].append(s)
            per[s].append(a["id"])
        time.sleep(0.3)
    hit = sum(1 for v in per.values() if v)
    log("news tickers", len(tickers), "with headlines", hit, "articles", len(arts))
    if tickers and hit < len(tickers) * 0.3:
        diag("news", f"headlines found for only {hit} of {len(tickers)} tickers")
    return arts, per


# ------------------------------------------------------------------ 3. translation
def pick_gemini():
    """Google Gemini API free tier (no card on the Google project = no charges). Flash-Lite has the largest free daily quota."""
    try:
        r = requests.get(GM_BASE + "/models", headers={"Authorization": f"Bearer {GM_KEY}"}, timeout=60)
        r.raise_for_status()
        ids = [m["id"].split("/")[-1] for m in r.json().get("data", [])]
    except Exception as e:
        diag("translate", f"Gemini model list unavailable ({e})")
        return None
    def ver(i):
        m = re.search(r"gemini-(\d+)\.(\d+)", i)
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    lite = [i for i in ids if "flash-lite" in i and not re.search(r"preview|exp|image|tts|audio|live", i)]
    flash = [i for i in ids if re.search(r"gemini-[\d.]+-flash$", i)]
    ranked = sorted(lite, key=lambda i: (ver(i), -len(i)), reverse=True)[:2] + sorted(flash, key=ver, reverse=True)[:1]
    if not ranked:
        diag("translate", "no Gemini Flash model found")
        return None
    PROVIDER.update(base=GM_BASE, key=GM_KEY, name="gemini")
    return ranked


def pick_model():
    """Gemini free tier first; otherwise OpenRouter free models only (ids ending in ':free') - this site must not spend credits."""
    if GM_KEY:
        m = pick_gemini()
        if m:
            return m
    if not OR_KEY:
        diag("translate", "OPENROUTER_API_KEY not set - English headlines only")
        return None
    if OR_MODEL:
        return [OR_MODEL] if OR_MODEL.endswith(":free") or OR_MODEL == "openrouter/free" else None
    try:
        r = requests.get(OR_BASE + "/models", headers={"Authorization": f"Bearer {OR_KEY}"}, timeout=60)
        r.raise_for_status()
        free = [m for m in r.json().get("data", []) if m["id"].endswith(":free") and (m.get("context_length") or 0) >= 32000]
    except Exception as e:
        diag("translate", f"model list unavailable ({e}) - using the free router")
        return ["openrouter/free"]
    ranked = []
    for pref in FREE_PREFS:
        ranked += sorted([m["id"] for m in free if m["id"].startswith(pref) and m["id"] not in ranked], key=len)
    ranked += [m["id"] for m in free if m["id"] not in ranked]
    return ranked[:3] + ["openrouter/free"]          # tried in order when one is busy


def ask(models, system, user, max_tokens=8000, cap=MAX_CALLS):
    """models: free model ids in priority order; OpenRouter falls back to the next one when one is busy."""
    body = {"model": models[0], "models": models[:3], "temperature": 0.2, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"}}
    hdr = {"Authorization": f"Bearer {PROVIDER['key']}", "Content-Type": "application/json",
           "HTTP-Referer": "https://bluelagoon1222.github.io/Reddit-Issue-Korea/", "X-Title": "Reddit-Issue-Korea"}
    last = ""
    for attempt in range(4):
        if USAGE["req"] >= cap:
            raise RuntimeError("per-run request cap reached")
        USAGE["req"] += 1
        try:
            if PROVIDER["name"] == "gemini":
                body.pop("models", None)
                time.sleep(7)                     # free tier allows about 10 requests a minute
            r = requests.post(PROVIDER["base"] + "/chat/completions", headers=hdr, json=body, timeout=180)
        except requests.RequestException as e:
            last = str(e)
            time.sleep(5)
            continue
        if r.status_code == 400 and "response_format" in body:
            body.pop("response_format")
            continue
        if r.status_code == 429 and re.search(r"per.?day|daily", r.text.lower()) or r.status_code == 402:
            raise RuntimeError(f"HTTP {r.status_code}: daily free limit or credit limit")
        if r.status_code in (429, 500, 502, 503):
            last = f"HTTP {r.status_code}"
            if len(models) > 1:                       # busy: rotate to the next free model
                models[:] = models[1:] + models[:1]   # in place: later calls start with the next model
                body["model"], body["models"] = models[0], models[:3]
            time.sleep(15 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        js = r.json()
        u = js.get("usage") or {}
        USAGE["calls"] += 1
        USAGE["in"] += u.get("prompt_tokens") or 0
        USAGE["out"] += u.get("completion_tokens") or 0
        txt = (js["choices"][0]["message"].get("content") or "").strip()
        txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt)
        m = re.search(r"\{.*\}", txt, re.S)
        try:
            res = json.loads(m.group(0) if m else txt)
            USAGE["model"] = js.get("model") or models[0]
            return res
        except ValueError:                       # empty or broken answer (e.g. a reasoning model ran out of room)
            last = f"unreadable answer from {js.get('model') or models[0]}"
            if len(models) > 1:
                models[:] = models[1:] + models[:1]
                body["model"], body["models"] = models[0], models[:3]
            continue
    raise RuntimeError(f"request failed ({last})")


SYS_T = ("너는 미국 증시 기사를 한국 증권사 PB(프라이빗뱅커)에게 전달하는 번역가다. "
         "주어진 영문 제목과 RSS 발췌문만 근거로 쓴다. 발췌문에 없는 사실·숫자·전망을 절대 덧붙이지 않는다. "
         "숫자, 날짜, 회사명, 인명은 원문 그대로 정확히 옮긴다. 한국어 제목의 숫자는 영문 제목의 숫자와 같아야 한다. 회사명은 한국 언론 표기(예: 엔비디아, 테슬라)를 쓴다. "
         "광고성·일반론 기사(특정 종목과 관계없는 투자 상식, 종목 추천 목록 등)는 rel을 0으로 한다. 반드시 JSON만 출력한다.")


def translate(model, items, names, unknown):
    """Korean headline titles only (plus Korean company names for tickers without one)."""
    done, ko_names = {}, {}
    for i in range(0, len(items) or 1, BATCH):
        chunk = items[i:i + BATCH]
        ask_names = unknown if i == 0 else []
        if not chunk and not ask_names:
            break
        payload = {"articles": [{"id": a["id"], "tickers": [f"{s} ({names.get(s) or s})" for s in a["tk"]], "title": a["t"], "excerpt": a["d"][:200]} for a in chunk],
                   "companies": [{"ticker": s, "name": n} for s, n in ask_names]}
        user = ("articles의 각 기사마다:\n- ko_t: 영문 제목의 한국어 번역 (60자 이내, 자연스러운 기사 제목체, 숫자는 영문 제목과 동일)\n"
                "- rel: 해당 종목에 관한 실질적 뉴스면 1, 종목과 무관한 일반론·광고성 기사면 0 (excerpt는 판단에만 사용)\n"
                "companies의 각 종목마다:\n- ko: 한국 언론에서 쓰는 한국어 회사명\n"
                '출력 형식: {"items":[{"id":"...","ko_t":"...","rel":1}],"names":[{"ticker":"...","ko":"..."}]}\n\n'
                + json.dumps(payload, ensure_ascii=False))
        try:
            js = ask(model, SYS_T, user)
            for x in js.get("items") or []:
                if x.get("id") and x.get("ko_t"):
                    done[x["id"]] = {"ko_t": str(x["ko_t"]).strip(), "ko_s": None, "rel": 1 if x.get("rel") in (1, "1", True) else 0}
            for x in js.get("names") or []:
                if x.get("ticker") and x.get("ko"):
                    ko_names[str(x["ticker"]).upper()] = str(x["ko"]).strip()[:30]
        except Exception as e:
            diag("translate", f"batch failed: {e}")
            if i == 0 or "limit" in str(e) or "cap" in str(e):
                break
    log("translated", len(done), "of", len(items), "| names", len(ko_names))
    return done, ko_names


# ------------------------------------------------------------------ main
def main():
    DATA.mkdir(parents=True, exist_ok=True)
    conf = json.loads(CONF.read_text(encoding="utf-8"))
    names_ko = load("names.json", {})
    names_ko.setdefault("GOOG", "알파벳(구글)")
    for g in ("seohak", "korea"):
        for x in conf[g]:
            names_ko[x["s"]] = x["ko"]

    rows, mkt = reddit()
    top, surge = hot_list(rows)
    hot = top + surge
    seohak = [x["s"] for x in conf["seohak"]]
    korea = [x["s"] for x in conf["korea"]]
    tickers = list(dict.fromkeys(hot + seohak + korea))
    en_names = {s: rows.get(s, {}).get("name") for s in tickers}
    arts, per = news(tickers, en_names)

    cache = load("articles.json", {})
    cutoff = (NOW - dt.timedelta(days=KEEP_DAYS)).isoformat()
    cache = {k: v for k, v in cache.items() if v.get("pub", "") >= cutoff and not blocked(v.get("url", ""))}
    for k, a in arts.items():
        c = cache.get(k)
        if c:
            a.update({x: c.get(x) for x in ("ko_t", "ko_s", "rel")})
            a["tk"] = sorted(set(a["tk"]) | set(c.get("tk", [])))
        else:
            a.update({"ko_t": None, "ko_s": None, "rel": None})

    model = pick_model()
    if model:
        todo = sorted([a for a in arts.values() if a["ko_t"] is None], key=lambda a: a["pub"], reverse=True)[:MAX_TRANSLATE]
        unknown = [(s, en_names.get(s) or s) for s in hot if s not in names_ko][:20]
        res, ko_names = translate(model, todo, {s: names_ko.get(s) or en_names.get(s) or s for s in tickers}, unknown)
        for k, v in res.items():
            if k in arts:
                arts[k].update(v)
        names_ko.update(ko_names)
        log("model", model, "usage", USAGE)

    for k, a in arts.items():
        cache[k] = a
    save("articles.json", cache)
    save("names.json", names_ko)

    kmap = {x["s"]: x for x in conf["korea"]}
    tk = {}
    for s in tickers:
        rv = rows.get(s, {})
        ids = [i for i in per.get(s, []) if arts[i].get("rel") != 0][:4]
        tk[s] = {"s": s, "ko": names_ko.get(s), "en": rv.get("name") or None,
                 "m": rv.get("m"), "m24": rv.get("m24"), "rel": rv.get("rel"), "r": rv.get("r"), "r24": rv.get("r24"),
                 "kr": kmap.get(s, {}).get("kr"), "kwhy": kmap.get(s, {}).get("why"),
                 "arts": ids}
    used = {i for t in tk.values() for i in t["arts"]}
    recent = sorted([a for a in arts.values() if a.get("rel") != 0], key=lambda a: a["pub"], reverse=True)
    archive = [a["id"] for a in recent]
    used |= set(archive)
    out = {"status": "ok", "generated_at": NOW.isoformat(timespec="seconds"), "model": USAGE.get("model") or (model[0] if model else None), "usage": USAGE,
           "reddit_ok": bool(rows), "reddit_total": sum(v["m"] for v in rows.values()) if rows else None, "market_ratio": round(mkt, 3),
           "groups": {"top": top, "surge": surge, "seohak": seohak, "korea": korea}, "tickers": tk,
           "articles": {i: {k: arts[i].get(k) for k in ("t", "ko_t", "url", "src", "pub", "tk")} for i in used},
           "archive": archive, "diag": DIAG}
    save("issues.json", out)
    log("wrote data/issues.json | tickers", len(tk), "| articles", len(used), "| diag", len(DIAG))


if __name__ == "__main__":
    main()
