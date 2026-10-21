"""Indian Market News Agent - daily collector. Standard library only."""
import json, os, re, sys, difflib, urllib.request, email.utils, html
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
MODEL = os.environ.get("GEMINI_MODEL", "").strip()
KEY = os.environ.get("GEMINI_API_KEY", "")
MAX_STORIES, MAX_CANDIDATES = 15, 110
BANNED = re.compile(r"\b(buy|sell|target price|guaranteed)\b", re.I)

def log(*a): print(*a, flush=True)

def window(now=None):
    now = (now or datetime.now(IST)).astimezone(IST)
    cutoff = now.replace(hour=8, minute=0, second=0, microsecond=0)
    start = (cutoff - timedelta(days=1)).replace(hour=15)
    return start, cutoff, now

def clean(t): return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t or ""))).strip()

def parse_time(s):
    if not s: return None
    try:
        d = email.utils.parsedate_to_datetime(s.strip())
    except Exception:
        try: d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except Exception: return None
    if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(IST)

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (personal news reader)"})
    with urllib.request.urlopen(req, timeout=25) as r: return r.read()

def parse_feed(raw):
    try: root = ET.fromstring(raw)
    except ET.ParseError:
        txt = raw.decode("utf-8", "ignore")
        txt = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", txt)
        txt = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)", "&amp;", txt)
        try: root = ET.fromstring(txt.encode("utf-8"))
        except ET.ParseError: return parse_loose(raw)
    return read_items(root)

def parse_loose(raw):
    txt = raw.decode("utf-8", "ignore"); out = []
    def tag(block, name):
        m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S | re.I)
        return re.sub(r"^<!\[CDATA\[|\]\]>$", "", m.group(1).strip()) if m else ""
    for b in re.findall(r"<item[ >].*?</item>", txt, re.S | re.I):
        out.append({"title": clean(tag(b, "title")), "link": tag(b, "link"), "time": parse_time(tag(b, "pubDate")),
                    "snippet": clean(tag(b, "description"))[:400]})
    if not out: raise ValueError("feed unreadable")
    return out

def read_items(root):
    out = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"): continue
        g = {c.tag.split("}")[-1]: c for c in it}
        link = ""
        if "link" in g: link = (g["link"].text or "").strip() or g["link"].attrib.get("href", "")
        t = next((g[k].text for k in ("pubDate", "published", "updated", "date") if k in g and g[k].text), None)
        desc = next((g[k].text for k in ("description", "summary", "content") if k in g and g[k].text), "")
        out.append({"title": clean(g["title"].text if "title" in g else ""), "link": link,
                    "time": parse_time(t), "snippet": clean(desc)[:400]})
    return out

def collect(sources, start, cutoff, seen, bootstrap, new_seen):
    items, status = [], {}
    for s in sources:
        try:
            feed = parse_feed(fetch(s["url"]))
            n_all, n_in, n_notime = len(feed), 0, 0
            ts = [f["time"] for f in feed if f["time"]]
            rng = f", newest {max(ts).strftime('%d %b %H:%M')}, oldest {min(ts).strftime('%d %b %H:%M')}" if ts else ""
            for f in feed:
                if not f["title"] or not f["link"]: continue
                if f["time"] is None:
                    n_notime += 1
                    if s["kind"] == "official":
                        new_seen.add(f["link"])
                        if not bootstrap and f["link"] not in seen:
                            n_in += 1; items.append({**f, "source": s["name"], "kind": s["kind"]})
                    continue
                if start <= f["time"] <= cutoff:
                    n_in += 1; items.append({**f, "source": s["name"], "kind": s["kind"]})
            status[s["name"]] = {"ok": True, "total": n_all, "in_window": n_in, "no_time": n_notime}
            log(f"OK   {s['name']}: {n_all} items, {n_in} in window, {n_notime} without time{rng}")
        except Exception as e:
            status[s["name"]] = {"ok": False, "error": str(e)[:120]}
            log(f"FAIL {s['name']}: {e}")
    return items, status

