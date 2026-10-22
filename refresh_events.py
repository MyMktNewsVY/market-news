"""Refreshes data/events.json from official schedule pages. Run every 15 days."""
import json, re, sys, calendar, urllib.request
from datetime import date, datetime, timedelta
import collect

ARC = "https://www.mospi.gov.in/uploads/documents/releaseCalender/1770293210621-ADVANCE%20RELEASE%20CALENDAR%202026-27%20FINAL%2005.02.2026.pdf"
norm = lambda s: re.sub(r"\s+", " ", s.lower()).strip()

def page_text(url):
    raw = collect.fetch(url).decode("utf-8", "ignore")
    return collect.clean(re.sub(r"(?is)<(script|style).*?</\1>", " ", raw))[:25000]

def valid(ev, text, today):
    try: d = date.fromisoformat(ev["date"])
    except Exception: return False
    ev_ = norm(ev.get("evidence", ""))
    if not (today <= d <= today + timedelta(days=450)) or len(ev_) < 10 or ev_ not in text: return False
    if not re.search(r"\b%d\b" % d.day, ev_): return False
    return calendar.month_name[d.month].lower() in ev_ or calendar.month_abbr[d.month].lower() in ev_

def india_cpi(today):
    out = []
    for k in range(1, 8):
        y, m = (today.year + (today.month - 1 + k) // 12, (today.month - 1 + k) % 12 + 1)
        d = date(y, m, 12)
        if d > date(2027, 3, 12): break
        out.append({"name": "India CPI inflation", "date": d.isoformat(), "why": "India retail inflation. Key input for RBI rate expectations.", "url": ARC, "verified": True,
                    "note": "MoSPI advance release calendar 2026-27 lists the 12th of each month. Check MoSPI if it falls on a weekend.", "source_id": "in_cpi"})
    return out

def main():
    today = datetime.now(collect.IST).date(); ts = today.isoformat()
    data = json.load(open("data/events.json")); status = data.get("sources_status", {}); events = data.get("events", []); refreshed = set()
    for s in json.load(open("events_sources.json")):
        try:
            text = norm(page_text(s["url"]))
            res = collect.gemini_json("From the official page text below, list scheduled events of this kind: " + s["what"] +
                ' Return ONLY JSON {"events":[{"date":"YYYY-MM-DD","evidence":"the exact words copied from the text that state this date"}]}. '
                "Use only dates stated in the text and never guess. Skip dates before " + ts + ".\n\nTEXT:\n" + text)
            good = [e for e in res.get("events", []) if valid(e, text, today)]
            if not good: raise ValueError("no date could be verified from the page text")
            old = [e for e in events if e.get("source_id") == s["id"] and e["date"] >= ts]
            if len(good) < 0.6 * len(old): raise ValueError(f"found {len(good)} dates but {len(old)} were listed before, so the old ones were kept")
            new = [{"name": s["name"], "date": e["date"], "time": s["time"], "why": s["why"], "url": s["url"], "verified": True,
                    "note": "From the official page: " + e["evidence"][:140], "source_id": s["id"], "checked_on": ts} for e in good]
            events = [e for e in events if e.get("source_id") != s["id"]] + new; refreshed.add(s["id"]); status[s["id"]] = {"ok": True, "checked_on": ts}
            print("OK  ", s["id"], len(new), "dates")
        except Exception as e:
            status[s["id"]] = {**status.get(s["id"], {}), "ok": False, "error": str(e)[:100]}; print("FAIL", s["id"], e)
    cpi = india_cpi(today)
    if cpi: events = [e for e in events if e.get("source_id") != "in_cpi"] + [{**c, "checked_on": ts} for c in cpi]; refreshed.add("in_cpi"); status["in_cpi"] = {"ok": True, "checked_on": ts}
    events = sorted([e for e in events if e["date"] >= (today - timedelta(days=1)).isoformat()], key=lambda e: e["date"])
    data.update({"events": events, "sources_status": status, "verified_on": ts if refreshed else data.get("verified_on", "")})
    json.dump(data, open("data/events.json", "w"), indent=1, ensure_ascii=False); print("Events saved:", len(events))

if __name__ == "__main__": main()
