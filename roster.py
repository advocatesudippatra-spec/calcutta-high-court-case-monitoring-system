"""
Master roster: the whole cause list (every court, every item) in a small database,
plus the live display board, and a plain-words question answerer for Telegram.

  index_list(date, side)   read a list PDF into the database (about 3 seconds)
  answer(text)             reply to a question such as
                           "group 6", "justice a b ghosh", "court 35", "mentioning 25",
                           "fixed 35", "find WPA/1234/2026", "advocate a k sen",
                           "running 12", "board", "my courts", "tomorrow court 18"
  save_board(rows)         store a board reading sent by the Board Watcher

Nothing here needs the internet except downloading a list that is not cached yet.
The database is never trimmed automatically: it grows by about 2 MB per court day and is
deleted only on request (casemonitor forget-database).
"""
import datetime as dt
import html
import json
import os
import re
import sqlite3
import time

import fitz  # PyMuPDF

import causelist as cl

HOME = os.environ.get("CASEMONITOR_HOME") or os.path.expanduser("~/.casemonitor")
DB = os.path.join(HOME, "roster.db")
CACHE = os.path.join(HOME, "cache")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII", 8: "VIII", 9: "IX", 10: "X",
         11: "XI", 12: "XII", 13: "XIII", 14: "XIV", 15: "XV"}
SIDE_WORDS = {"A": ("APPELLATE",), "O": ("ORIGINAL",), "J": ("JALPAIGURI", "JALPAIGURI"), "P": ("PORT BLAIR", "PORTBLAIR")}


def _e(s):
    return html.escape(str(s or ""), quote=False)


def now_ist():
    return dt.datetime.now(IST)


def dstr(d):
    return d.strftime("%d%m%Y")


def pretty(d):
    return "%s-%s-%s" % (d[:2], d[2:4], d[4:])