def group(items):
    now = datetime.now(IST)
    for it in items: it["ts"] = it["time"] or now
    items.sort(key=lambda x: x["ts"]); groups = []
    for it in items:
        key = it["title"].lower()
        for g in groups:
            if difflib.SequenceMatcher(None, key, g["title"].lower()).ratio() > 0.62:
                g["members"].append(it); break
        else: groups.append({"title": it["title"], "members": [it]})
    for g in groups:
        srcs = {m["source"] for m in g["members"]}
        g["official"] = any(m["kind"] == "official" for m in g["members"])
        g["n_sources"] = len(srcs)
        g["label"] = ("Official source" if g["official"] else
                      "Confirmed by 2 or more outlets" if len(srcs) > 1 else
                      "Single source, not independently confirmed")
    groups.sort(key=lambda g: (-g["official"], -g["n_sources"], g["members"][0]["ts"]))
    return groups[:MAX_CANDIDATES]

def pick_models():
    if MODEL: return [MODEL]
    req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", headers={"x-goog-api-key": KEY})
    with urllib.request.urlopen(req, timeout=30) as r: models = json.load(r).get("models", [])
    def ver(n):
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n); return float(m.group(1)) if m else 0
    for bad in (("lite","image","tts","audio","live","thinking","exp","preview","8b","latest"), ("lite","image","tts","audio","live","8b")):
        c = [m["name"].split("/")[-1] for m in models if "generateContent" in m.get("supportedGenerationMethods", [])
             and "flash" in m["name"] and not any(b in m["name"] for b in bad)]
        if c: c.sort(key=lambda n: (ver(n), n), reverse=True); return c[:4]
    raise RuntimeError("No Gemini flash model available for this key")

def ask_gemini(groups, names):
    lines = []
    for i, g in enumerate(groups):
        m = g["members"][0]
        lines.append(f"[{i}] ({g['label']}; {m['source']}) {g['title']} :: {m['snippet']}")
    prompt = ("You help an Indian equity trader understand news affecting the Nifty 50.\n"
      "From the numbered items, choose up to 15 that are genuinely material for Indian markets, ranked by credibility, "
      "surprise vs expectations, time sensitivity and likely effect. Fewer is fine. Skip trivia and weak rumours.\n"
      "RULES: use ONLY facts in the item text; invent no numbers, dates, quotes or consensus; if an item has only a headline, say so. "
      "Never give buy/sell advice or price targets. Use cautious wording (may support, could pressure, mixed, impact uncertain). "
      "Summary: 4-5 short lines, plain English, facts only. Impact: 1-2 lines of possible effect, clearly an interpretation.\n"
      "Factors are numbers 1-36 from this list: " + "; ".join(f"{i+1} {n}" for i, n in enumerate(names)) + "\n"
      'Return ONLY JSON: {"quick_read":"3-4 lines: supportive factors, pressures, uncertainties (not a forecast)",'
      '"stories":[{"id":0,"factors":[1],"summary":"...","impact":"...","sectors":["Banks"]}]}\n\nITEMS:\n' + "\n".join(lines))
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}}).encode()
    last = None
    for model in pick_models():
        log("Trying Gemini model:", model)
        req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=body, headers={"Content-Type": "application/json", "x-goog-api-key": KEY})
        try:
            with urllib.request.urlopen(req, timeout=100) as r: resp = json.load(r)
            out = json.loads(resp["candidates"][0]["content"]["parts"][0]["text"])
            log("Gemini answered using", model); return out
        except Exception as e:
            last = e; log(f"  {model} failed: {e}")
    raise RuntimeError(f"All Gemini models failed: {last}")

