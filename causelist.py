"""
Calcutta High Court cause list: download, parse and analyse.

Reads the public daily cause list PDFs from calcuttahighcourt.gov.in, finds the
items where the configured advocate name appears, and estimates for each one
whether it is realistically likely to be reached, with a short plain-English
comment on the Bench's determination and notes so the estimate can be checked
by hand.
"""
import os
import re
import ssl
import time
import urllib.error
import urllib.request

import fitz  # PyMuPDF

BASE = "https://www.calcuttahighcourt.gov.in/downloads/old_cause_lists"
# side code -> (folder, file infix, label)
SIDES = {
    "A": ("AS", "a", "Appellate Side"),
    "O": ("OS", "", "Original Side"),
    "J": ("J", "j", "Jalpaiguri Circuit Bench"),
    "P": ("PB", "pb", "Port Blair Circuit Bench"),
}
UA = {"User-Agent": "Mozilla/5.0 (Calcutta HC Case Monitoring System)"}

# Rough pace of a Bench. Tune in config.env if your experience differs.
MIN_PER_UNIT = float(os.environ.get("MIN_PER_UNIT", "5"))   # minutes per ordinary motion item
DAY_END = os.environ.get("DAY_END", "16:30")                 # courts rise
LUNCH = ("13:30", "14:00")                                  # recess

Y_TOP, Y_BOTTOM = 40, 800            # running page header / footer
SERIAL_RE = re.compile(r"^(wt|with)?\s*(\d{1,4})(?:\s+(.*))?$", re.I)
CASE_RE = re.compile(r"^[A-Z][A-Z .()&-]*/\d+/\d{4}")
BENCH_ID_RE = re.compile(r"\(Bench ID-(\d+)\s*\)")


# list kind -> (url sub-folder, label). Supplementary lists are extra daily lists
# published for the same date; the monthly list is dated with its first hearing day.
KINDS = {
    "daily": ("", ""),
    "sup1": ("supplementary/", "Supplementary List"),
    "sup2": ("supplementary2/", "Supplementary List 2"),
    "sup3": ("supplementary3/", "Supplementary List 3"),
    "sup4": ("supplementary4/", "Supplementary List 4"),
    "sup5": ("supplementary5/", "Supplementary List 5"),
    "monthly": ("monthly/", "Monthly List"),
}
SUP_KINDS = ("sup1", "sup2", "sup3", "sup4", "sup5")


# ------------------------------------------------------------------ download
def pdf_url(date_str, side="A", kind="daily"):
    folder, infix, _ = SIDES[side]
    return "%s/%s%s/cl%s%s.pdf" % (BASE, KINDS[kind][0], folder, infix, date_str)


def _ssl_ctx():
    try:
        import certifi  # present when installed via requirements.txt
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _fetch(url, timeout=180, tries=3):
    """GET with retries: the court site sometimes drops connections or fails the
    TLS handshake for a moment. Returns bytes, or None for 404."""
    last = None
    for n in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
        except (urllib.error.URLError, OSError) as e:  # timeouts, resets, TLS hiccups
            last = e
        time.sleep(5 * (n + 1))
    raise last


def exists(date_str, side="A", kind="daily"):
    """Cheap check whether a list is published (first bytes only)."""
    url = pdf_url(date_str, side, kind)
    for n in range(2):
        try:
            req = urllib.request.Request(url, headers=dict(UA, Range="bytes=0-7"))
            with urllib.request.urlopen(req, timeout=40, context=_ssl_ctx()) as r:
                return r.read(8).startswith(b"%PDF")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
        except (urllib.error.URLError, OSError):
            time.sleep(3)
    return False


def download(date_str, side, cache_dir, refresh=False, kind="daily"):
    """Return local path of the PDF, or None if not published (404)."""
    sub = os.path.join(cache_dir, kind) if kind != "daily" else cache_dir
    os.makedirs(sub, exist_ok=True)
    path = os.path.join(sub, os.path.basename(pdf_url(date_str, side, kind)))
    if os.path.exists(path) and not refresh:
        return path
    data = _fetch(pdf_url(date_str, side, kind))
    if data is None or not data.startswith(b"%PDF"):
        return None
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path