# ------------------------------------------------------------------ database
def db():
    os.makedirs(HOME, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS lists(date TEXT, side TEXT, kind TEXT, indexed REAL, courts INT, items INT,
        PRIMARY KEY(date, side, kind));
    CREATE TABLE IF NOT EXISTS courts(date TEXT, side TEXT, kind TEXT, court TEXT, bench TEXT, time TEXT,
        judges TEXT, determination TEXT, notes TEXT, plan TEXT, vc TEXT, items INT,
        PRIMARY KEY(date, side, kind, court));
    CREATE TABLE IF NOT EXISTS items(date TEXT, side TEXT, kind TEXT, court TEXT, serial INT, tagged INT,
        case_no TEXT, parties TEXT, advocates TEXT, section TEXT, fixed INT);
    CREATE INDEX IF NOT EXISTS items_dc ON items(date, side, court, serial);
    CREATE TABLE IF NOT EXISTS board(t REAL, side TEXT, court TEXT, serial TEXT, detail TEXT, daily INT,
        judges TEXT, fetched TEXT);
    CREATE INDEX IF NOT EXISTS board_c ON board(court, t);
    CREATE TABLE IF NOT EXISTS board_now(side TEXT, court TEXT, serial TEXT, detail TEXT, daily INT,
        judges TEXT, fetched TEXT, t REAL, PRIMARY KEY(side, court));
    """)
    return con


def forget(before=None):
    """Delete the stored roster: everything, or only lists dated before `before` (DDMMYYYY).
    Only ever called when you ask for it (casemonitor forget-database)."""
    con = db()
    if before is None:
        con.close()
        os.remove(DB)
        return "all"
    cut = before[4:] + before[2:4] + before[:2]
    old = [r["date"] for r in con.execute("SELECT DISTINCT date FROM lists") if r["date"][4:] + r["date"][2:4] + r["date"][:2] < cut]
    for d in old:
        for t in ("lists", "courts", "items"):
            con.execute("DELETE FROM %s WHERE date=?" % t, (d,))
    con.commit()
    con.execute("VACUUM")
    return "%d day(s)" % len(old)


def index_pdf(con, path, date, side, kind="daily"):
    doc = fitz.open(path)
    con.execute("DELETE FROM courts WHERE date=? AND side=? AND kind=?", (date, side, kind))
    con.execute("DELETE FROM items WHERE date=? AND side=? AND kind=?", (date, side, kind))
    p, nc, ni = 0, 0, 0
    while p < len(doc):
        if cl.bench_id(doc[p]) is None or "COURT NO." not in doc[p].get_text():
            p += 1
            continue
        s, e = cl.court_page_range(doc, p)
        h = cl.parse_header(cl.page_lines(doc[s]))
        its = cl.parse_items(doc, s, e)
        plan = cl.day_plan(its, h, date, side)
        con.execute("INSERT OR REPLACE INTO courts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            date, side, kind, h["court_no"], h["bench"], h["time"], " & ".join(h["judges"]), h["determination"],
            h["notes"], json.dumps({"lines": cl.plan_lines(plan), "code": cl.plan_code(plan),
                                    "unread": plan["tt"]["unread"], "sit": plan["sit"]}), h["vc_link"], len(its)))
        con.executemany("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
            (date, side, kind, h["court_no"], i["serial"], int(i["tagged"]), i["case_no"], i["case_name"],
             i["advocates_text"], i["section"], i.get("fixed")) for i in its])
        nc += 1
        ni += len(its)
        p = e + 1
    con.execute("INSERT OR REPLACE INTO lists VALUES(?,?,?,?,?,?)", (date, side, kind, time.time(), nc, ni))
    con.commit()
    return nc, ni


def index_list(date, side="A", refresh=False, con=None):
    """Make sure the list of that date/side (daily + supplementary) is in the database.
    Returns number of courts, or None if not published."""
    con = con or db()
    have = con.execute("SELECT courts FROM lists WHERE date=? AND side=? AND kind='daily'", (date, side)).fetchone()
    if have and not refresh:
        return have["courts"]
    path = cl.download(date, side, CACHE, refresh=refresh)
    if not path:
        return None
    nc, _ = index_pdf(con, path, date, side, "daily")
    for kind in cl.SUP_KINDS:
        sp = os.path.join(CACHE, kind, os.path.basename(cl.pdf_url(date, side, kind)))
        if not os.path.exists(sp) and not cl.exists(date, side, kind):
            break
        sp = cl.download(date, side, CACHE, refresh=refresh, kind=kind)
        if sp:
            index_pdf(con, sp, date, side, kind)
    return nc


# ------------------------------------------------------------------ live board
def save_board(rows, fetched=""):
    """rows: [{room, serialText, side, daily, judges, detail}] from the Board Watcher."""
    con = db()
    t = time.time()
    for r in rows:
        key = (r.get("side", "A"), str(r.get("room", "")))
        old = con.execute("SELECT serial, daily FROM board_now WHERE side=? AND court=?", key).fetchone()
        vals = (key[0], key[1], r.get("serialText", ""), (r.get("detail") or "")[:300], int(bool(r.get("daily", True))),
                (r.get("judges") or "")[:200], fetched, t)
        con.execute("INSERT OR REPLACE INTO board_now VALUES(?,?,?,?,?,?,?,?)", vals)
        if not old or old["serial"] != vals[2] or old["daily"] != vals[4]:
            con.execute("INSERT INTO board VALUES(?,?,?,?,?,?,?,?)", (t, key[0], key[1], vals[2], vals[3], vals[4], vals[5], fetched))
    con.commit()
    return len(rows)


def board_age(con):
    r = con.execute("SELECT MAX(t) m FROM board_now").fetchone()
    return (time.time() - r["m"]) if r and r["m"] else None


# ------------------------------------------------------------------ question parsing helpers
def _date_from(text, base=None):
    base = base or now_ist().date()
    t = text.upper()
    if "DAY AFTER" in t:
        return base + dt.timedelta(days=2)
    if "TOMORROW" in t:
        d = base + dt.timedelta(days=1)
        return d
    if "YESTERDAY" in t:
        return base - dt.timedelta(days=1)
    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b", t)
    if m:
        y = int(m.group(3)) if m.group(3) else base.year
        y = y + 2000 if y < 100 else y
        try:
            return dt.date(y, int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    for i, wd in enumerate(("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY")):
        if re.search(r"\b%s\b" % wd, t):
            d = base
            while d.weekday() != i:
                d += dt.timedelta(days=1)
            return d
    return base


def _side_from(text):
    t = text.upper()
    if "ORIGINAL" in t or re.search(r"\bOS\b", t):
        return "O"
    if "JALPAIGURI" in t or re.search(r"\bJAL\b", t):
        return "J"
    if "PORT BLAIR" in t:
        return "P"
    return "A"


def _court_from(text, loose=False):
    t = text.upper()
    m = re.search(r"\b(?:COURT|CT|ROOM|CR)\s*(?:NO\.?\s*)?(\d{1,3})\b", t)
    if m:
        return m.group(1)
    m = re.fullmatch(r"\s*(\d{1,3})\s*", t)
    if m:
        return m.group(1)
    if loose:  # "mentioning 25", "running 12": a bare number that is not part of a case number
        t = re.sub(r"[A-Z][A-Z.() ]{1,12}\s*/\s*\d{1,6}\s*/\s*\d{4}", " ", t)
        m = re.search(r"(?<![/\d])\b(\d{1,3})\b(?![/\d])", t)
        return m.group(1) if m else None
    return None


def _group_re(n):
    r = ROMAN.get(n, str(n))
    return re.compile(r"GROUP\s*[-–:]?\s*(?:\(?\s*)?(?:%s|%d)(?![IVX0-9])" % (r, n))


def _snip(text, pat, width=150):
    m = pat.search(text) if hasattr(pat, "search") else re.search(pat, text)
    if not m:
        return text[:width]
    a = max(0, m.start() - 60)
    s = text[a:m.end() + width - 60]
    return ("…" if a else "") + s.strip() + ("…" if m.end() + width - 60 < len(text) else "")


def _ensure(con, date, side):
    ds = dstr(date)
    n = index_list(ds, side, con=con)
    return ds, n


def _next_list_date(con, side, base):
    """The list date to use when none is named: today until 5 PM, then the next published list."""
    d = base
    if now_ist().hour >= 17 or d.weekday() >= 5:
        d = d + dt.timedelta(days=1)
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
    return d


def _courts(con, ds, side):
    return con.execute("SELECT * FROM courts WHERE date=? AND side=? AND kind='daily' ORDER BY CAST(court AS INT)",
                       (ds, side)).fetchall()


def _court_line(c):
    return "<b>Court %s</b> · %s · %s · %d items" % (_e(c["court"]), _e(c["judges"].title()), _e(c["time"] or "time -"), c["items"])


def _board_for(con, side, court):
    return con.execute("SELECT * FROM board_now WHERE side=? AND court=?", (side, court)).fetchone()


def _item_at(con, ds, side, court, serial):
    return con.execute("SELECT * FROM items WHERE date=? AND side=? AND court=? AND serial=? ORDER BY kind LIMIT 1",
                       (ds, side, court, serial)).fetchone()


def _serial_num(s):
    m = re.search(r"\d+", s or "")
    return int(m.group(0)) if m else None


def _live_line(con, ds, side, court, plan_code=None):
    b = _board_for(con, side, court)
    if not b:
        return None
    age = (time.time() - b["t"]) / 60
    n = _serial_num(b["serial"])
    it = _item_at(con, ds, side, court, n) if (n and b["daily"]) else None
    sec = ""
    if it:
        sec = " · %s" % it["section"].title()
    txt = "📺 Running <b>%s</b>%s%s (board %s%s)" % (
        _e(b["serial"]), "" if b["daily"] else " (not the daily list)", _e(sec), _e(b["fetched"] or ""),
        ", %d min old" % age if age > 2 else "")
    if it:
        txt += "\n    %s  %s" % (_e(it["case_no"]), _e((it["parties"] or "")[:70]))
    return txt


# ------------------------------------------------------------------ answers
def ans_group(con, ds, side, n):
    pat = _group_re(n)
    hits = [c for c in _courts(con, ds, side) if pat.search(c["determination"].upper())]
    if not hits:
        return "No court's determination on %s mentions Group-%s (%s)." % (pretty(ds), ROMAN.get(n, n), side_name(side))
    out = ["<b>Group-%s (%s), %s: %d court(s)</b>" % (ROMAN.get(n, n), side_name(side), pretty(ds), len(hits))]
    for c in hits:
        out.append("\n" + _court_line(c) + "\n<i>%s</i>" % _e(_snip(c["determination"].upper(), pat)))
    return "\n".join(out)


def ans_topic(con, ds, side, words):
    """Courts whose determination / headings mention all the words."""
    ws = [w for w in re.findall(r"[A-Z0-9]{3,}", words.upper()) if w not in ("WHICH", "COURT", "COURTS", "TAKING",
                                                                            "TAKES", "TAKE", "MATTER", "MATTERS", "WHERE", "TODAY", "TOMORROW", "LIST", "THE", "ARE", "WHAT")]
    if not ws:
        return None
    hits = []
    for c in _courts(con, ds, side):
        det = c["determination"].upper()
        secs = " ".join(r["section"] for r in con.execute(
            "SELECT DISTINCT section FROM items WHERE date=? AND side=? AND court=?", (ds, side, c["court"]))).upper()
        where = [x for x, txt in (("determination", det), ("headings", secs)) if all(w[:6] in txt for w in ws)]
        if where:
            hits.append((c, where, det))
    if not hits:
        return None
    out = ["<b>%s (%s), %s: %d court(s)</b>" % (_e(" ".join(ws).title()), side_name(side), pretty(ds), len(hits))]
    for c, where, det in hits[:25]:
        extra = ("\n<i>%s</i>" % _e(_snip(det, re.escape(ws[0][:6])))) if "determination" in where else "  (in its headings)"
        out.append("\n" + _court_line(c) + extra)
    if len(hits) > 25:
        out.append("\n…and %d more." % (len(hits) - 25))
    return "\n".join(out)


def ans_court(con, ds, side, court, focus=""):
    c = con.execute("SELECT * FROM courts WHERE date=? AND side=? AND kind='daily' AND court=?", (ds, side, court)).fetchone()
    if not c:
        return "Court %s is not in the %s list for %s." % (court, side_name(side), pretty(ds))
    plan = json.loads(c["plan"] or "{}")
    notes, det = c["notes"] or "", c["determination"] or ""
    if focus == "mention":
        sents = [s.strip() for s in re.split(r"(?<=\.)\s+|\s(?=\d\s*[.):])", notes + " " + det) if "MENTION" in s.upper()]
        if not sents:
            return ("Court %s (%s), %s: <b>mentioning is not stated</b> in the cause list for this court sitting."
                    % (court, _e(c["judges"].title()), pretty(ds)))
        return "<b>Mentioning, Court %s</b> (%s), %s:\n%s" % (court, _e(c["judges"].title()), pretty(ds),
                                                           "\n".join("• " + _e(s) for s in sents))
    if focus == "fixed":
        fx = con.execute("SELECT serial, case_no, parties, fixed FROM items WHERE date=? AND side=? AND court=? "
                         "AND fixed IS NOT NULL ORDER BY serial", (ds, side, court)).fetchall()
        fsecs = [l for l in plan.get("lines", []) if "FIXED" in l.upper() or "AT " in l.upper()]
        fnotes = [s.strip() for s in re.split(r"(?<=\.)\s+", notes) if "FIXED" in s.upper()]
        if not (fx or fsecs or fnotes):
            return "Court %s, %s: no fixed items or fixed hearings in the cause list." % (court, pretty(ds))
        out = ["<b>Fixed matters, Court %s</b> (%s), %s" % (court, _e(c["judges"].title()), pretty(ds))]
        out += ["• Item %d at %s: %s %s" % (r["serial"], cl._fmt(r["fixed"]), _e(r["case_no"]), _e((r["parties"] or "")[:60])) for r in fx]
        out += ["• Heading: %s" % _e(l) for l in fsecs]
        out += ["• Note: %s" % _e(s) for s in fnotes]
        return "\n".join(out)
    out = [_court_line(c) + " · %s" % _e(side_name(side)), "<i>%s</i>" % _e(c["bench"])]
    live = _live_line(con, ds, side, court)
    if live and ds == dstr(now_ist().date()):
        out.append(live)
    out.append("\n<b>Determination:</b> %s" % _e(det[:500] + ("…" if len(det) > 500 else "")))
    if notes:
        out.append("<b>Note:</b> %s" % _e(notes[:600] + ("…" if len(notes) > 600 else "")))
    if plan.get("lines"):
        out.append("\n<b>Day plan (%s):</b>\n%s" % (pretty(ds), "\n".join("• " + _e(l) for l in plan["lines"])))
    if plan.get("unread"):
        out.append("<i>Note not fully understood: %s</i>" % _e("; ".join(plan["unread"])[:250]))
    if c["vc"] and c["vc"].startswith("http"):
        out.append('<a href="%s">VC link</a>' % _e(c["vc"]))
    return "\n".join(out)


def ans_justice(con, ds, side, name):
    ws = [w for w in re.findall(r"[A-Z]{3,}", name.upper()) if w not in ("JUSTICE", "HON", "BLE", "WHAT", "WHICH", "TAKING",
                                                                         "TAKES", "MATTERS", "KIND", "DOES", "THE", "JUDGE", "LORDSHIP")]
    if not ws:
        return None
    hits = [c for c in _courts(con, ds, side) if all(w in c["judges"].upper() for w in ws)]
    if not hits:
        return None
    out = []
    for c in hits:
        secs = con.execute("SELECT section, COUNT(*) n FROM items WHERE date=? AND side=? AND court=? GROUP BY section "
                           "ORDER BY MIN(serial)", (ds, side, c["court"])).fetchall()
        out.append(_court_line(c) + "\n<b>Takes:</b> %s\n<b>Headings today:</b> %s" % (
            _e(c["determination"][:450] + ("…" if len(c["determination"]) > 450 else "")),
            _e(", ".join("%s (%d)" % (s["section"].title(), s["n"]) for s in secs))))
        if c["notes"]:
            out.append("<b>Note:</b> %s" % _e(c["notes"][:300]))
    return ("<b>%s, %s</b>\n" % (_e(" ".join(ws).title()), pretty(ds))) + "\n\n".join(out)


def ans_find(con, ds, side, q, field="any", names=None):
    """Find a case number, party or advocate in the list of that date (all sides already indexed)."""
    qq = re.sub(r"\s+", " ", q.upper()).strip()
    params = [ds]
    m = re.search(r"\b([A-Z][A-Z.() ]{1,12})\s*/\s*(\d{1,6})\s*/\s*(\d{4})\b", qq)
    num = re.fullmatch(r"\d{2,6}(?:\s*/\s*\d{4})?", qq)
    if m:
        where, params = "REPLACE(case_no,' ','') LIKE ?", [ds, "%%%s/%s/%s%%" % (m.group(1).replace(" ", ""), m.group(2), m.group(3))]
    elif num:
        n = qq.replace(" ", "")
        where, params = "REPLACE(case_no,' ','') LIKE ?", [ds, "%%/%s%%" % n]
    else:
        col = {"advocate": "advocates", "party": "parties"}.get(field, None)
        words = [w for w in re.findall(r"[A-Z0-9]{2,}", qq)]
        if not words:
            return None
        cols = [col] if col else ["parties", "advocates"]
        where = " AND ".join("(%s)" % " OR ".join("%s LIKE ?" % c_ for c_ in cols) for _ in words)
        params = [ds] + [p for w in words for p in ["%%%s%%" % w] * len(cols)]
    rows = con.execute("SELECT * FROM items WHERE date=? AND (%s) ORDER BY side, CAST(court AS INT), serial LIMIT 60" % where,
                       params).fetchall()
    if not rows:
        return "Nothing found for <b>%s</b> in the lists of %s." % (_e(q), pretty(ds))
    out = ["<b>%s</b>, %s: %d item(s)%s" % (_e(q), pretty(ds), len(rows), " (first 60)" if len(rows) == 60 else "")]
    cache = {}
    for r in rows[:30]:
        key = (r["side"], r["court"])
        if key not in cache:
            cache[key] = con.execute("SELECT * FROM courts WHERE date=? AND side=? AND kind='daily' AND court=?",
                                     (ds, r["side"], r["court"])).fetchone()
        c = cache[key]
        lk = likelihood(con, ds, r)
        out.append("\n%s <b>%s Court %s</b>, item %s%s  %s\n%s\n<i>%s</i>%s" % (
            lk[0], side_name(r["side"]), _e(r["court"]), "wt " if r["tagged"] else "", r["serial"], _e(r["case_no"]),
            _e((r["parties"] or "")[:80]), _e((r["section"] or "").title()),
            ("\n" + _e(lk[1])) if lk[1] else ""))
        if r["kind"] != "daily":
            out.append("<i>(%s)</i>" % _e(cl.KINDS.get(r["kind"], ("", r["kind"]))[1]))
        live = _live_line(con, ds, r["side"], r["court"]) if ds == dstr(now_ist().date()) else None
        if live:
            n = _serial_num(_board_for(con, r["side"], r["court"])["serial"])
            out.append(live + ("\n    → %d items to go" % (r["serial"] - n) if n and r["serial"] >= n else ""))
        if c:
            out.append("<i>%s, %s</i>" % (_e(c["judges"].title()), _e(c["time"])))
    if len(rows) > 30:
        out.append("\n…and %d more; narrow the search." % (len(rows) - 30))
    return "\n".join(out)


def likelihood(con, ds, r):
    """(icon, short text) for an item, using the same logic as the nightly report."""
    try:
        c = con.execute("SELECT * FROM courts WHERE date=? AND side=? AND kind='daily' AND court=?",
                        (ds, r["side"], r["court"])).fetchone()
        if not c or r["kind"] != "daily":
            return ("⚪", "")
        items = [{"serial": i["serial"], "tagged": bool(i["tagged"]), "section": i["section"], "fixed": i["fixed"]}
                 for i in con.execute("SELECT serial, tagged, section, fixed FROM items WHERE date=? AND side=? AND court=? "
                                      "AND kind='daily'", (ds, r["side"], r["court"]))]
        header = {"time": c["time"], "notes": c["notes"], "determination": c["determination"], "bench": c["bench"],
                  "weekday": dt.datetime.strptime(ds, "%d%m%Y").strftime("%A").upper()}
        it = {"serial": r["serial"], "tagged": bool(r["tagged"]), "section": r["section"], "fixed": r["fixed"]}
        a = cl.assess(it, items, header, None, ds, r["side"])
        icon = {"HIGH": "🟢", "MODERATE": "🟡", "NONE": "⛔"}.get(a["level"], "⚪")
        return (icon, "%s%s%s" % (a["level"], (" around " + a["eta"]) if a["eta"] else "",
                                   ("; " + a["closes"]) if a["closes"] else ""))
    except Exception as ex:  # never let one odd court break a reply
        return ("⚪", "likelihood not available (%s)" % ex.__class__.__name__)


def ans_board(con, ds, courts=None, side="A"):
    age = board_age(con)
    if age is None or age > 15 * 60:
        return None
    q = "SELECT * FROM board_now WHERE 1=1" + (" AND side=?" if side else "")
    rows = con.execute(q + " ORDER BY side, CAST(court AS INT)", (side,) if side else ()).fetchall()
    if courts:
        rows = [r for r in rows if r["court"] in courts]
    out = ["<b>📺 Display board</b> (%s, read %d min ago)" % (_e(rows[0]["fetched"] if rows else ""), age // 60)]
    for b in rows:
        n = _serial_num(b["serial"])
        it = _item_at(con, ds, b["side"], b["court"], n) if (n and b["daily"]) else None
        out.append("Ct %s: <b>%s</b>%s%s" % (_e(b["court"]), _e(b["serial"]), "" if b["daily"] else " (other list)",
                                            (" · %s · %s" % (_e(it["section"].title()[:28]), _e(it["case_no"]))) if it else ""))
    return "\n".join(out)


def _ymd(d):
    return d[4:] + d[2:4] + d[:2]


def _norm_det(s):
    s = re.sub(r"\s+", " ", (s or "").upper())
    s = re.sub(r"\bON\s+\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b[^.;]*", " ", s)  # 'on 25.09.2026 at 2 PM ...' changes daily
    return re.sub(r"\s+", " ", s).strip()


def _det_diff(a, b):
    """(added, removed) text between two determinations, ignoring day-specific phrases and tiny edits."""
    import difflib
    a, b = _norm_det(a), _norm_det(b)
    if a == b:
        return [], []
    added, removed = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("delete", "replace") and len(re.findall(r"[A-Z]", a[i1:i2])) >= 20:
            removed.append(a[i1:i2].strip())
        if op in ("insert", "replace") and len(re.findall(r"[A-Z]", b[j1:j2])) >= 20:
            added.append(b[j1:j2].strip())
    return added, removed


def _similar(a, b):
    added, removed = _det_diff(a, b)
    return not added and not removed


def periods(con, side, match):
    """Runs of consecutive list days in which a court matched: [(court, judges, first, last, det)]."""
    dates = [r["date"] for r in con.execute("SELECT date FROM lists WHERE side=? AND kind='daily'", (side,))]
    dates.sort(key=_ymd)
    runs, open_ = [], {}
    for d in dates:
        seen = set()
        for c in con.execute("SELECT court, judges, determination FROM courts WHERE date=? AND side=? AND kind='daily'",
                             (d, side)):
            if not match(c):
                continue
            k = (c["court"], c["judges"])
            seen.add(k)
            if k in open_:
                open_[k][3] = d
                open_[k][4] = c["determination"]
            else:
                open_[k] = [c["court"], c["judges"], d, d, c["determination"]]
        for k in list(open_):
            if k not in seen:  # court absent or no longer matching on this list day
                runs.append(tuple(open_.pop(k)))
    runs += [tuple(v) for v in open_.values()]
    runs.sort(key=lambda x: (_ymd(x[2]), int(re.sub(r"\D", "", x[0]) or 0)))
    return runs, (dates[0], dates[-1]) if dates else (None, None)


def _runs_text(runs, span, latest):
    out = []
    for court, judges, a, b, det in runs:
        now = " (to date)" if b == latest else ""
        out.append("• <b>Court %s</b> · %s: %s → %s%s" % (_e(court), _e(judges.title()), pretty(a), pretty(b), now))
    out.append("<i>Records kept from %s to %s.</i>" % (pretty(span[0]), pretty(span[1])))
    return "\n".join(out)


def ans_group_history(con, side, n):
    pat = _group_re(n)
    runs, span = periods(con, side, lambda c: bool(pat.search((c["determination"] or "").upper())))
    if not runs:
        return "No record of Group-%s in the stored %s lists." % (ROMAN.get(n, n), side_name(side))
    return "<b>Group-%s over time (%s)</b>\n%s" % (ROMAN.get(n, n), side_name(side), _runs_text(runs, span, span[1]))


def ans_topic_history(con, side, words):
    ws = [w for w in re.findall(r"[A-Z0-9]{3,}", words.upper()) if w not in HIST_WORDS]
    if not ws:
        return None
    runs, span = periods(con, side, lambda c: all(w[:6] in (c["determination"] or "").upper() for w in ws))
    if not runs:
        return None
    return "<b>%s over time (%s)</b>\n%s" % (_e(" ".join(ws).title()), side_name(side), _runs_text(runs, span, span[1]))


def ans_justice_history(con, side, name):
    ws = [w for w in re.findall(r"[A-Z]{3,}", name.upper()) if w not in HIST_WORDS]
    if not ws:
        return None
    dates = sorted((r["date"] for r in con.execute("SELECT date FROM lists WHERE side=? AND kind='daily'", (side,))), key=_ymd)
    runs, cur = [], None
    for d in dates:
        c = con.execute("SELECT court, judges, determination FROM courts WHERE date=? AND side=? AND kind='daily' AND "
                        + " AND ".join("judges LIKE ?" for _ in ws), [d, side] + ["%%%s%%" % w for w in ws]).fetchone()
        if not c:
            continue
        if cur and cur["court"] == c["court"] and _similar(cur["det"], c["determination"]):
            cur["last"] = d
        else:
            cur = {"court": c["court"], "judges": c["judges"], "det": c["determination"], "first": d, "last": d}
            runs.append(cur)
    if not runs:
        return None
    out = ["<b>%s over time (%s)</b>" % (_e(" ".join(ws).title()), side_name(side))]
    for x in runs:
        out.append("\n<b>%s → %s</b> · Court %s%s\n<i>%s</i>" % (
            pretty(x["first"]), pretty(x["last"]), _e(x["court"]), " (to date)" if x["last"] == dates[-1] else "",
            _e(x["det"][:320] + ("…" if len(x["det"]) > 320 else ""))))
    out.append("\n<i>Records kept from %s to %s.</i>" % (pretty(dates[0]), pretty(dates[-1])))
    return "\n".join(out)


def roster_changes(con, side, d, prev=None):
    """What changed between the list of day d and the previous stored list day: [(court, text)]."""
    dates = sorted((r["date"] for r in con.execute("SELECT date FROM lists WHERE side=? AND kind='daily'", (side,))), key=_ymd)
    if d not in dates:
        return []
    i = dates.index(d)
    prev = prev or (dates[i - 1] if i else None)
    if not prev:
        return []
    old = {c["court"]: c for c in con.execute("SELECT * FROM courts WHERE date=? AND side=? AND kind='daily'", (prev, side))}
    new = {c["court"]: c for c in con.execute("SELECT * FROM courts WHERE date=? AND side=? AND kind='daily'", (d, side))}
    out = []
    for k, c in sorted(new.items(), key=lambda x: int(re.sub(r"\D", "", x[0]) or 0)):
        o = old.get(k)
        if not o:
            continue  # not sitting on the previous day: not a roster change
        if o["judges"] != c["judges"]:
            out.append((k, "now %s (was %s)" % (c["judges"].title(), o["judges"].title())))
        else:
            added, removed = _det_diff(o["determination"], c["determination"])
            if added or removed:
                bits = ["%s:" % c["judges"].title()]
                if added:
                    bits.append("added “%s”" % "… ".join(x[:200] for x in added[:2]))
                if removed:
                    bits.append("removed “%s”" % "… ".join(x[:200] for x in removed[:2]))
                out.append((k, " ".join(bits)))
    return out


def ans_changes(con, side, d):
    ch = roster_changes(con, side, d)
    if not ch:
        return "No roster changes in the %s list of %s compared with the previous list day." % (side_name(side), pretty(d))
    return "<b>Roster changes, %s list of %s</b>\n%s" % (side_name(side), pretty(d),
                                                         "\n".join("• <b>Court %s</b>: %s" % (_e(k), _e(t)) for k, t in ch))


HIST_WORDS = {"WHICH", "COURT", "COURTS", "TAKING", "TAKES", "TAKE", "TOOK", "MATTER", "MATTERS", "WHERE", "WHAT",
              "HISTORY", "PREVIOUSLY", "BEFORE", "EARLIER", "WAS", "WERE", "USED", "LAST", "PAST", "JUSTICE", "JUDGE",
              "HON", "BLE", "THE", "WHO", "KIND", "DOES", "DID", "OVER", "TIME", "LORDSHIP", "CHANGES", "CHANGED"}


def side_name(s):
    return cl.SIDES.get(s, ("", "", s))[2]


HELP = """<b>Ask me in plain words</b> (add "tomorrow", a date like 29/09, or "original side" / "jalpaiguri" if needed):
• <code>group 6</code> — which court(s) take Group-VI
• <code>anticipatory bail</code> / <code>service</code> — courts taking that kind of matter
• <code>justice a b ghosh</code> — what that judge takes, headings, note
• <code>court 35</code> or just <code>35</code> — full picture: determination, note, day plan, live item
• <code>mentioning 25</code> — when mentioning is allowed in that court
• <code>fixed 35</code> — fixed items / fixed hearings in that court
• <code>find WPA/1234/2026</code> or <code>find 1234</code> — where a case is listed
• <code>advocate a k sen</code> / <code>party ram das</code> — anyone's matters, with likelihood
• <code>running 12</code> / <code>board</code> / <code>my courts</code> — live display board (while the board tab is open)
• <code>courts</code> — all courts sitting, with judges and times
• <code>group 6 history</code> / <code>who took anticipatory bail before</code> — which courts/judges took it, and when
• <code>justice a b ghosh history</code> — what that judge took over time
• <code>changes</code> — roster changes in the latest list (you also get these automatically)
• <code>/help</code> — this list"""


def answer(text, names=()):
    """Reply (HTML) to a plain-words question."""
    raw = (text or "").strip()
    t = raw.upper()
    if not t or t in ("/HELP", "HELP", "/START", "?"):
        return HELP
    con = db()
    side = _side_from(t)
    explicit_side = side != "A" or "APPELLATE" in t
    explicit_date = bool(re.search(r"TOMORROW|YESTERDAY|DAY AFTER|\d{1,2}[./-]\d{1,2}|MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY", t))
    date = _date_from(t) if explicit_date else _next_list_date(con, side, now_ist().date())
    body = re.sub(r"\b(TOMORROW|TODAY|YESTERDAY|DAY AFTER|ON|FOR|IN|ORIGINAL SIDE|APPELLATE SIDE|JALPAIGURI|OS)\b", " ", t)
    body = re.sub(r"(?<![/\w])\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?(?![/\w])", " ", body)
    ds, n = _ensure(con, date, side)
    if n is None:
        # nothing for that day (weekend / holiday / not out yet): fall back to the latest list we have
        r = con.execute("SELECT date FROM lists WHERE side=? AND kind='daily' ORDER BY substr(date,5,4)||substr(date,3,2)||substr(date,1,2) DESC LIMIT 1",
                        (side,)).fetchone()
        if explicit_date or not r:
            return "The %s list for %s is not published (yet)." % (side_name(side), pretty(ds))
        ds = r["date"]
        note = "<i>(No list for %s yet; using %s.)</i>\n" % (pretty(dstr(date)), pretty(ds))
    else:
        note = ""
    court = _court_from(body)
    loose_court = court or _court_from(body, loose=True)

    def done(s):
        return note + s if s else note + "I could not find that. Send /help for what I can answer."

    def all_sides():  # searches for a case / person look at every side's list of that day
        if not explicit_side:
            for sd in ("O", "J"):
                try:
                    index_list(ds, sd, con=con)
                except Exception:
                    pass

    if re.search(r"\b(HISTORY|PREVIOUSLY|EARLIER|BEFORE|USED TO|WAS TAKING|WERE TAKING|TOOK|OVER TIME|IN THE PAST)\b", t):
        m = re.search(r"\bGROUP\s*[-–:]?\s*(\d{1,2}|[IVX]{1,5})\b", t)
        if m:
            g = m.group(1)
            num = int(g) if g.isdigit() else {v: k for k, v in ROMAN.items()}.get(g)
            if num:
                return ans_group_history(con, side, num)
        if re.search(r"\bJUSTICE\b|\bJUDGE\b|\bLORDSHIP\b", t):
            r = ans_justice_history(con, side, re.sub(r".*?\b(JUSTICE|JUDGE|LORDSHIP)\b", "", t))
            if r:
                return r
        r = ans_justice_history(con, side, t) or ans_topic_history(con, side, t)
        if r:
            return r
    if re.search(r"\b(ROSTER )?CHANGES?\b|\bWHAT CHANGED\b", t) and not re.search(r"\bFIND\b", t):
        return done(ans_changes(con, side, ds))

    if re.search(r"\bMY (COURTS?|MATTERS?|ITEMS?|CASES?)\b|\bMINE\b", t) and names:
        who = names[0]
        all_sides()
        return done(ans_find(con, ds, side, who, "advocate"))
    m = re.search(r"\bGROUP\s*[-–:]?\s*(\d{1,2}|[IVX]{1,5})\b", t)
    if m:
        g = m.group(1)
        num = int(g) if g.isdigit() else {v: k for k, v in ROMAN.items()}.get(g)
        if num:
            return done(ans_group(con, ds, side, num))
    if re.search(r"\bMENTION", t) and loose_court:
        return done(ans_court(con, ds, side, loose_court, "mention"))
    if re.search(r"\bFIXED\b", t) and loose_court:
        return done(ans_court(con, ds, side, loose_court, "fixed"))
    if re.search(r"\b(BOARD|DISPLAY)\b", t) and not court:
        return done(ans_board(con, ds, None, side) or "The display board is not being read right now. Open it in Chrome "
                    "(<code>~/.casemonitor/casemonitor board</code>) and type the CAPTCHA.")
    if re.search(r"\b(RUNNING|GOING ON|NOW|CURRENT|WHICH ITEM|WHAT ITEM|LIVE)\b", t) and loose_court:
        court = loose_court
        live = _live_line(con, ds, side, court)
        c = con.execute("SELECT * FROM courts WHERE date=? AND side=? AND kind='daily' AND court=?", (ds, side, court)).fetchone()
        head = _court_line(c) if c else "Court %s" % court
        return done(head + "\n" + (live or "The display board is not being read right now (open it and type the CAPTCHA)."))
    body = re.sub(r"\s+", " ", body).strip()
    m = re.search(r"\b(ADVOCATE|ADV|COUNSEL|LAWYER)\.?\s+(.+)$", body)
    if m:
        all_sides()
        return done(ans_find(con, ds, side, m.group(2), "advocate"))
    m = re.search(r"\b(PARTY|PETITIONER|RESPONDENT)\s+(.+)$", body)
    if m:
        all_sides()
        return done(ans_find(con, ds, side, m.group(2), "party"))
    m = re.search(r"\b(FIND|SEARCH|WHERE IS|STATUS OF|CASE)\s+(.+)$", body)
    if m:
        all_sides()
        return done(ans_find(con, ds, side, m.group(2)))
    mc = re.search(r"[A-Z][A-Z.() ]{1,12}\s*/\s*\d{1,6}\s*/\s*\d{4}", t)
    if mc:
        all_sides()
        return done(ans_find(con, ds, side, mc.group(0)))
    if re.search(r"\bJUSTICE\b|\bJUDGE\b|\bLORDSHIP\b", t):
        r = ans_justice(con, ds, side, re.sub(r".*?\b(JUSTICE|JUDGE|LORDSHIP)\b", "", body))
        if r:
            return done(r)
    if re.search(r"\bCOURTS\b|\bROSTER\b|\bALL COURTS\b", t) and not court:
        rows = _courts(con, ds, side)
        return done("<b>%s, %s: %d courts</b>\n%s" % (side_name(side), pretty(ds), len(rows),
                                                   "\n".join(_court_line(c) for c in rows)))
    if court:
        return done(ans_court(con, ds, side, court))
    r = ans_justice(con, ds, side, body)
    if r:
        return done(r)
    r = ans_topic(con, ds, side, body)
    if r:
        return done(r)
    return done(ans_find(con, ds, side, body))