def main():
    cfg = json.load(open("sources.json"))["sources"]
    names = json.load(open("factors.json"))
    start, cutoff, now = window()
    log("Window:", start.isoformat(), "->", cutoff.isoformat())
    seen_path = "data/seen.json"
    bootstrap = not os.path.exists(seen_path)
    seen = set(json.load(open(seen_path))) if not bootstrap else set()
    new_seen = set()
    items, status = collect(cfg, start, cutoff, seen, bootstrap, new_seen)
    if bootstrap: log("First run: remembering current SEBI/PIB items; only new ones will appear from the next run.")
    if not any(v["ok"] for v in status.values()):
        log("No source could be reached. Keeping the previous report."); sys.exit(1)
    groups = group(items); log(f"{len(items)} items -> {len(groups)} distinct stories")
    gaps = [f"Source unavailable: {k}" for k, v in status.items() if not v["ok"]]
    gaps += [f"{k}: {v['no_time']} items skipped because no publish time was given" for k, v in status.items() if v.get("no_time")]
    if bootstrap: gaps.append("First run: SEBI and PIB publish no times, so only items new since the previous run are shown from now on.")
    elif any(m["time"] is None for g in groups for m in g["members"]): gaps.append("SEBI/PIB items without a publish time are shown as new since the last run; time not provided by the source.")
    gaps.append("Not covered yet: India VIX, PCR, options and futures open interest, breadth and delivery data, exchange filings, heavyweight earnings calendar.")
    stories, quick, ai_ok = [], "", False
    if groups and KEY:
        try:
            res = ask_gemini(groups, names); quick = clean(res.get("quick_read", "")); ai_ok = True
            for s in res.get("stories", [])[:MAX_STORIES]:
                g = groups[s["id"]]; txt = s.get("summary", "") + " " + s.get("impact", "")
                if BANNED.search(txt): gaps.append(f"Removed AI text with trading language: {g['title'][:60]}"); s["impact"] = "Explanation withheld."
                stories.append({"headline": g["title"], "time": (next((m["time"] for m in g["members"] if m["time"]), None) or None) and next(m["time"] for m in g["members"] if m["time"]).isoformat(),
                  "factors": [names[f-1] for f in s.get("factors", []) if 1 <= f <= 36],
                  "factor_ids": [f for f in s.get("factors", []) if 1 <= f <= 36],
                  "summary": s.get("summary", ""), "confirmation": g["label"], "impact": s.get("impact", ""),
                  "sectors": s.get("sectors", []),
                  "sources": [{"name": m["source"], "url": m["link"]} for m in g["members"][:3]]})
        except Exception as e:
            log("AI step failed:", e); gaps.append("AI summaries unavailable this run. Showing headlines only.")
    elif groups:
        gaps.append("No Gemini key found. Showing headlines only.")
    if not ai_ok:
        for g in groups[:MAX_STORIES]:
            m = g["members"][0]
            stories.append({"headline": g["title"], "time": m["time"].isoformat() if m["time"] else None, "factors": [], "factor_ids": [],
              "summary": m["snippet"] or "Headline only.", "confirmation": g["label"],
              "impact": "AI explanation not available for this story.", "sectors": [],
              "sources": [{"name": x["source"], "url": x["link"]} for x in g["members"][:3]]})
    tagged = {f for s in stories for f in s["factor_ids"]}
    covered = {}
    for s in cfg:
        if status[s["name"]]["ok"]:
            for f in s["factors"]: covered[f] = True
    fs = {str(i): ("found" if i in tagged else "none" if covered.get(i) else "unverified") for i in range(1, 37)}
    if not ai_ok:
        fs = {k: ("unverified" if v == "none" else v) for k, v in fs.items()}
    date = cutoff.strftime("%Y-%m-%d")
    rep = {"status": "ok" if ai_ok else "partial", "title": f"Indian Market News Brief — {cutoff.strftime('%d %b %Y')} — Nifty 50",
           "generated_at": now.isoformat(), "window_start": start.isoformat(), "cutoff": cutoff.isoformat(),
           "quick_read": quick or "AI summary not available for this run.", "stories": stories,
           "factor_status": fs, "gaps": gaps, "sources_checked": status}
    os.makedirs("data/reports", exist_ok=True)
    for p in ("data/latest.json", f"data/reports/{date}.json"):
        json.dump(rep, open(p, "w"), ensure_ascii=False, indent=1)
    idx = json.load(open("data/index.json")) if os.path.exists("data/index.json") else {"reports": []}
    idx["reports"] = [r for r in idx["reports"] if r["date"] != date] + [{"date": date, "file": f"data/reports/{date}.json"}]
    idx["reports"].sort(key=lambda r: r["date"], reverse=True)
    json.dump(idx, open("data/index.json", "w"), indent=1)
    json.dump(sorted((seen | new_seen))[-3000:], open(seen_path, "w"))
    log(f"Report written: {len(stories)} stories, AI={'yes' if ai_ok else 'no'}")

if __name__ == "__main__": main()