# ------------------------------------------------------------------ PDF parsing
def page_lines(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            t = "".join(s["text"] for s in l["spans"]).strip()
            if t:
                out.append((l["bbox"][0], l["bbox"][1], t))
    out.sort(key=lambda r: (round(r[1]), r[0]))
    return out


def bench_id(page):
    m = BENCH_ID_RE.search(page.get_text())
    return m.group(1) if m else None


def court_page_range(doc, pno):
    bid = bench_id(doc[pno])
    if bid is None:
        return pno, pno
    s = pno
    while s > 0 and bench_id(doc[s - 1]) == bid:
        s -= 1
    e = pno
    while e < len(doc) - 1 and bench_id(doc[e + 1]) == bid:
        e += 1
    return s, e


def _columns(lines):
    """Detect column x-positions on a page: (case_x, party_x, advocate_x)."""
    vs = sorted(x for x, y, t in lines if t == "VS")
    party_x = vs[len(vs) // 2] if vs else 184
    case_xs = sorted(x for x, y, t in lines if CASE_RE.match(t) and x < party_x - 20 and x > 55)
    case_x = case_xs[len(case_xs) // 2] if case_xs else 70
    adv = sorted(x for x, y, t in lines if x > party_x + 150)
    adv_x = adv[0] if adv else party_x + 185
    return case_x, party_x, adv_x


def parse_header(lines):
    head, in_head = [], False
    for x, y, t in lines:
        if t.upper().startswith("COURT NO."):
            in_head = True
        if in_head:
            head.append(t)
            if t.upper().startswith("VC LINK"):
                break
    info = {"court_no": "", "bench": "", "time": "", "floor": "", "judges": [],
            "determination": "", "notes": "", "vc_link": "", "weekday": ""}
    for x, y, t in lines[:12]:
        m = re.match(r"^For\s+(\w+day)\b", t, re.I)
        if m:
            info["weekday"] = m.group(1).upper()
    if not head:
        return info
    info["court_no"] = head[0].split(".", 1)[1].strip()
    stage, det, notes = "top", [], []
    for t in head[1:]:
        u = t.upper()
        if re.match(r"^(THE\s+)?HON.BLE", u) and stage in ("top", "judges"):
            name = re.sub(r"^(THE\s+)?HON.BLE\s+", "", t, flags=re.I).strip()
            info["judges"].append(name)
            stage = "judges"
        elif u.startswith("VC LINK"):
            info["vc_link"] = t.split(":", 1)[1].strip()
        elif re.match(r"^(NOTE|NB)\b", u):
            stage = "notes"
            rest = re.sub(r"^(NOTE|NB)\s*:?\s*", "", t, flags=re.I)
            if rest:
                notes.append(rest)
        elif stage == "top":
            if "BENCH" in u:
                info["bench"] = t
            elif u.startswith("AT "):
                info["time"] = t[3:].strip()
            elif "FLOOR" in u:
                info["floor"] = t
        elif stage in ("judges", "det"):
            stage = "det"
            det.append(t)
        elif stage == "notes":
            notes.append(t)
    info["determination"] = re.sub(r"\s+", " ", " ".join(det)).strip()
    info["notes"] = re.sub(r"\s+", " ", " ".join(notes)).strip()
    return info


FIXED_RE = re.compile(r"\[\s*AT\s+(\d{1,2})(?:[.:](\d{2}))?\s*([AP])\.?\s*M", re.I)


def parse_items(doc, start, end):
    items, section, cur = [], "", None
    past_heading = title_open = False
    for pno in range(start, end + 1):
        lines = [r for r in page_lines(doc[pno]) if Y_TOP <= r[1] <= Y_BOTTOM]
        case_x, party_x, adv_x = _columns(lines)
        for x, y, t in lines:
            if t.upper().startswith("VC LINK"):
                past_heading = True
                continue
            if x < case_x - 8:  # serial column
                m = SERIAL_RE.match(t)
                if m and (m.group(3) is None or CASE_RE.match(m.group(3) or "")):
                    cur = {"serial": int(m.group(2)), "tagged": bool(m.group(1)),
                           "case_no": (m.group(3) or "").strip(), "parties": [],
                           "advocates": [], "section": section, "page": pno + 1, "fixed": None}
                    items.append(cur)
                    title_open = False
                continue
            fm = FIXED_RE.search(t)
            if fm and cur is not None:  # "[AT 4.00 P.M.]": this item is fixed for that time
                h = int(fm.group(1)) % 12 + (12 if fm.group(3).upper() == "P" else 0)
                cur["fixed"] = h * 60 + int(fm.group(2) or 0)
                continue
            # centred section titles sit right of the parties column, left of advocates;
            # a title may run over two lines ("BAIL APPLICATION" / "(2026)")
            if party_x + 18 < x < adv_x - 20 and t == t.upper() and t != "VS" and (past_heading or cur):
                section = (section + " " + t) if title_open else t
                title_open = True
                continue
            if cur is None:
                continue
            if x < party_x - 8:
                if not cur["case_no"] and CASE_RE.match(t):
                    cur["case_no"] = t
            elif x < adv_x - 10:
                cur["parties"].append(t)
            else:
                cur["advocates"].append(t)
    for it in items:
        it["case_name"] = re.sub(r"\s+", " ", " ".join(it["parties"])).replace(" VS ", " vs ").strip()
        it["advocates_text"] = " ".join(it["advocates"])
    return items


def name_hit(text, names):
    t = re.sub(r"\s+", " ", text.upper())
    for n in names:
        pat = r"\b" + r"\s+".join(re.escape(w) for w in n.upper().split()) + r"\b"
        if re.search(pat, t):
            return True
    return False


# ------------------------------------------------------------------ likelihood
def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _parse_time(s):
    m = re.search(r"(\d{1,2})[:.](\d{2})\s*([AP])\.?M", s.upper())
    if not m:
        return None
    h = int(m.group(1)) % 12 + (12 if m.group(3) == "P" else 0)
    return h * 60 + int(m.group(2))


def _fmt(mins):
    h, m = divmod(int(mins), 60)
    return "%d:%02d %s" % ((h - 1) % 12 + 1, m, "AM" if h < 12 else "PM")


def section_weight(section):
    s = (section or "").upper()
    if "JUDGMENT" in s:
        return 0.3, "judgment delivery (quick)"
    if "FOR ORDER" in s or "MENTION" in s:
        return 0.5, "for orders / mentioned (quick)"
    if "HEARING" in s:
        return 3.0, "hearing (slow, about 3x a motion)"
    return 1.0, "motion / application"


# ------------------------------------------------------------------ Bench note -> timetable
RECESS_START, RECESS_END = _hm(LUNCH[0]), _hm(LUNCH[1])
DAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY")
STOP = set("""THE OF FOR AND TO BE WILL SHALL WOULD CALLED CALL TAKEN TAKE UP TILL UPTO UNTIL FROM AFTER BEFORE
ON AT IN BY WITH MATTERS MATTER LIST LISTS DAILY ALL HIS HER LORDSHIP LORDSHIPS HON BLE COURT BENCH ITEMS
ITEM ONWARDS THEREAFTER DATED WHICH ARE IS AS PER NOTE ONLY ALSO CASES CASE HEAR HEARD NO NOS SERIAL
P M A NOON RECESS LUNCH TODAY SAID THIS THESE THOSE BETWEEN IF ANY REST REMAINING OTHER THAN
MONDAY TUESDAY WEDNESDAY THURSDAY FRIDAY SATURDAY REGULAR BASIS BASI SERIALLY MANNER FIRST SITTING
EVERY EVERYDAY EVERDAY DAY THEN NEXT CONTINUE POSITION ORDER WISE RELATING RELATED UNDER CONNECTED
THERETO PERMIT PERMITS TIME ONE WHICHEVER EARLIER LATER SIDE""".split())
T_TOK = re.compile(r"<T(\d+)>")
D_TOK = re.compile(r"<D([\d.]+)>")
MONTHS = "JANUARY FEBRUARY MARCH APRIL MAY JUNE JULY AUGUST SEPTEMBER OCTOBER NOVEMBER DECEMBER".split()


def _tokenise_note(text):
    """Upper-case note with dates as <Ddd.mm.yyyy> and times as <Tminutes>."""
    t = " " + re.sub(r"\s+", " ", text.upper()) + " "
    t = re.sub(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b",
               lambda m: "<D%02d.%02d.%s>" % (int(m.group(1)), int(m.group(2)),
                                             m.group(3) if len(m.group(3)) == 4 else "20" + m.group(3)), t)
    t = re.sub(r"\bNOS?\s*\.", "NO", t)
    t = re.sub(r"\b(DR|MR|MRS|MS)\s*\.", r"\1", t)

    def tm(m):
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ap.startswith("NOON"):
            return "<T%d>" % (12 * 60 + mi)
        h = h % 12 + (12 if ap.startswith("P") else 0)
        return "<T%d>" % (h * 60 + mi)
    t = re.sub(r"\b(\d{1,2})(?:\s*[:.]\s*(\d{2}))?\s*(A\.?\s?M\b\.?|P\.?\s?M\b\.?|NOON\b)", tm, t)
    t = re.sub(r"\b(TILL|UPTO|UP TO|UNTIL|BEFORE|UPTILL|TO)\s+(THE\s+)?(RECESS|LUNCH)(\s+(HOUR|BREAK|TIME))?", r"\1 <T%d>" % RECESS_START, t)
    t = re.sub(r"\b(AFTER|FROM|POST)\s+(THE\s+)?(RECESS|LUNCH)(\s+(HOUR|BREAK|TIME|SESSION))?", "AFTER <T%d>" % RECESS_END, t)
    t = re.sub(r"\b(IN\s+THE\s+)?(FIRST|1ST) HALF\b", "TILL <T%d>" % RECESS_START, t)
    t = re.sub(r"\b(IN\s+THE\s+)?(SECOND|2ND) HALF\b", "AFTER <T%d>" % RECESS_END, t)
    return t


def _words(text):
    out = set()
    for w in re.findall(r"[A-Z0-9]+", T_TOK.sub(" ", D_TOK.sub(" ", text.upper()))):
        if w in STOP or w.startswith("APPLI") or w.startswith("APLI"):
            continue
        if w.isdigit() and len(w) != 4:
            continue  # section numbering / counts, but keep years like 2026
        out.add(w[:-1] if len(w) > 4 and w.endswith("S") else w)
    return out


def _same(a, b):
    if a == b:
        return True
    if len(a) >= 5 and len(b) >= 5:
        import difflib
        return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8  # CENCELLATION ~ CANCELLATION
    return False


def _subset(small, big):
    return all(any(_same(w, x) for x in big) for w in small)


def _heading_score(heading, phrase_words):
    hw = {w for w in _words(heading) if not w.isdigit()} or _words(heading)
    if not hw or not phrase_words:
        return 0
    if _subset(hw, phrase_words) and _subset(phrase_words, hw):
        return 3
    if _subset(hw, phrase_words):
        return 2 - 0.1 * (len(phrase_words) - len(hw))
    if _subset(phrase_words, hw):  # "SIR MATTERS" -> "FIXED MATTERS (SIR MATTERS)"
        return 1.5 - 0.1 * (len(hw) - len(phrase_words))
    return 0


def _date_ok(clause, list_date, weekday):
    """False if the clause is expressly for another date or weekday."""
    dates = re.findall(r"\b(?:ON|FOR)\s+(?:\w+DAY\s*,?\s*)?<D([\d.]+)>", clause)
    if dates and list_date:
        want = "%s.%s.%s" % (list_date[:2], list_date[2:4], list_date[4:])
        if want not in dates:
            return False
    named = [d for d in DAYS if re.search(r"\b%sS?\b" % d, clause)]
    for a, b in re.findall(r"\b(%s)S?\s+(?:TO|-)\s+(%s)" % ("|".join(DAYS), "|".join(DAYS)), clause):
        named += list(DAYS[DAYS.index(a):DAYS.index(b) + 1])
    if named and weekday and weekday not in named:
        return False
    if re.search(r"\bTODAY\b|\bEVERY ?DAY\b|\bDAILY\b", clause) and not named:
        return True
    return None if not (dates or named) else True


APPELLATE_RE = re.compile(r"\bAPP?ELL?A?TE SIDE\b")
ORIGINAL_RE = re.compile(r"\bORI?GINAL SIDE\b")


def parse_timetable(notes, headings, sit, list_date="", weekday="", side="A"):
    """Read the Bench note as a timetable.

    Returns dict with:
      slots:      {heading: [(start, end, clause_text)]}
      list_start: this list (e.g. 'appellate side matters') starts later than the sitting time
      sit_at:     'his Lordship shall sit at 2:30 PM' for this date
      fixed_from: minutes when 'fixed matters' are called (or None)
      other_from: minutes after which the Bench is on other work (the other side's list,
                  division bench matters...) with no heading of this list named
      monthly:    [(start, end, clause_text)] clauses that take up a monthly list
      unread:     [clause_text] clauses with a time that matched nothing
    """
    res = {"slots": {}, "fixed_from": None, "other_from": None, "list_start": None, "sit_at": None,
           "monthly": [], "unread": []}
    if not notes:
        return res
    tok = _tokenise_note(notes)
    own_re, other_re = (APPELLATE_RE, ORIGINAL_RE) if side != "O" else (ORIGINAL_RE, APPELLATE_RE)
    # numbered notes "1. / 1) / 1 : / (1) / I. / NB:" restart the date context
    parts = re.split(r"(?:^|\s)(?:\d{1,2}\s*(?:\.|\)|::?)(?!\d)|\(\d{1,2}\)|[IVX]{1,4}\.|NB\s*:|SPECIAL NOTE\s*:?)\s", tok)
    clauses = []
    for pi, part in enumerate(parts):
        ctx, prio = None, 0
        for sent in re.split(r"(?<=[A-Z0-9)\]>'\"])\.\s|;|\*{3,}", part):
            # a new clause starts at 'AFTER/FROM <time>' only right after a clause that ended on a time
            # ("... till 12:30 PM. After 12:30 PM, anticipatory bail ...")
            pieces, cur = [], ""
            for bit in re.split(r"(?=\b(?:AFTER|FROM|BETWEEN)\s+<T)", sent):
                if cur and re.search(r"<T\d+>\W*$", cur):
                    pieces.append(cur)
                    cur = bit
                else:
                    cur += bit
            pieces.append(cur)
            for c in pieces:
                c = (c or "").strip(" ,.-:")
                if not c:
                    continue
                ok = _date_ok(c, list_date, weekday)
                if ok is not None:
                    ctx = ok
                    prio = 2 if re.search(r"<D|\bTODAY\b", c) else 1 if re.search(r"DAY\b", c) else 0
                if ctx is False:
                    continue
                m_s = re.search(r"\b(AFTER|FROM|BETWEEN|AT)\s+<T(\d+)>", c)
                m_e = re.search(r"\b(TILL|UPTO|UP TO|UNTIL|BEFORE|UPTILL|AND|TO)\s+<T(\d+)>", c)
                clauses.append({"text": c, "start": int(m_s.group(2)) if m_s else None, "part": pi, "prio": prio,
                                "end": int(m_e.group(2)) if m_e else None, "timed": bool(T_TOK.search(c))})
    # fill gaps: a clause without a start follows the previous one (same note); without an end,
    # it runs to the next start
    prev_end, prev_part = sit, None
    for i, c in enumerate(clauses):
        if not c["timed"]:
            continue
        if c["part"] != prev_part:
            prev_end, prev_part = sit, c["part"]
        if c["start"] is None:
            c["start"] = prev_end
        if c["end"] is None or c["end"] <= c["start"]:
            nxt = [d["start"] for d in clauses[i + 1:] if d["timed"] and d["start"] and d["start"] > c["start"]]
            c["end"] = nxt[0] if nxt else _hm(DAY_END)
        prev_end = c["end"]

    def pretty_clause(c):
        return D_TOK.sub(lambda m: m.group(1), T_TOK.sub(lambda m: _fmt(int(m.group(1))), c)).strip().lower()

    for c in clauses:
        if not c["timed"]:
            continue
        body = c["text"]
        m = re.search(r"\bSIT\b.{0,40}?\b(?:AT|FROM)\s+<T(\d+)>", body)
        if m:
            res["sit_at"] = int(m.group(1))
            continue
        phrases = [p for p in re.split(r",|\bAND\b|\bALSO\b|&|\bOR\b", body) if p.strip()]
        matched = False
        for ph in phrases:
            pt = [int(x) for x in T_TOK.findall(ph)]
            p_start = c["start"]
            if pt and re.search(r"\b(AFTER|FROM|AT)\s+<T", ph):
                p_start = int(re.search(r"\b(?:AFTER|FROM|AT)\s+<T(\d+)>", ph).group(1))
            p_start = p_start if p_start is not None else sit
            if own_re.search(ph) and not other_re.search(ph):
                if re.search(r"\b(TILL|UPTO|UP TO|UNTIL|TO)\s+<T", ph):   # 'appellate side matters till recess'
                    end = int(re.search(r"\b(?:TILL|UPTO|UP TO|UNTIL|TO)\s+<T(\d+)>", ph).group(1))
                    res["other_from"] = end if res["other_from"] is None else min(res["other_from"], end)
                elif p_start and p_start > sit:
                    res["list_start"] = p_start if res["list_start"] is None else min(res["list_start"], p_start)
                matched = True
                continue
            if other_re.search(ph) or re.search(r"DIVISION BENCH|\bDB\b", ph):
                if p_start > sit and not re.search(r"COMPLETION|EXHAUST|AFTER THE", ph):
                    res["other_from"] = p_start if res["other_from"] is None else min(res["other_from"], p_start)
                    matched = True
                continue
            pw = _words(ph)
            best, score = None, 0
            for h in headings:
                sc = _heading_score(h, pw)
                if sc > score:
                    best, score = h, sc
            if best:
                matched = True
                if p_start <= sit and c["end"] >= _hm(DAY_END):
                    continue  # 'X will be taken up from 10:30 AM': no real time limit
                for h in headings:  # also equally good matches (e.g. two 'MENTIONED' headings)
                    if h == best or (score >= 3 and _heading_score(h, pw) == score):
                        res["slots"].setdefault(h, []).append((p_start, c["end"], pretty_clause(body), c["prio"]))
        if "MONTHLY" in body or "COMBINED" in body:
            res["monthly"].append((c["start"], c["end"], pretty_clause(body)))
            matched = True
        if re.search(r"\bFIXED\b", body) and not any(re.search(r"\bFIXED\b", h.upper()) for h in res["slots"]):
            res["fixed_from"] = c["start"] if res["fixed_from"] is None else min(res["fixed_from"], c["start"])
            matched = True
        if not matched and _words(body) - {"EVERYDAY", "ONWARD"}:
            res["unread"].append(pretty_clause(body))
    # a note for this very date beats a weekly rule, which beats a general one
    for h, v in list(res["slots"].items()):
        top = max(x[3] for x in v)
        res["slots"][h] = [(a, b, c) for a, b, c, p in v if p == top]
    if res["other_from"]:
        of = res["other_from"]
        res["slots"] = {h: [(a, min(b, of), c) for a, b, c in v if a < of] for h, v in res["slots"].items()}
        res["slots"] = {h: v for h, v in res["slots"].items() if v}
    return res


# ------------------------------------------------------------------ day plan + likelihood
def day_plan(items, header, list_date="", side="A"):
    """Every heading of this court's list, in order, with item range, count and time slot."""
    sit = _parse_time(header.get("time", "") or "") or _hm("10:30")
    secs, order = {}, []
    for it in items:
        s = it["section"] or "-"
        if s not in secs:
            secs[s] = {"name": s, "first": it["serial"], "last": it["serial"], "count": 0, "fixed": 0}
            order.append(s)
        d = secs[s]
        d["last"] = max(d["last"], it["serial"])
        if it.get("fixed"):
            d["fixed"] += 1
        elif not it["tagged"]:
            d["count"] += 1
    tt = parse_timetable(header.get("notes", ""), order, sit, list_date, header.get("weekday", ""), side)
    if tt["sit_at"]:
        sit = tt["sit_at"]
    if tt["list_start"] and tt["list_start"] > sit:
        sit = tt["list_start"]
    plan = []
    for s in order:
        d = dict(secs[s])
        sl = tt["slots"].get(s) or []
        hm_ = re.search(r"\bAT\s+(\d{1,2})(?:[.:](\d{2}))?\s*([AP])\.?\s*M", s.upper())
        if not sl and hm_:
            st = int(hm_.group(1)) % 12 * 60 + (720 if hm_.group(3) == "P" else 0) + int(hm_.group(2) or 0)
            en = min(tt["other_from"] or _hm(DAY_END), st + max(1, d["count"]) * MIN_PER_UNIT)
            sl = [(st, max(st + 5, en), "heading fixed for %s" % _fmt(st))]
        d["slot"] = (min(a for a, b, c in sl), max(b for a, b, c in sl)) if sl else None
        d["slot_note"] = sl[0][2] if sl else ""
        plan.append(d)
    return {"sit": sit, "sections": plan, "tt": tt}


def _work_minutes(start, end):
    """Minutes of court time between start and end, less the recess."""
    if end <= start:
        return 0
    lunch = max(0, min(end, RECESS_END) - max(start, RECESS_START))
    return end - start - lunch


def _eta(start, need):
    eta = start + need
    if start < RECESS_START <= eta or (RECESS_START <= start < RECESS_END):
        eta += RECESS_END - max(start, RECESS_START)
    return eta


def _level(ratio):
    if ratio <= 0.6:
        return "HIGH"
    if ratio <= 1.0:
        return "MODERATE"
    if ratio <= 1.4:
        return "LOW"
    return "VERY LOW"


def assess(item, items, header, plan=None, list_date="", side="A"):
    """Return dict: level, realistic(bool), eta, position text, determination comment."""
    plan = plan or day_plan(items, header, list_date, side)
    sit, secs, tt = plan["sit"], plan["sections"], plan["tt"]
    day_end = _hm(DAY_END)
    if tt["other_from"] and sit < tt["other_from"] < day_end:
        day_end = tt["other_from"]
    total = max((i["serial"] for i in items), default=0)
    sec = next((s for s in secs if s["name"] == (item["section"] or "-")), None)
    sec_items = [i for i in items if (i["section"] or "-") == (item["section"] or "-")]
    pos = 1 + sum(1 for i in sec_items if i["serial"] < item["serial"] and not i["tagged"] and not i.get("fixed"))
    w, wtxt = section_weight(item["section"])
    parts = ["%s sits %s" % (header.get("bench") or "Bench", header.get("time") or _fmt(sit))]
    if tt["sit_at"]:
        parts.append("note: sits at %s today" % _fmt(tt["sit_at"]))
    if tt["list_start"]:
        parts.append("note: this side's list is taken up from %s" % _fmt(tt["list_start"]))
    parts.append("item %s of %d; %s item of %d in '%s' (items %d-%d, %s)" % (
        item["serial"], total, _ordinal(pos), sec["count"] if sec else 0, item["section"] or "-",
        sec["first"] if sec else 0, sec["last"] if sec else 0, wtxt))
    any_slot = any(s["slot"] for s in secs)
    closes = ""

    if "NOT SIT" in (header.get("notes", "") + header.get("determination", "")).upper():
        level, eta, need, cap = "NONE", None, 0, 0
        parts.append("Bench not sitting per note")
    elif item.get("fixed"):
        level, eta, need, cap = "HIGH", item["fixed"], 0, 1
        parts.append("fixed for %s (marked in the list)" % _fmt(item["fixed"]))
    elif sec and sec["slot"]:
        s0, e0 = sec["slot"]
        cap = _work_minutes(s0, e0)
        ahead = [i for i in sec_items if i["serial"] < item["serial"] and not i["tagged"] and not i.get("fixed")]
        load = sum(section_weight(i["section"])[0] for i in ahead)
        need = load * MIN_PER_UNIT
        level = _level(need / cap if cap else 9)
        eta = _eta(s0, need)
        parts.append("Bench note gives this heading %s to %s (%d min): '%s'" % (_fmt(s0), _fmt(e0), cap, sec["slot_note"][:110]))
        parts.append("about %d items ahead in this heading, about %d min of work vs %d min" % (round(load), need, cap))
        if level not in ("HIGH", "MODERATE"):
            closes = "this heading closes at %s and is not scheduled again today, so the item is likely not reached" % _fmt(e0)
            parts.append(closes)
    elif any_slot:
        # the note schedules some headings; this one only gets the time left over
        busy = sorted(s["slot"] for s in secs if s["slot"])
        windows, cur = [], sit
        for a, b in busy:
            if a > cur:
                windows.append((cur, min(a, day_end)))
            cur = max(cur, b)
        if cur < day_end:
            windows.append((cur, day_end))
        free = sum(_work_minutes(a, b) for a, b in windows)
        slotted = {s["name"] for s in secs if s["slot"]}
        ahead = [i for i in items if i["serial"] < item["serial"] and not i["tagged"] and not i.get("fixed")
                 and (i["section"] or "-") not in slotted]
        load = sum(section_weight(i["section"])[0] for i in ahead)
        need, cap = load * MIN_PER_UNIT, free
        if free <= 0:
            level, eta = "VERY LOW", None
            closes = "the Bench note gives no time to '%s' today; only if the scheduled parts finish early" % (item["section"] or "-")
            parts.append(closes)
        else:
            level, eta, left = _level(need / free), None, need
            for a, b in windows:  # walk the free windows to find the time
                wm = _work_minutes(a, b)
                if left <= wm:
                    eta = _eta(a, left)
                    break
                left -= wm
            parts.append("the Bench note schedules other headings; about %d min left for unscheduled ones, "
                         "about %d min of work ahead" % (free, need))
    else:
        start = sit
        cap = _work_minutes(start, day_end)
        fixed_min = MIN_PER_UNIT * sum(1 for i in items if i.get("fixed"))
        cap = max(0, cap - fixed_min)
        ahead = [i for i in items if i["serial"] < item["serial"] and not i["tagged"] and not i.get("fixed")]
        load = sum(section_weight(i["section"])[0] for i in ahead)
        need = load * MIN_PER_UNIT
        level = _level(need / cap if cap else 9)
        eta = _eta(start, need)
        if tt["other_from"] and tt["other_from"] < _hm(DAY_END):
            parts.append("daily list effectively till %s per note" % _fmt(tt["other_from"]))
        parts.append("about %d motion-equivalents ahead, about %d min of work vs %d min available" % (round(load), need, cap))
    if tt["fixed_from"]:
        parts.append("fixed matters at %s" % _fmt(tt["fixed_from"]))
    if tt["unread"]:
        parts.append("note not fully understood, check it yourself: '%s'" % "'; '".join(u[:120] for u in tt["unread"][:2]))
    if item["tagged"]:
        parts.append("tagged: will be called with the item just above it")
    det = header.get("determination", "")
    m = re.search(r"ITEM NO\.?\s*(\d+)\s*TO\s*ITEM NO\.?\s*(\d+)[^.]*?(MONTHLY|COMBINED)", det.upper())
    if m:
        parts.append("determination also assigns items %s-%s of a combined/monthly list" % (m.group(1), m.group(2)))
    eta_txt = _fmt(eta) if eta is not None and level in ("HIGH", "MODERATE") else ""
    return {
        "level": level,
        "realistic": level in ("HIGH", "MODERATE"),
        "eta": eta_txt,
        "position": "Item %s%s of %d" % ("wt " if item["tagged"] else "", item["serial"], total),
        "comment": "; ".join(parts),
        "closes": closes,
    }


def _ordinal(n):
    return "%d%s" % (n, "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def plan_lines(plan, mine=()):
    """Short text lines for the day plan; 'mine' = serials of your items."""
    out = []
    for s in plan["sections"]:
        slot = " [%s-%s]" % (_fmt(s["slot"][0]), _fmt(s["slot"][1])) if s["slot"] else ""
        fx = ", %d fixed" % s["fixed"] if s["fixed"] else ""
        you = [str(x) for x in mine if s["first"] <= x <= s["last"]]
        out.append("%d-%d %s (%d%s)%s%s" % (s["first"], s["last"], s["name"].title(), s["count"], fx, slot,
                                           ("  ← your item %s" % ", ".join(you)) if you else ""))
    tt = plan["tt"]
    if tt["fixed_from"]:
        out.append("Fixed matters from %s" % _fmt(tt["fixed_from"]))
    if tt["other_from"]:
        out.append("Other work from %s" % _fmt(tt["other_from"]))
    return out


# ------------------------------------------------------------------ main entry
def _date_words(s):
    """'4TH. AUGUST, 2025' or '07.09.2026' -> '04082025'."""
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", s)
    if m:
        return "%02d%02d%s" % (int(m.group(1)), int(m.group(2)), m.group(3))
    m = re.search(r"(\d{1,2})\s*(?:ST|ND|RD|TH)?\.?\s+([A-Z]+),?\s+(\d{4})", s)
    if m and m.group(2)[:3] in [x[:3] for x in MONTHS]:
        return "%02d%02d%s" % (int(m.group(1)), [x[:3] for x in MONTHS].index(m.group(2)[:3]) + 1, m.group(3))
    return ""


def monthly_refs(header):
    """Ranges of a monthly list this Bench takes up today: [(first, last, judge or '', list date or '')]."""
    text = re.sub(r"\s+", " ", (header.get("determination", "") + " " + header.get("notes", "")).upper())
    out = []
    pats = [r"ITEM\s*NO\.?\s*(\d+)\s*(?:TO|-)\s*(?:ITEM\s*NO\.?\s*)?(\d+)\s*(?:OF|FROM)\s*THE\s*(?:COMBINED\s*)?MONTHLY",
            r"FROM\s+ITEM\s*NO\.?\s*(\d+)()(?:\s*\([^)]*\))?\s*(?:OF\s+THE\s+)?(?:MONTHLY|COMBINED)",
            r"MONTHLY LIST\s*(?:DATED\s+[^,]{4,30}?)?\s*FROM\s+ITEM\s*NOS?[.\-\s]*(\d+)\s*(?:-|TO)\s*(\d+)"]
    for pat in pats:
        for m in re.finditer(pat, text):
            tail = re.split(r"ITEM\s*NO", text[m.end():m.end() + 260])[0]
            dm = re.search(r"DATED\s+(\d{1,2}\s*(?:ST|ND|RD|TH)?\.?\s+[A-Z]+,?\s+\d{4}|\d{1,2}[./-]\d{1,2}[./-]\d{4})", tail)
            jm = re.search(r"OF\s+(?:THE\s+)?HON.BLE\s+(?:DR\.?\s+)?JUSTICE\s+([A-Z .()]+?)(?=,|\.|\s+EXCEPT|\s+AND\s|$)", tail)
            out.append((int(m.group(1)), int(m.group(2)) if m.group(2) else 99999,
                        jm.group(1).strip() if jm else "", _date_words(dm.group(1)) if dm else ""))
    return out


def analyse(pdf_path, names, side="A", list_date="", kind="daily", monthly_lookup=None):
    """Your matters in one list PDF, each with a likelihood and the court's day plan.
    monthly_lookup(date or ""): your matters in the monthly list of that date (or the
    latest one), to link 'items x to y of the monthly list' directions in today's list."""
    doc = fitz.open(pdf_path)
    hit_pages = [i for i, p in enumerate(doc) if name_hit(p.get_text(), names)]
    ref_pages = []
    if monthly_lookup:  # courts whose header points to a monthly list range
        for i, p in enumerate(doc):
            t = p.get_text()
            if "COURT NO." in t and "MONTHLY" in t.upper() and monthly_refs({"determination": t}):
                ref_pages.append(i)
    rows, seen = [], set()
    label = SIDES[side][2] + ((" - " + KINDS[kind][1]) if KINDS[kind][1] else "")
    for pno in hit_pages + ref_pages:
        s, e = court_page_range(doc, pno)
        if (s, e) in seen:
            continue
        seen.add((s, e))
        header = parse_header(page_lines(doc[s]))
        items = parse_items(doc, s, e)
        plan = day_plan(items, header, list_date, side)
        mine = [it for it in items if name_hit(it["advocates_text"], names)]
        base = dict(side=side, side_label=label, kind=kind, court_no=header["court_no"], bench=header["bench"],
                    time=header["time"], judges=" & ".join(header["judges"]), determination=header["determination"],
                    notes=header["notes"], vc_link=header["vc_link"], fixed_code=fixed_code(items))
        for it in mine:
            a = assess(it, items, header, plan, list_date, side)
            rows.append(dict(base, case_no=it["case_no"], case_name=it["case_name"], serial=it["serial"],
                             tagged=it["tagged"], item_no=("wt " if it["tagged"] else "") + str(it["serial"]),
                             section=it["section"], page=it["page"], fixed=it.get("fixed"),
                             plan=plan_lines(plan, [x["serial"] for x in mine]),
                             plan_code=plan_code(plan), **a))
        # this Bench also takes up a range of a monthly list today: are any of your monthly matters in it?
        for first, last, judge, mdate in (monthly_refs(header) if monthly_lookup else []):
            who = judge or " ".join(header["judges"])
            try:
                mrows = monthly_lookup(mdate) or []
            except Exception as ex:
                print("monthly list %s not available: %s" % (mdate or "latest", ex))
                mrows = []
            for mr in mrows:
                if not (first <= mr["serial"] <= last):
                    continue
                if not _judge_match(who, mr.get("judges", "")):
                    continue
                daily_load = sum(section_weight(i["section"])[0] for i in items if not i["tagged"] and not i.get("fixed"))
                need = (daily_load + (mr["serial"] - first)) * MIN_PER_UNIT
                cap = _work_minutes(plan["sit"], _hm(DAY_END))
                level = _level(need / cap if cap else 9)
                rows.append(dict(base, kind="monthly-link", case_no=mr["case_no"], case_name=mr["case_name"],
                                 serial=mr["serial"], tagged=mr.get("tagged", False),
                                 item_no="M-%s" % mr["serial"], section=mr.get("section", ""), page=0, fixed=None,
                                 plan=plan_lines(plan), plan_code=plan_code(plan), level=level,
                                 realistic=level in ("HIGH", "MODERATE"), eta="", closes="",
                                 position="Monthly list item %s" % mr["serial"],
                                 comment="today this Bench also takes up items %s of the monthly list%s of Justice %s "
                                         "(after its daily list of %d items); your monthly item %s is in that range"
                                         % ("%d-%d" % (first, last) if last < 99999 else "from %d" % first,
                                            (" dated %s-%s-%s" % (mdate[:2], mdate[2:4], mdate[4:])) if mdate else "",
                                            re.sub(r"^(HON.BLE\s+)?(DR\.?\s+)?JUSTICE\s+", "", (who or "-").upper()).title(), len(items), mr["serial"])))
    rows.sort(key=lambda r: (int(re.sub(r"\D", "", r["court_no"]) or 0), r["kind"] == "monthly-link", r["serial"]))
    return rows


def _judge_match(a, b):
    wa = {w for w in re.findall(r"[A-Z]{3,}", a.upper()) if w not in ("JUSTICE", "HON", "BLE", "THE")}
    wb = {w for w in re.findall(r"[A-Z]{3,}", b.upper()) if w not in ("JUSTICE", "HON", "BLE", "THE")}
    return bool(wa) and len(wa & wb) >= min(2, len(wa))


def plan_code(plan):
    """Compact day plan for the board watcher: [[first, last, short name, slot start, slot end], ...]."""
    out = []
    for s in plan["sections"]:
        name = re.sub(r"\s+", " ", re.sub(r"APPLICATIONS?|MATTERS|\bFOR\b|\bOF\b", "", s["name"].upper())).strip()[:22]
        out.append([s["first"], s["last"], name or s["name"][:22]] + (list(s["slot"]) if s["slot"] else []))
    return out


def fixed_code(items):
    return [[i["serial"], i["fixed"]] for i in items if i.get("fixed")]
