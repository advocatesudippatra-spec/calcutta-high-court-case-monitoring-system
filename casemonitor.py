#!/usr/bin/env python3
"""
Calcutta High Court Case Monitoring System - cause list reports, reminders, live board
alerts and roster questions on Telegram, for any advocate (set your name with `setup`).

Runs every 5 minutes on every Mac where it is installed (launchd). Whichever Mac
is awake does the job that is due; the others see it is done and stay quiet.
The shared "memory" (what was sent, paused or not, your ntfy topic, which extra
sides you asked for) lives in a pinned status message in your Telegram chat with
the bot, so all Macs agree.

Schedule (India time):
  8:30 PM, 9:30 PM   heads-up for tomorrow, only if a matter is realistically likely
  10:00 PM           full report for the next list day (Appellate Side), with each
                     court's day plan; then a question: Original Side / Jalpaiguri too?
  after your answer  report for the side(s) you picked (within about 5 minutes)
  Saturday/Sunday    Monday's list is checked from Saturday noon; report as soon as it is out
  00:00 - 8:29 AM    catch-up: if last night's report was missed, send it on wake
  8:30, 9:30, 10 AM  reminders listing every matter today, with its likelihood
  9:30 AM            board watcher code (Appellate Side, with each court's day plan)
  new monthly list   a "Monthly list" report of all your matters in it

Commands (send to your bot on Telegram, from any phone or Mac):
  /stop     pause everything on all Macs        /resume   start again
  /status   show what was sent and by which Mac /now      send the report now
  /os /jal /both    add Original Side / Jalpaiguri / both for the next list day
  /monthly  your matters in the latest monthly list      /ntfy  show the ntfy topic
  Anything else in plain words is a question to the roster: "group 6", "court 35",
  "mentioning 25", "fixed 35", "justice a b ghosh", "find 1234", "advocate x",
  "running 12", "board" (see /help)
  /courton [HH:MM]  keep the Mac awake for court   /courtoff  let it sleep again
  /rise 3:30 pm     courts rise early today (or forward/send the notice, even as a photo)
  /rise cancel      back to normal hours           /watchgroup  (sent by you inside a group) read notices there

Terminal:
  casemonitor tick                 (what launchd runs)
  casemonitor report --date DDMMYYYY [--sides A,O,J] [--dry-run]
  casemonitor monthly [--date DDMMYYYY] [--dry-run]
  casemonitor board                (open the display board in Chrome)
  casemonitor watcher-settings  (settings code, only for a Mac without the program)
  casemonitor ntfy-topic           (show this Mac's ntfy topic)
  casemonitor ask "group 6"        (ask the roster a question, answered here)
  casemonitor listen               (always-on: instant Telegram replies + board link; LISTENER=1)
  casemonitor backfill --from DDMMYYYY [--sides A,O,J]   (load past lists for history questions)
  casemonitor forget-database [--before DDMMYYYY]       (delete stored lists: only when you want)
  casemonitor stop | resume | status
  casemonitor setup             (all the questions: name, bot, phone, alarms, sides, main Mac)
  casemonitor simulate --at "2026-09-26 14:00"   (test the schedule, nothing is sent)
"""
import argparse
import datetime as dt
import glob
import html
import json
import os
import random
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import causelist as cl  # noqa: E402
import roster  # noqa: E402

HOME = os.environ.get("CASEMONITOR_HOME") or os.path.expanduser("~/.casemonitor")
CONFIG = os.path.join(HOME, "config.env")
CACHE = os.path.join(HOME, "cache")
MONTHLY = os.path.join(HOME, "monthly")
REPORTS = os.path.join(HOME, "reports")
LOCAL_STATE = os.path.join(HOME, "state.json")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
STATE_TAG = "CASEMONITOR-STATE"
BOARD_URL = "https://display.calcuttahighcourt.gov.in/principal.php"
NTFY_OK = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
PLACEHOLDER = "YOUR-NTFY-TOPIC"


# ------------------------------------------------------------------ config
def load_config():
    cfg = {"ADVOCATE_NAMES": "", "DEVICE_NAME": socket.gethostname().split(".")[0]}
    if os.path.exists(CONFIG):
        for line in open(CONFIG):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"')
    for k in ("MIN_PER_UNIT", "DAY_END"):
        if k in cfg:
            os.environ[k] = cfg[k]
    cfg["names"] = [n.strip() for n in cfg["ADVOCATE_NAMES"].split(",") if n.strip()]
    # Appellate Side is checked every night; Original Side / Jalpaiguri only when you ask.
    cfg["sides"] = _sides(cfg.get("AUTO_SIDES", "A")) or ["A"]
    cfg["ask_sides"] = [s for s in _sides(cfg.get("ASK_SIDES", "O,J")) if s not in cfg["sides"]]
    return cfg


def _sides(text):
    return [s.strip().upper() for s in (text or "").split(",") if s.strip().upper() in cl.SIDES]


def set_config(key, value):
    os.makedirs(HOME, exist_ok=True)
    lines = [l for l in (open(CONFIG).read().splitlines() if os.path.exists(CONFIG) else [])
             if not l.startswith(key + "=")]
    lines.append("%s=%s" % (key, value))
    open(CONFIG, "w").write("\n".join(lines) + "\n")
    os.chmod(CONFIG, 0o600)


def valid_topic(t):
    return bool(t) and t != PLACEHOLDER and bool(NTFY_OK.match(t))


def now_ist(override=None):
    if override:
        return dt.datetime.strptime(override, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    return dt.datetime.now(IST)


def dstr(d):
    return d.strftime("%d%m%Y")


def ddate(s):
    return dt.datetime.strptime(s, "%d%m%Y").date()


def pretty(date_str):
    return "%s-%s-%s" % (date_str[:2], date_str[2:4], date_str[4:])


def ymd(date_str):
    return date_str[4:] + date_str[2:4] + date_str[:2]


def next_weekday(d):
    d = d + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


# ------------------------------------------------------------------ telegram
class Telegram(object):
    def __init__(self, token, chat):
        self.token, self.chat = token, str(chat)

    def call(self, method, params=None, files=None):
        url = "https://api.telegram.org/bot%s/%s" % (self.token, method)
        if files:
            boundary = uuid.uuid4().hex
            body = b""
            for k, v in (params or {}).items():
                body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode()
            for k, (fname, data) in files.items():
                body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                         "Content-Type: application/octet-stream\r\n\r\n" % (boundary, k, fname)).encode() + data + b"\r\n"
            body += ("--%s--\r\n" % boundary).encode()
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        else:
            req = urllib.request.Request(url, data=json.dumps(params or {}).encode(),
                                         headers={"Content-Type": "application/json"})
        last = None
        for n in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60, context=cl._ssl_ctx()) as r:
                    res = json.loads(r.read().decode())
                break
            except urllib.error.HTTPError as e:
                res = json.loads(e.read().decode() or "{}")
                break
            except (urllib.error.URLError, OSError) as e:
                last = e
                time.sleep(3 * (n + 1))
        else:
            raise last
        if not res.get("ok"):
            raise RuntimeError("Telegram %s failed: %s" % (method, res))
        return res["result"]

    def send(self, text, silent=False, buttons=None):
        chunks, cur = [], ""
        for line in text.split("\n"):
            if len(cur) + len(line) > 3800:
                chunks.append(cur)
                cur = ""
            cur += line + "\n"
        chunks.append(cur)
        chunks = [c for c in chunks if c.strip()]
        last = None
        for i, c in enumerate(chunks):
            p = {"chat_id": self.chat, "text": c, "parse_mode": "HTML",
                 "disable_web_page_preview": True, "disable_notification": silent}
            if buttons and i == len(chunks) - 1:
                p["reply_markup"] = {"inline_keyboard": buttons}
            last = self.call("sendMessage", p)
        return last

    def send_file(self, path, caption=""):
        with open(path, "rb") as f:
            self.call("sendDocument", {"chat_id": self.chat, "caption": caption},
                      files={"document": (os.path.basename(path), f.read())})


def ntfy(topic, html_text, alarm=False):
    """Mirror a message to the ntfy app (plain text, first 3500 characters)."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", html_text)).strip()
    title, _, body = plain.partition("\n")
    try:
        req = urllib.request.Request("https://ntfy.sh/" + urllib.parse.quote(topic), data=body.strip()[:3500].encode(),
                                     headers={"Title": title[:120].encode("ascii", "ignore").decode() or "Case Monitor",
                                              "Priority": "5" if alarm else "4", "Tags": "scales"})
        urllib.request.urlopen(req, timeout=30, context=cl._ssl_ctx())
    except Exception as ex:
        print("ntfy failed:", ex)


def pushover(cfg, html_text, alarm=False):
    """iPhone (or Android) alarm through Pushover: emergency priority repeats until acknowledged."""
    user, token = cfg.get("PUSHOVER_USER"), cfg.get("PUSHOVER_TOKEN")
    if not (user and token):
        return
    plain = html.unescape(re.sub(r"<[^>]+>", "", html_text)).strip()
    title, _, body = plain.partition("\n")
    data = {"token": token, "user": user, "title": title[:250] or "Case Monitor", "message": (body.strip() or title)[:1000],
            "priority": 2 if alarm else 0}
    if alarm:
        data.update({"retry": 60, "expire": 1800, "sound": "siren"})
    try:
        urllib.request.urlopen(urllib.request.Request("https://api.pushover.net/1/messages.json",
                                                      data=urllib.parse.urlencode(data).encode()),
                               timeout=30, context=cl._ssl_ctx())
    except Exception as ex:
        print("pushover failed:", ex)


def notify(cfg, text, alarm=False):
    """Mirror a message to the alarm apps that are set up (ntfy for Android, Pushover for iPhone)."""
    if cfg.get("NTFY_TOPIC"):
        ntfy(cfg["NTFY_TOPIC"], text, alarm)
    pushover(cfg, text, alarm)


# ------------------------------------------------------------------ shared state
class State(object):
    """Shared across Macs via a pinned Telegram message; local file if no Telegram."""

    def __init__(self, tg):
        self.tg, self.msg_id = tg, None
        self.data = {"paused": False, "sent": {}, "upd": 0, "xs": {}}

    def load(self):
        if self.tg:
            chat = self.tg.call("getChat", {"chat_id": self.tg.chat})
            pm = chat.get("pinned_message") or {}
            text = pm.get("text") or ""
            if STATE_TAG in text:
                try:
                    self.data = json.loads(text.split(STATE_TAG, 1)[1].strip())
                    self.msg_id = pm["message_id"]
                except Exception:
                    pass
        elif os.path.exists(LOCAL_STATE):
            self.data = json.load(open(LOCAL_STATE))
        self.data.setdefault("sent", {})
        self.data.setdefault("xs", {})
        return self

    def _prune(self):
        cutoff = (dt.datetime.now(IST) - dt.timedelta(days=4)).strftime("%Y%m%d")

        def keep(k):
            d = k.split(":")[-1]
            return not (len(d) == 8 and d.isdigit()) or ymd(d) >= cutoff or k.startswith("mon:")
        self.data["sent"] = {k: v for k, v in self.data["sent"].items() if keep(k)}
        mons = sorted((k for k in self.data["sent"] if k.startswith("mon:")), key=lambda k: ymd(k.split(":")[-1]))
        for k in mons[:-6]:  # remember the last few monthly lists only
            self.data["sent"].pop(k, None)
        self.data["xs"] = {d: v for d, v in self.data.get("xs", {}).items() if ymd(d) >= cutoff}
        self.data["rise"] = {d: v for d, v in (self.data.get("rise") or {}).items() if ymd(d) >= cutoff}

    def save(self):
        self._prune()
        if not self.tg:
            os.makedirs(HOME, exist_ok=True)
            json.dump(self.data, open(LOCAL_STATE, "w"))
            return
        status = "PAUSED (send /resume)" if self.data.get("paused") else "ACTIVE"
        text = ("Case Monitor bot: %s\nLast update: %s by %s\n(Keep this message pinned. It is the bots' shared memory.)\n\n%s %s"
                % (status, dt.datetime.now(IST).strftime("%d-%m %I:%M %p"), load_config()["DEVICE_NAME"],
                   STATE_TAG, json.dumps(self.data, separators=(",", ":"))))
        if self.msg_id:
            try:
                self.tg.call("editMessageText", {"chat_id": self.tg.chat, "message_id": self.msg_id, "text": text})
                return
            except RuntimeError as e:
                if "not modified" in str(e):
                    return
        m = self.tg.call("sendMessage", {"chat_id": self.tg.chat, "text": text, "disable_notification": True})
        self.msg_id = m["message_id"]
        self.tg.call("pinChatMessage", {"chat_id": self.tg.chat, "message_id": self.msg_id, "disable_notification": True})

    def done(self, key):
        return key in self.data["sent"]

    def claim(self, key, device):
        """Mark a job as ours; re-check after a pause so two Macs don't both send."""
        self.data["sent"][key] = "claim:%s" % device
        self.save()
        time.sleep(4)
        self.load()
        return self.data["sent"].get(key) == "claim:%s" % device

    def mark(self, key, device):
        self.data["sent"][key] = "%s@%s" % (device, dt.datetime.now(IST).strftime("%H:%M"))
        self.save()


def sync_ntfy(cfg, state):
    """One ntfy topic for all Macs: the one in the shared state wins; if there is none yet,
    this Mac's topic (or a new random one) becomes the shared one."""
    shared, mine = state.data.get("ntfy", ""), cfg.get("NTFY_TOPIC", "")
    if valid_topic(shared):
        if mine != shared:
            set_config("NTFY_TOPIC", shared)
            cfg["NTFY_TOPIC"] = shared
        return False
    if not valid_topic(mine):
        mine = "casemonitor-%s" % uuid.uuid4().hex[:12]
        set_config("NTFY_TOPIC", mine)
        cfg["NTFY_TOPIC"] = mine
    state.data["ntfy"] = mine
    return True


# ------------------------------------------------------------------ monthly lists
def _slim(r):
    return {k: r.get(k) for k in ("serial", "tagged", "court_no", "judges", "case_no", "case_name", "section",
                                  "bench", "time", "side", "side_label")}


def monthly_rows(cfg, side, date_str, fetch=True):
    """Your matters in the monthly list of that date (None if there is no such list).
    Only your rows are kept (a small file); the big PDF is deleted after reading."""
    os.makedirs(MONTHLY, exist_ok=True)
    path = os.path.join(MONTHLY, "%s_%s.json" % (side, date_str))
    names = sorted(n.upper() for n in cfg["names"])
    if os.path.exists(path):
        saved = json.load(open(path))
        if saved.get("names") == names:  # saved for the same name(s); a changed name reads the list again
            return saved["rows"]
        if not fetch:
            return None
    if not fetch:
        return None
    pdf = cl.download(date_str, side, CACHE, kind="monthly")
    if not pdf:
        return None
    rows = [_slim(r) for r in cl.analyse(pdf, cfg["names"], side, date_str, kind="monthly")]
    json.dump({"side": side, "date": date_str, "names": names, "rows": rows}, open(path, "w"))
    try:
        os.remove(pdf)
    except OSError:
        pass
    return rows


def known_monthlies(side):
    out = []
    for p in glob.glob(os.path.join(MONTHLY, "%s_*.json" % side)):
        d = os.path.basename(p)[len(side) + 1:-5]
        if len(d) == 8:
            out.append(d)
    return sorted(out, key=ymd)


def latest_monthly(cfg, side, upto):
    ds = [d for d in known_monthlies(side) if ymd(d) <= ymd(upto)]
    return (ds[-1], monthly_rows(cfg, side, ds[-1], fetch=False)) if ds else (None, None)


def prune_monthlies():
    cutoff = (dt.datetime.now(IST) - dt.timedelta(days=400)).strftime("%Y%m%d")
    for p in glob.glob(os.path.join(MONTHLY, "*.json")):
        d = os.path.basename(p).split("_")[-1][:-5]
        if len(d) == 8 and ymd(d) < cutoff:
            os.remove(p)


def prune_cache(days=10):
    """Delete downloaded list PDFs older than `days` (the database keeps what questions need)."""
    cutoff = time.time() - days * 86400
    for root, _, files in os.walk(CACHE):
        for f in files:
            p = os.path.join(root, f)
            if f.lower().endswith((".pdf", ".part")) and os.path.getmtime(p) < cutoff:
                try:
                    os.remove(p)
                except OSError:
                    pass


def probe_monthlies(cfg, today):
    """Look for new monthly lists: dated from today to a week ahead (they are dated with their
    first hearing day and published a few days before). First run: the whole month so far."""
    found = []
    for side in cfg["sides"]:
        known = known_monthlies(side)
        start = today - dt.timedelta(days=0 if any(ymd(d)[:6] == today.strftime("%Y%m") for d in known)
                                     else today.day - 1)
        d = start
        while d <= today + dt.timedelta(days=7):
            ds = dstr(d)
            if d.weekday() < 5 and ds not in known and cl.exists(ds, side, "monthly"):
                found.append((side, ds))
            d += dt.timedelta(days=1)
    return found


# ------------------------------------------------------------------ analysis
def analyse_day(cfg, date_str, refresh=False, sides=None, day_end=None):
    """Returns (published_daily, rows) for the sides given (default: the nightly sides),
    including supplementary lists and links into monthly lists. day_end (minutes) = courts
    rise early that day (from a notice)."""
    saved = cl.DAY_END
    if day_end:
        cl.DAY_END = "%02d:%02d" % divmod(min(day_end, cl._hm(saved)), 60)
    try:
        return _analyse_day(cfg, date_str, refresh, sides)
    finally:
        cl.DAY_END = saved


def _analyse_day(cfg, date_str, refresh=False, sides=None):
    rows, published = [], False
    for side in sides or cfg["sides"]:
        def lookup(mdate, side=side):
            if mdate:
                return monthly_rows(cfg, side, mdate)
            return latest_monthly(cfg, side, date_str)[1]
        path = cl.download(date_str, side, CACHE, refresh=refresh)
        if not path:
            continue
        published = True
        rows += cl.analyse(path, cfg["names"], side, date_str, "daily", lookup)
        for kind in cl.SUP_KINDS:
            spath = cl.download(date_str, side, CACHE, refresh=refresh, kind=kind) if (
                refresh or os.path.exists(os.path.join(CACHE, kind, os.path.basename(cl.pdf_url(date_str, side, kind))))
                or cl.exists(date_str, side, kind)) else None
            if not spath:
                break
            rows += cl.analyse(spath, cfg["names"], side, date_str, kind, lookup)
    # the same case can be both in the list and linked from a monthly range: keep the list entry
    seen, out = set(), []
    for r in rows:
        k = (r["side"], r["court_no"], r["case_no"])
        if r["kind"] == "monthly-link" and k in seen:
            continue
        seen.add(k)
        out.append(r)
    return published, out


def watch_code(rows, date_str):
    """Board watcher code: your Appellate Side items plus each court's day plan."""
    items, plans, fixed = [], {}, {}
    for r in rows:
        if r["side"] != "A" or r["level"] == "NONE":
            continue
        it = {"c": r["court_no"], "i": r["serial"], "k": r["case_no"]}
        if r["kind"] == "monthly-link":
            it["m"] = 1  # watched only while the board shows a non-daily (monthly) list
        elif r.get("section"):
            it["s"] = cl.plan_code({"sections": [{"name": r["section"], "first": 0, "last": 0, "slot": None}]})[0][2]
        items.append(it)
        if r["kind"] != "monthly-link" and r.get("plan_code"):
            plans.setdefault(r["court_no"], r["plan_code"])
            if r.get("fixed_code"):
                fixed.setdefault(r["court_no"], r["fixed_code"])
    code = {"v": 2, "d": date_str, "w": items, "p": plans}
    if fixed:
        code["f"] = fixed
    return json.dumps(code, separators=(",", ":"))


LEVEL_ICON = {"HIGH": "🟢", "MODERATE": "🟡", "LOW": "⚪", "VERY LOW": "⚪", "NONE": "⛔"}


def _e(s):
    return html.escape(s or "", quote=False)


def _court_key(r):
    return (r["side_label"], r["court_no"])


def full_report_text(rows, date_str, cfg, header="Cause list report", sides=None):
    likely = [r for r in rows if r["realistic"]]
    out = ["<b>%s: %s</b>" % (header, pretty(date_str)),
           "Sides checked: %s. Matters of %s: <b>%d</b>, realistically likely: <b>%d</b>"
           % (", ".join(cl.SIDES[s][2] for s in (sides or cfg["sides"])), _e(", ".join(cfg["names"])),
              len(rows), len(likely))]
    if not rows:
        out.append("\nNo matter listed.")
    shown_plan = set()
    for r in rows:
        k = _court_key(r)
        if k not in shown_plan and r.get("plan") and r["kind"] != "monthly-link":
            shown_plan.add(k)
            out.append("\n<b>━ %s, Court %s</b> (%s, %s)\n%s" % (_e(r["side_label"]), _e(r["court_no"]),
                                                               _e(r["judges"]), _e(r["time"] or "time not given"),
                                                               "<i>Day plan:</i>\n" + "\n".join("• " + _e(l) for l in r["plan"])))
        out.append("\n%s <b>%s</b>  %s\n%s\n<b>%s</b>%s\n<i>Why: %s</i>" % (
            LEVEL_ICON.get(r["level"], "⚪"), _e(r["position"]), _e(r["case_no"]), _e(r["case_name"]),
            _e(r["level"]), (" (around %s)" % r["eta"]) if r["eta"] else "", _e(r["comment"])))
        if r["kind"] == "monthly-link":
            out.append("<i>(From the monthly list, Court %s, %s)</i>" % (_e(r["court_no"]), _e(r["judges"])))
        if r["vc_link"].startswith("http"):
            out.append('<a href="%s">VC link</a>' % _e(r["vc_link"]))
    out.append("\n<i>Likelihood is a rule-of-thumb estimate from the item's heading, the Bench's note and "
               "the items ahead. Check the determination yourself.</i>")
    return "\n".join(out)


def reminder_text(rows, date_str, label, only_likely=False, board=False):
    shown = [r for r in rows if r["realistic"]] if only_likely else rows
    if not shown:
        return None
    out = ["<b>⏰ %s: %s</b>" % (label, pretty(date_str))]
    for r in shown:
        out.append("%s Court %s (%s): <b>%s</b> %s\n%s\n%s%s%s" % (
            LEVEL_ICON.get(r["level"], "⚪"), _e(r["court_no"]), _e(r["side_label"]), _e(r["position"]),
            _e(r["case_no"]), _e(r["case_name"][:80]),
            _e(r["level"]) + ((", around " + r["eta"]) if r["eta"] else ""),
            ("\n" + _e(r["closes"][:140])) if r.get("closes") else "",
            ('\n<a href="%s">VC link</a>' % _e(r["vc_link"])) if r["vc_link"].startswith("http") else ""))
    if board:
        out.append('📺 Open the <a href="%s">display board</a> in Chrome now and type the CAPTCHA, '
                   'so the watcher can alert you.' % BOARD_URL)
    return "\n\n".join(out)


def monthly_report_text(rows, side, date_str):
    out = ["<b>📚 Monthly list: %s, dated %s</b>" % (_e(cl.SIDES[side][2]), pretty(date_str)),
           "Your matters in it: <b>%d</b>" % len(rows)]
    last = None
    for r in rows:
        k = r["court_no"]
        if k != last:
            out.append("\n<b>Court %s</b>, %s" % (_e(r["court_no"]), _e(r["judges"])))
            last = k
        out.append("• Item %s%s  %s\n   %s\n   <i>%s</i>" % ("wt " if r.get("tagged") else "", r["serial"],
                                                          _e(r["case_no"]), _e((r["case_name"] or "")[:80]),
                                                          _e((r.get("section") or "").title())))
    out.append("\n<i>Benches take these up on the days their daily list says so (e.g. 'items 415 to 531 of the "
               "monthly list'). The nightly report will tell you when one of these falls in such a range.</i>")
    return "\n".join(out)


def save_html(rows, date_str, cfg, sides=None):
    os.makedirs(REPORTS, exist_ok=True)
    trs = "".join(
        "<tr><td>%s<br><b>%s</b><div class=s>%s<br>%s</div></td><td>%s</td><td class=det>%s%s%s</td>"
        "<td><b>%s</b><div class=s>%s</div></td><td class=n>%s</td><td><b>%s</b>%s<div class=s>%s</div>%s</td></tr>" % (
            _e(r["side_label"]), _e(r["court_no"]), _e(r["bench"]), _e(r["time"]), _e(r["judges"]),
            _e(r["determination"]), ("<div class=s><b>Notes:</b> %s</div>" % _e(r["notes"])) if r["notes"] else "",
            ("<div class=s><b>Day plan:</b><br>%s</div>" % "<br>".join(_e(l) for l in r.get("plan") or [])),
            _e(r["case_no"]), _e(r["case_name"]), _e(r["item_no"]), _e(r["level"]),
            (" (around %s)" % _e(r["eta"])) if r["eta"] else "", _e(r["comment"]),
            ("<div class=s><a href='%s'>VC link</a></div>" % _e(r["vc_link"])) if r["vc_link"].startswith("http") else "")
        for r in rows)
    page = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Cause List %s</title><style>
:root{--bg:#fff;--fg:#1a1a1a;--mut:#555;--line:#ccc;--head:#f1f1f1}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--mut:#aaa;--line:#444;--head:#262626}}
body{font:14px/1.45 system-ui,sans-serif;margin:16px;color:var(--fg);background:var(--bg)}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%%;min-width:760px}
th,td{border:1px solid var(--line);padding:6px;vertical-align:top;text-align:left}th{background:var(--head)}
.s{color:var(--mut);font-size:12px}.det{font-size:12px;max-width:380px}.n{text-align:center;font-weight:700}a{color:inherit}
</style><h2>Cause list %s: matters of %s</h2>
<p class=s>Sides: %s. %d matter(s). "Likely" is a rule-of-thumb estimate, not a prediction by the Court. Check the determination column yourself.</p>
<div class=wrap><table><tr><th>Court</th><th>Judge(s)</th><th>Determination, notes and day plan</th><th>Case</th><th>Item</th><th>Likely, and why</th></tr>%s</table></div>""" % (
        pretty(date_str), pretty(date_str), _e(", ".join(cfg["names"])),
        _e(", ".join(cl.SIDES[s][2] for s in (sides or cfg["sides"]))), len(rows), trs)
    path = os.path.join(REPORTS, "report_%s%s.html" % (date_str, "" if not sides else "_" + "".join(sides)))
    open(path, "w").write(page)
    return path


# ------------------------------------------------------------------ commands from phone
SIDE_BUTTONS = [[{"text": "Original Side", "callback_data": "xs:O"}, {"text": "Jalpaiguri", "callback_data": "xs:J"}],
                [{"text": "Both", "callback_data": "xs:OJ"}, {"text": "No, Appellate only", "callback_data": "xs:-"}]]


def request_sides(state, date_str, sides):
    cur = set(state.data["xs"].get(date_str, ""))
    cur |= set(sides)
    state.data["xs"][date_str] = "".join(sorted(cur))


def process_commands(tg, state, cfg, out, now):
    """Read new Telegram messages (used by the 5-minute check when no listener is running)."""
    try:
        ups = tg.call("getUpdates", {"offset": int(state.data.get("upd", 0)) + 1, "timeout": 0,
                                     "allowed_updates": ["message", "callback_query"]})
    except Exception as ex:
        print("getUpdates failed:", ex)
        return False
    for u in ups:
        state.data["upd"] = u["update_id"]
        handle_update(u, tg, state, cfg, out, now)
    if ups:
        state.save()
    return bool(ups)


def handle_update(u, tg, state, cfg, out, now):
    """One Telegram update: a button tap, a /command, a PDF, or a question in plain words."""
    nxt = state.data.get("next_list") or dstr(list_day_for(now))
    cq = u.get("callback_query")
    if cq:
        if str(((cq.get("message") or {}).get("chat") or {}).get("id")) != tg.chat:
            return
        data = cq.get("data", "")
        try:
            tg.call("answerCallbackQuery", {"callback_query_id": cq["id"]})
        except Exception:
            pass
        if data.startswith("xs:"):
            parts = data.split(":")
            d = parts[2] if len(parts) > 2 else nxt
            pick = parts[1]
            if pick == "-":
                state.data["xs"][d] = state.data["xs"].get(d, "") or "-"
                out("OK, Appellate Side only for %s." % pretty(d), silent=True)
            else:
                request_sides(state, d, pick)
                out("OK: %s for %s. The report follows within about 5 minutes."
                    % (" and ".join(cl.SIDES[s][2] for s in pick), pretty(d)), silent=True)
        return
    msg = u.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id"))
    from_id = str((msg.get("from") or {}).get("id"))
    groups = state.data.setdefault("groups", [])
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if chat_id != tg.chat:
        # a Telegram group: only you can register it; after that, only notices are read there
        if from_id == tg.chat and text.lower().startswith("/watchgroup"):
            if chat_id not in groups:
                groups.append(chat_id)
            out("👂 I'll read court notices posted in <b>%s</b> (e.g. courts rising early)." % _e((msg.get("chat") or {}).get("title", "the group")))
            return
        if chat_id not in groups:
            return  # ignore anyone else who finds the bot
        body = text
        try:
            if msg.get("photo"):
                body += "\n" + text_of_file(tg, msg["photo"][-1]["file_id"], "notice_%s.jpg" % msg.get("message_id"))[1]
            elif msg.get("document") and (msg["document"].get("mime_type", "").startswith("image/")
                                          or msg["document"].get("file_name", "").lower().endswith(".pdf")):
                body += "\n" + text_of_file(tg, msg["document"]["file_id"], msg["document"].get("file_name"))[1]
        except Exception as ex:
            print("group file read failed:", ex)
        n = parse_notice(body, now)
        if n:
            apply_notice(state, cfg, out, now, n, "group")
        return
    forwarded = bool(msg.get("forward_origin") or msg.get("forward_from") or msg.get("forward_from_chat"))
    if msg.get("photo"):
        try:
            _, body = text_of_file(tg, msg["photo"][-1]["file_id"], "photo_%s.jpg" % msg.get("message_id"))
        except Exception as ex:
            out("Could not read that picture (%s)." % _e(repr(ex)[:120]), silent=True)
            return
        n = parse_notice(text + "\n" + body, now, lenient=True)
        if n:
            apply_notice(state, cfg, out, now, n, "picture")
        else:
            out("I read this from the picture, but found no court timing notice in it:\n<i>%s</i>" % _e(body[:600] or "(no text)"), silent=True)
        return
    if msg.get("document"):
        doc = msg["document"]
        if doc.get("mime_type", "").startswith("image/"):
            _, body = text_of_file(tg, doc["file_id"], doc.get("file_name"))
            n = parse_notice(text + "\n" + body, now, lenient=True)
            if n:
                apply_notice(state, cfg, out, now, n, "picture")
            else:
                out("No court timing notice found in that picture.", silent=True)
            return
        path, body = text_of_file(tg, doc["file_id"], doc.get("file_name"))
        if body.upper().count("COURT NO.") < 3:  # not a cause list: maybe a notice
            n = parse_notice(text + "\n" + body, now, lenient=True)
            if n:
                apply_notice(state, cfg, out, now, n, "PDF")
                return
        out(read_sent_pdf(tg, cfg, doc, path), silent=True)
        return
    cmd = text.split()[0].lower() if text else ""
    if cmd == "/rise" or re.match(r"(?i)^(cancel|undo) (the )?notice", text):
        d = dstr(now.date())
        rest = " ".join(text.split()[1:]).strip() if cmd == "/rise" else "cancel"
        if rest.lower().startswith(("cancel", "off", "none")) or not rest:
            dd = [k for k in (state.data.get("rise") or {}) if ymd(k) >= ymd(d)]
            for k in dd:
                state.data["rise"].pop(k, None)
            out("OK, notice cancelled: normal court hours %s." % ("today" if dd else "(there was none)"), silent=True)
        else:
            n = parse_notice("courts rise at " + rest, now) or parse_notice(rest, now, lenient=True)
            if n:
                apply_notice(state, cfg, out, now, n, "command")
            else:
                out("Send e.g. <code>/rise 3:30 pm</code>, <code>/rise 1 pm tomorrow</code>, or <code>/rise cancel</code>.", silent=True)
        return
    n = parse_notice(text, now, lenient=forwarded) if (forwarded or not cmd.startswith("/")) else None
    if n and (forwarded or re.search(r"RESOLUTION|CONDOLENCE|DEMISE|BAR ASSOCIATION|COURTS? (WILL|SHALL)|NOTICE", text.upper())):
        apply_notice(state, cfg, out, now, n, "forwarded message" if forwarded else "message")
        return
    if cmd == "/stop":
        state.data["paused"] = True
        out("⏸ Paused on all Macs. Send /resume to start again.")
    elif cmd in ("/resume", "/start"):
        state.data["paused"] = False
        out("▶️ Resumed. Next jobs will run on whichever Mac is awake.\n\n" + roster.HELP, silent=True)
    elif cmd == "/status":
        sent = state.data.get("sent", {})
        recent = "\n".join("%s: %s" % (k, v) for k, v in sorted(sent.items())[-10:]) or "nothing yet"
        lis = state.data.get("listener") or {}
        out("Status: %s\nThis reply from: %s\nInstant replies: %s\nntfy topic: %s\nRecent jobs:\n%s" % (
            "PAUSED" if state.data.get("paused") else "ACTIVE", cfg["DEVICE_NAME"],
            ("on (%s)" % lis.get("dev")) if listener_alive(state) else "off (answers within 5 minutes)",
            state.data.get("ntfy", "-"), recent))
    elif cmd == "/now":
        state.data["run_now"] = True
        out("OK, the report follows within about 5 minutes.", silent=True)
    elif cmd in ("/os", "/jal", "/both"):
        pick = {"/os": "O", "/jal": "J", "/both": "OJ"}[cmd]
        request_sides(state, nxt, pick)
        out("OK: %s for %s. The report follows within about 5 minutes."
            % (" and ".join(cl.SIDES[s][2] for s in pick), pretty(nxt)), silent=True)
    elif cmd == "/monthly":
        state.data["run_monthly"] = True
        out("OK, the monthly list summary follows within about 5 minutes.", silent=True)
    elif cmd == "/help":
        out(roster.HELP, silent=True)
    elif cmd == "/ntfy":
        out("ntfy topic (subscribe to it in the ntfy app): <code>%s</code>" % _e(state.data.get("ntfy", "-")))
    elif cmd in ("/courton", "/courtoff"):
        # Court Mode is per-Mac: the Mac that reads this command switches it.
        args = ["start"] + text.split()[1:2] if cmd == "/courton" else ["stop"]
        r = subprocess.run([os.path.join(HERE, "court_mode.sh")] + args, capture_output=True, text=True)
        out("%s: %s" % (cfg["DEVICE_NAME"], (r.stdout or r.stderr).strip().splitlines()[0]))
    elif text:
        try:
            reply = roster.answer(text, cfg["names"])
        except Exception as ex:
            reply = "Sorry, I could not answer that (%s). Send /help for examples." % _e(repr(ex)[:120])
        out(reply, silent=True)


def read_sent_pdf(tg, cfg, doc, path):
    """A cause list PDF sent to the bot: reply with your matters in it."""
    name = doc.get("file_name") or "list.pdf"
    if not name.lower().endswith(".pdf"):
        return "Send a cause list as a PDF file and I will read it."
    try:
        m = re.search(r"(\d{8})", name)
        rows = cl.analyse(path, cfg["names"], "A", m.group(1) if m else "", "daily")
    except Exception as ex:
        return "Could not read that PDF (%s)." % _e(repr(ex)[:150])
    if not rows:
        return "Read <b>%s</b>: no matter of %s in it." % (_e(name), _e(", ".join(cfg["names"])))
    return full_report_text(rows, m.group(1) if m else "00000000", cfg, "Report for the PDF you sent (%s)" % name)


# ------------------------------------------------------------------ court notices (early rising, no work)
NOTICE_WORDS = re.compile(r"RESOLUTION|CONDOLENCE|DEMISE|PASSED AWAY|OBITUARY|BAR ASSOCIATION|BAR LIBRARY|INCORPORATED LAW|"
                          r"\bRISE\b|\bRISING\b|CEASE|ABSTAIN|REFRAIN|WILL NOT FUNCTION|SHALL NOT FUNCTION|NO WORK|"
                          r"WILL NOT SIT|SHALL NOT SIT|COURTS? (?:WILL|SHALL) (?:REMAIN )?CLOSED|HOLIDAY")
WHOLE_DAY = re.compile(r"ABSTAIN|REFRAIN FROM|NO WORK|WILL NOT FUNCTION|SHALL NOT FUNCTION|WILL NOT SIT|SHALL NOT SIT|"
                       r"(?:REMAIN|BE) CLOSED|DECLARED (?:A )?HOLIDAY|FULL DAY")


def parse_notice(text, now, lenient=False):
    """A bar/court notice about the courts rising early or not working.
    Returns {"date": DDMMYYYY, "t": minutes (0 = no court work that day), "why": short text} or None."""
    t = re.sub(r"\s+", " ", (text or "").upper())
    if not NOTICE_WORDS.search(t):
        return None
    tok = cl._tokenise_note(t)
    m = re.search(r"\b(?:RISE|RISING|CEASE[DS]?|STOP|FUNCTION(?:ING)?|WORK|SIT(?:TING)?|UPTO|UP TO|TILL|UNTIL)\b[^<]{0,40}?"
                  r"(?:AT|BY|TILL|UPTO|UP TO|FROM|AFTER)?\s*<T(\d+)>", tok)
    if not m:
        m = re.search(r"\bAFTER\s+<T(\d+)>", tok) if re.search(r"NOT FUNCTION|NO WORK|ABSTAIN|REFRAIN|CEASE", t) else None
    rise = int(m.group(1)) if m else None
    if rise is None:
        if WHOLE_DAY.search(t):
            rise = 0
        elif not lenient:
            return None
        else:
            return None
    d = now.date()
    if "TOMORROW" in t:
        d = d + dt.timedelta(days=1)
    dm = re.search(r"<D(\d{2})\.(\d{2})\.(\d{4})>", tok)
    if dm:
        try:
            d = dt.date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
        except ValueError:
            pass
    why = re.search(r"CONDOLENCE|DEMISE|PASSED AWAY|PASSING AWAY|MARK OF RESPECT|RESOLUTION", t)
    snip = t[why.start():why.start() + 120] if why else t[:120]
    snip = re.split(r"(?<=[A-Z]{3})\.\s", snip)[0]  # stop at a real sentence end, not at "A. B."
    return {"date": dstr(d), "t": rise, "why": snip.strip().capitalize()}


def apply_notice(state, cfg, out, now, notice, source="message"):
    d, t = notice["date"], notice["t"]
    state.data.setdefault("rise", {})[d] = {"t": t, "why": notice["why"][:140]}
    when = "today" if d == dstr(now.date()) else pretty(d)
    if t == 0:
        out("📢 Noted from the %s: <b>no court work %s</b> (%s).\nMorning reminders and board alerts for that day are off. "
            "Send <code>cancel notice</code> if this is wrong." % (source, when, _e(notice["why"])))
        return
    msg = ("📢 Noted from the %s: <b>courts rise at %s %s</b> (%s).\nThe board watcher stops after that time and will not "
           "send 'board not updating' alerts. Send <code>cancel notice</code> if this is wrong." % (source, cl._fmt(t), when, _e(notice["why"])))
    out(msg)
    if d == dstr(now.date()) and now.hour * 60 + now.minute < t:
        pub, rows = analyse_day(cfg, d, day_end=t)
        mine = [r for r in rows if r["side"] in cfg["sides"]]
        if mine:
            out(reminder_text(mine, d, "Updated for rising at %s" % cl._fmt(t)), silent=True)


def day_end_for(state, d):
    r = (state.data.get("rise") or {}).get(d)
    return None if not r else r["t"]


def ocr_image(path):
    exe = os.path.join(HOME, "bin", "ocr")
    if not os.path.exists(exe):
        return ""
    r = subprocess.run([exe, path], capture_output=True, text=True, timeout=120)
    return r.stdout


def text_of_file(tg, file_id, name):
    """Download a photo / PDF sent to the bot and return its text (reading the image if needed)."""
    f = tg.call("getFile", {"file_id": file_id})
    url = "https://api.telegram.org/file/bot%s/%s" % (tg.token, f["file_path"])
    with urllib.request.urlopen(url, timeout=120, context=cl._ssl_ctx()) as r:
        data = r.read()
    os.makedirs(os.path.join(CACHE, "sent"), exist_ok=True)
    path = os.path.join(CACHE, "sent", re.sub(r"[^\w.-]", "_", name or os.path.basename(f["file_path"])))
    open(path, "wb").write(data)
    if path.lower().endswith(".pdf"):
        import fitz
        doc = fitz.open(path)
        txt = "\n".join(p.get_text() for p in doc)
        if len(txt.strip()) < 40 and len(doc):  # scanned PDF: read the first page as an image
            img = path + ".png"
            doc[0].get_pixmap(dpi=200).save(img)
            txt = ocr_image(img)
        return path, txt
    return path, ocr_image(path)


LISTENER_FRESH = 8 * 60


def listener_alive(state):
    lis = state.data.get("listener") or {}
    return bool(lis) and time.time() - float(lis.get("hb", 0)) < LISTENER_FRESH


# ------------------------------------------------------------------ the scheduler
def watcher_update_notice(state, out):
    """The Board Watcher installs from this Mac (http://127.0.0.1:8766/watcher.user.js) and Tampermonkey
    keeps it up to date by itself, so there is nothing to announce."""
    return


def list_day_for(now):
    """The next day a list is expected: tomorrow, or Monday over a weekend."""
    tomorrow = now.date() + dt.timedelta(days=1)
    return tomorrow if tomorrow.weekday() < 5 else next_weekday(now.date())


def due_jobs(now):
    """Jobs due at this moment: (key, kind, list_date). Missed slots collapse to the latest one."""
    hm = now.hour * 60 + now.minute
    today = dstr(now.date())
    nxt_d = list_day_for(now)
    nxt = dstr(nxt_d)
    is_tomorrow = nxt_d == now.date() + dt.timedelta(days=1)
    jobs = []
    if is_tomorrow and 20 * 60 + 30 <= hm < 21 * 60 + 30:
        jobs.append(("eve1:%s" % nxt, "evening", nxt))
    elif is_tomorrow and 21 * 60 + 30 <= hm < 22 * 60:
        jobs.append(("eve2:%s" % nxt, "evening", nxt))
    elif hm >= 22 * 60:
        jobs.append(("rep:%s" % nxt, "report", nxt))
        if is_tomorrow:
            jobs.append(("ask:%s" % nxt, "ask", nxt))
    elif hm < 8 * 60 + 30:
        if now.weekday() < 5:
            jobs.append(("rep:%s" % today, "report", today))  # catch-up for last night
    elif hm < 13 * 60:
        if now.weekday() < 5:
            jobs.append(("rep:%s" % today, "report", today))  # if nobody sent it, send it first
            slot = "m0830" if hm < 9 * 60 + 30 else "m0930" if hm < 10 * 60 else "m1000"
            jobs.append(("%s:%s" % (slot, today), "morning", today))
            if hm >= 9 * 60 + 30:
                jobs.append(("watch:%s" % today, "watch", today))  # copy-paste code for the board watcher
    # weekend: Monday's list usually comes out on Saturday (sometimes Sunday) - send it as soon as it is out
    if (now.weekday() == 5 and hm >= 12 * 60) or now.weekday() == 6:
        if not any(j[1] == "report" and j[2] == nxt for j in jobs):
            jobs.append(("rep:%s" % nxt, "report", nxt))
    if hm >= 12 * 60:
        jobs.append(("mprobe:%s" % today, "mprobe", today))  # look for a new monthly list once a day
    return jobs


def tick(cfg, now=None, dry=False, tg=None, verbose=True, state=None):
    now = now or now_ist()
    if not cfg["names"]:
        print("No name set yet: run  ~/.casemonitor/casemonitor setup")
        return []
    if not dry:
        auto_open_board(cfg, now)
    log = print if verbose else (lambda *a: None)
    sent_msgs = []

    def out(text, silent=False, alarm=False, buttons=None):
        sent_msgs.append(text)
        if tg and not dry:
            tg.send(text, silent=silent, buttons=buttons)
        if not dry and not silent:
            notify(cfg, text, alarm)
        if dry or not tg:
            log("---- WOULD SEND ----\n" + re.sub(r"<[^>]+>", "", text) +
                ("\n[buttons: %s]" % ", ".join(b["text"] for row in buttons for b in row) if buttons else "") +
                "\n--------------------")

    if state is None:
        state = State(None if dry else tg).load()
    if dry:
        state.save = lambda: None  # simulation: never write shared or local state
    if not dry and sync_ntfy(cfg, state):
        state.save()
    if tg and not dry:
        if not listener_alive(state):  # the always-on listener (if running) answers messages itself
            process_commands(tg, state, cfg, out, now)
        watcher_update_notice(state, out)
    if state.data.get("paused"):
        log("Paused - nothing to do.")
        return sent_msgs
    dev = cfg["DEVICE_NAME"]

    if state.data.pop("run_now", False):
        d = dstr(list_day_for(now)) if now.hour >= 17 or now.weekday() >= 5 else dstr(now.date())
        pub, rows = analyse_day(cfg, d, refresh=True)
        out(full_report_text(rows, d, cfg, "Report on request") if pub else "Cause list for %s is not published yet." % pretty(d))
        state.save()
    if state.data.pop("run_monthly", False):
        md, mrows = latest_monthly(cfg, "A", dstr(now.date() + dt.timedelta(days=7)))
        out(monthly_report_text(mrows, "A", md) if md else "No monthly list found yet. I check for one every day.")
        state.save()

    for key, kind, d in due_jobs(now):
        if state.done(key):
            continue
        if kind == "mprobe":
            if not dry and not state.claim(key, dev):
                continue
            for side, md in probe_monthlies(cfg, now.date()):
                mkey = "mon:%s:%s" % (side, md)
                rows_m = monthly_rows(cfg, side, md)
                if rows_m is not None and not state.done(mkey):
                    out(monthly_report_text(rows_m, side, md))
                    if not dry:
                        state.mark(mkey, dev)
            prune_monthlies()
            prune_cache()
            if not dry:
                state.mark(key, dev)
            continue
        if kind == "ask":
            if not cfg["ask_sides"] or not cl.exists(d, "A"):
                continue
            if not dry and not state.claim(key, dev):
                continue
            state.data["next_list"] = d
            btns = [[dict(b, callback_data=b["callback_data"] + ":" + d) for b in row] for row in SIDE_BUTTONS]
            out("❓ Do you also want the <b>Original Side</b> and/or <b>Jalpaiguri</b> list for %s checked? "
                "Tap below (or send /os, /jal, /both). No answer means Appellate Side only." % pretty(d),
                silent=True, buttons=btns)
            if not dry:
                state.mark(key, dev)
            continue
        rise = day_end_for(state, d)
        if rise == 0 and kind in ("morning", "watch", "evening"):
            state.mark(key, dev) if not dry else None
            log("%s: no court work on %s (notice); skipped." % (key, pretty(d)))
            continue
        pub, rows = analyse_day(cfg, d, refresh=(kind == "morning" and key.startswith("m0830")), day_end=rise)
        extra = [s for s in state.data["xs"].get(d, "") if s in cl.SIDES]
        if kind == "morning" and extra:
            rows += analyse_day(cfg, d, sides=extra, day_end=rise)[1]
        if not pub:
            if kind == "report" and now.hour >= 22 and ddate(d) == now.date() + dt.timedelta(days=1):
                # a holiday? then the next working day's list may already be out
                later = next_weekday(ddate(d))
                for _ in range(3):
                    if cl.exists(dstr(later), "A"):
                        break
                    later = next_weekday(later)
                else:
                    later = None
                if later:
                    lk = "rep:%s" % dstr(later)
                    if not state.done(lk) and (dry or state.claim(lk, dev)):
                        p2, r2 = analyse_day(cfg, dstr(later))
                        out(full_report_text(r2, dstr(later), cfg, "No list for %s (holiday?). Next list" % pretty(d)))
                        if not dry:
                            state.mark(lk, dev)
                    continue
                np_key = "np:%s" % d
                if ddate(d).weekday() < 5 and not state.done(np_key) and (dry or state.claim(np_key, dev)):
                    out("ℹ️ Cause list for %s is not published yet (holiday, or it comes later). "
                        "I'll keep checking until midnight and again from early morning." % pretty(d), silent=True)
                    if not dry:
                        state.mark(np_key, dev)
            log("%s: list for %s not published yet." % (key, pretty(d)))
            continue
        if not dry and not state.claim(key, dev):
            log("%s: another Mac is handling it." % key)
            continue
        if kind == "report":
            title = ("Cause list report" if now.hour >= 20 else "Cause list report (catch-up)"
                     if ddate(d) == now.date() else "Cause list report (published early)")
            out(full_report_text(rows, d, cfg, title))
            path = save_html(rows, d, cfg)
            if tg and not dry and rows:
                try:
                    tg.send_file(path, "Full table with day plans, open in a browser")
                except Exception as ex:
                    log("file send failed:", ex)
        elif kind == "evening":
            msg = reminder_text(rows, d, "Heads-up for tomorrow", only_likely=True)
            if msg:
                out(msg)
            else:
                log("%s: nothing realistically likely tomorrow; staying quiet." % key)
        elif kind == "watch":
            if any(r["side"] == "A" for r in rows):
                out("<b>📋 Board watcher code for %s</b>\nOn the Mac mini the watcher loads this by itself once the "
                    "board is open and the CAPTCHA is typed. On another Mac: tap the code to copy it, paste it into the "
                    "code box on the display board page and click Save.\n\n<code>%s</code>"
                    % (pretty(d), _e(watch_code(rows, d))), silent=True)
        elif kind == "morning":
            slot = key.split(":")[0]
            label = {"m0830": "Morning reminder (8:30)", "m0930": "Reminder (9:30)", "m1000": "Court starts soon (10:00)"}[slot]
            msg = reminder_text(rows, d, label, board=(slot == "m1000"))
            if msg:
                out(msg, alarm=any(r["realistic"] for r in rows) and slot == "m1000")
        state.mark(key, dev)

    # extra sides you asked for (Original Side / Jalpaiguri): report as soon as each is published
    for d, picked in list(state.data["xs"].items()):
        if ymd(d) < now.strftime("%Y%m%d") or (ddate(d) == now.date() and now.hour >= 13):
            continue
        for s in picked:
            if s not in cl.SIDES:
                continue
            key = "xrep:%s:%s" % (s, d)
            if state.done(key):
                continue
            pub, rows = analyse_day(cfg, d, sides=[s])
            if not pub:
                log("%s: %s list for %s not published yet." % (key, cl.SIDES[s][2], pretty(d)))
                continue
            if not dry and not state.claim(key, dev):
                continue
            out(full_report_text(rows, d, cfg, "%s report" % cl.SIDES[s][2], sides=[s]))
            path = save_html(rows, d, cfg, sides=[s])
            if tg and not dry and rows:
                try:
                    tg.send_file(path, "%s table" % cl.SIDES[s][2])
                except Exception as ex:
                    log("file send failed:", ex)
            if not dry:
                state.mark(key, dev)
    return sent_msgs


# ------------------------------------------------------------------ always-on listener
BRIDGE_PORT = 8766
RISE = {}  # today's notices, kept fresh by the listener loop for the board watcher
CFG = {}
_WATCH = {}  # today's board watcher code, worked out at most every 30 minutes


def todays_watch_code():
    """The board watcher code for today (your Appellate items + each court's day plan)."""
    d = dstr(now_ist().date())
    hit = _WATCH.get(d)
    if hit and time.time() - hit[0] < 30 * 60:
        return hit[1]
    if now_ist().weekday() >= 5:
        return None
    rise = (RISE.get(d) or {}).get("t")
    if rise == 0:
        return None
    pub, rows = analyse_day(CFG, d, day_end=rise)
    code = json.loads(watch_code(rows, d)) if pub and any(r["side"] == "A" for r in rows) else None
    _WATCH.clear()
    _WATCH[d] = (time.time(), code)
    return code


def start_bridge():
    """Local-only web endpoint (127.0.0.1) where the Board Watcher posts the whole board."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(n).decode() or "{}")
                k = roster.save_board(data.get("rows") or [], data.get("fetched", ""))
                r = RISE.get(dstr(now_ist().date()))
                body, code = json.dumps({"ok": True, "rows": k, "rise": r["t"] if r else None,
                                         "why": r["why"] if r else ""}).encode(), 200
            except Exception as ex:
                body, code = json.dumps({"ok": False, "error": str(ex)}).encode(), 400
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/watcher.user.js"):
                try:
                    body = open(os.path.join(HERE, "watcher.user.js"), "rb").read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/javascript; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    self.send_response(404)
                    self.end_headers()
                return
            if self.path.startswith("/settings"):
                c = load_config()
                body = json.dumps({"ntfy": c.get("NTFY_TOPIC", ""), "tg": c.get("TELEGRAM_BOT_TOKEN", ""),
                                   "chat": c.get("TELEGRAM_CHAT_ID", ""), "po_user": c.get("PUSHOVER_USER", ""),
                                   "po_token": c.get("PUSHOVER_TOKEN", "")}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/watch"):
                try:
                    code = todays_watch_code()
                    body = json.dumps({"ok": bool(code), "code": code,
                                       "reason": "" if code else "no matters or no list today"}).encode()
                except Exception as ex:
                    body = json.dumps({"ok": False, "reason": str(ex)[:200]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Case Monitor bridge OK")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def listen(cfg, tg):
    """Answer Telegram messages within seconds, and receive the board from the watcher.
    Runs on one Mac (LISTENER=1 in config.env); launchd restarts it if it stops, and it
    restarts itself when the program files are updated."""
    if not tg:
        print("Telegram is not connected on this Mac (run setup-telegram first).")
        sys.exit(1)
    CFG.update(cfg)
    try:
        start_bridge()
    except OSError as ex:
        print("bridge not started (%s); board questions will use whatever another listener saved" % ex)
    files = [os.path.join(HERE, f) for f in ("casemonitor.py", "causelist.py", "roster.py")]
    stamp = [os.path.getmtime(f) for f in files if os.path.exists(f)]
    dev, last_hb = cfg["DEVICE_NAME"], 0

    def out(text, silent=False, alarm=False, buttons=None):
        tg.send(text, silent=silent, buttons=buttons)
        if not silent:
            notify(cfg, text, alarm)

    print(now_ist().isoformat(), "listener started on", dev)
    last_idx = 0
    while True:
        try:
            if time.time() - last_idx > 20 * 60:
                last_idx = time.time()
                keep_roster(cfg, tg, out)
            if [os.path.getmtime(f) for f in files if os.path.exists(f)] != stamp:
                print("program updated; restarting")
                sys.exit(0)
            state = State(tg).load()
            RISE.clear()
            RISE.update(state.data.get("rise") or {})
            if time.time() - last_hb > 240:
                state.data["listener"] = {"dev": dev, "hb": int(time.time())}
                state.save()
                last_hb = time.time()
            ups = tg.call("getUpdates", {"offset": int(state.data.get("upd", 0)) + 1, "timeout": 50,
                                         "allowed_updates": ["message", "callback_query"]})
            for u in ups:
                state = State(tg).load()  # fresh copy: a 5-minute check may have written meanwhile
                state.data["upd"] = max(int(state.data.get("upd", 0)), u["update_id"])
                handle_update(u, tg, state, cfg, out, now_ist())
                RISE.clear()
                RISE.update(state.data.get("rise") or {})
                state.data["listener"] = {"dev": dev, "hb": int(time.time())}
                state.save()
                last_hb = time.time()
        except SystemExit:
            raise
        except Exception as ex:
            print(now_ist().isoformat(), "listener error:", repr(ex)[:300])
            time.sleep(10)


def keep_roster(cfg, tg, out):
    """On the listener Mac: store today's and the next list day's lists (all sides) in the
    roster database, and tell you once when a new Appellate list shows roster changes."""
    now = now_ist()
    days = [now.date()] if now.weekday() < 5 else []
    days.append(list_day_for(now))
    con = roster.db()
    for d in days:
        ds = dstr(d)
        for side in ("A", "O", "J"):
            had = con.execute("SELECT 1 FROM lists WHERE date=? AND side=? AND kind='daily'", (ds, side)).fetchone()
            try:
                n = roster.index_list(ds, side, con=con)
            except Exception as ex:
                print("index %s %s failed: %s" % (ds, side, ex))
                continue
            if n and not had and side == "A":
                ch = roster.roster_changes(con, side, ds)
                if ch:
                    state = State(tg).load()
                    key = "rchg:%s" % ds
                    if not state.done(key):
                        state.mark(key, cfg["DEVICE_NAME"])
                        out(roster.ans_changes(con, side, ds))


def backfill(cfg, start, end, sides):
    """Load past lists into the roster database (for history questions). PDFs are not kept."""
    d, n = start, 0
    while d <= end:
        if d.weekday() < 5:
            ds = dstr(d)
            for side in sides:
                con = roster.db()
                if con.execute("SELECT 1 FROM lists WHERE date=? AND side=? AND kind='daily'", (ds, side)).fetchone():
                    continue
                try:
                    k = roster.index_list(ds, side, con=con)
                except Exception as ex:
                    print(pretty(ds), side, "failed:", ex)
                    continue
                print(pretty(ds), cl.SIDES[side][2], "%d courts" % k if k else "no list (holiday)", flush=True)
                n += 1 if k else 0
                for kind in ("daily",) + cl.SUP_KINDS:
                    sub = CACHE if kind == "daily" else os.path.join(CACHE, kind)
                    p = os.path.join(sub, os.path.basename(cl.pdf_url(ds, side, kind)))
                    if os.path.exists(p) and ymd(ds) < now_ist().strftime("%Y%m%d"):
                        os.remove(p)
        d += dt.timedelta(days=1)
    print("Done: %d list(s) added. Database: %.1f MB" % (n, os.path.getsize(roster.DB) / 1e6))


# ------------------------------------------------------------------ CLI
def telegram_from(cfg):
    if cfg.get("TELEGRAM_BOT_TOKEN") and cfg.get("TELEGRAM_CHAT_ID"):
        return Telegram(cfg["TELEGRAM_BOT_TOKEN"], cfg["TELEGRAM_CHAT_ID"])
    return None


def auto_open_board(cfg, now):
    """On the main Mac, open the display board by itself at about 10:15 on a court day with your matters."""
    if cfg.get("AUTO_OPEN_BOARD") != "1" or cfg.get("LISTENER") != "1" or now.weekday() >= 5:
        return
    hm = now.hour * 60 + now.minute
    if not (10 * 60 + 10 <= hm < 11 * 60 + 30):
        return
    stamp = os.path.join(HOME, ".board_opened_%s" % dstr(now.date()))
    if os.path.exists(stamp):
        return
    open(stamp, "w").close()
    for old in glob.glob(os.path.join(HOME, ".board_opened_*")):
        if old != stamp:
            os.remove(old)
    try:
        pub, rows = analyse_day(cfg, dstr(now.date()))
        if pub and any(r["side"] == "A" for r in rows):
            open_board()
    except Exception as ex:
        print("auto-open failed:", ex)


def services(cfg):
    """(Re)create the background jobs: the 5-minute check everywhere, the listener on the main Mac."""
    uid = os.getuid()
    la = os.path.expanduser("~/Library/LaunchAgents")
    os.makedirs(la, exist_ok=True)
    os.makedirs(os.path.join(HOME, "logs"), exist_ok=True)
    py, app = os.path.join(HOME, "venv", "bin", "python"), os.path.join(HOME, "app", "casemonitor.py")

    def plist(label, args, extra):
        return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
                "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n<plist version=\"1.0\"><dict>\n"
                "  <key>Label</key><string>%s</string>\n  <key>ProgramArguments</key><array>%s</array>\n%s"
                "  <key>StandardOutPath</key><string>%s</string>\n  <key>StandardErrorPath</key><string>%s</string>\n"
                "</dict></plist>\n") % (label, "".join("<string>%s</string>" % html.escape(x) for x in args), extra,
                                         os.path.join(HOME, "logs", label.split(".")[-1] + ".log"),
                                         os.path.join(HOME, "logs", label.split(".")[-1] + ".log"))
    jobs = {"com.casemonitor.app.tick": ([os.path.join(HOME, "run.sh"), "tick"],
                                         "  <key>StartInterval</key><integer>300</integer>\n  <key>RunAtLoad</key><true/>\n", True),
            "com.casemonitor.app.listener": ([py, "-u", app, "listen"],
                                             "  <key>KeepAlive</key><true/>\n  <key>RunAtLoad</key><true/>\n"
                                             "  <key>ThrottleInterval</key><integer>20</integer>\n", cfg.get("LISTENER") == "1")}
    for label, (args, extra, wanted) in jobs.items():
        path = os.path.join(la, label + ".plist")
        subprocess.run(["launchctl", "bootout", "gui/%d" % uid, path], capture_output=True)
        if wanted:
            open(path, "w").write(plist(label, args, extra))
            subprocess.run(["launchctl", "bootstrap", "gui/%d" % uid, path], capture_output=True)
        elif os.path.exists(path):
            os.remove(path)
    return cfg.get("LISTENER") == "1"


def _ask(q, default=""):
    a = input("%s%s: " % (q, (" [%s]" % default) if default else "")).strip()
    return a or default


def _yes(q, default=True):
    a = input("%s [%s]: " % (q, "Y/n" if default else "y/N")).strip().lower()
    return default if not a else a.startswith("y")


def setup(cfg):
    """One set of questions for everything; safe to run again to change answers."""
    print("\n== Calcutta High Court Case Monitoring System: setup ==\n")
    names = _ask("Your name as printed in the cause list (other spellings separated by commas)",
                 cfg.get("ADVOCATE_NAMES", ""))
    while not names.strip():
        names = _ask("Please enter your name as it appears in the cause list")
    set_config("ADVOCATE_NAMES", ",".join(n.strip().upper() for n in names.split(",") if n.strip()))
    token = cfg.get("TELEGRAM_BOT_TOKEN", "")
    if token and _yes("Keep the Telegram bot already set up on this Mac?"):
        pass
    else:
        token = ""
    while not token:
        token = _ask("Paste your Telegram bot token from @BotFather").strip()
        try:
            me = Telegram(token, 0).call("getMe")
        except Exception:
            print("That token did not work. Copy it again from @BotFather.")
            token = ""
            continue
        print("Token OK: your bot is @%s. On your phone, open it in Telegram, tap Start and send: hello" % me.get("username"))
        print("Waiting for your message (up to 3 minutes)...")
        chat = None
        for _ in range(60):
            ups = Telegram(token, 0).call("getUpdates", {})
            ids = [str(u["message"]["chat"]["id"]) for u in ups if "message" in u and u["message"]["chat"].get("type") == "private"]
            if ids:
                chat = ids[-1]
                break
            time.sleep(3)
        if not chat:
            print("No message arrived. Send 'hello' to your bot and try again.")
            token = ""
            continue
        set_config("TELEGRAM_BOT_TOKEN", token)
        set_config("TELEGRAM_CHAT_ID", chat)
        print("Connected to your chat.")
    phone = _ask("Your phone: 1 = Android, 2 = iPhone", "2" if cfg.get("PHONE") == "iphone" else "1")
    set_config("PHONE", "iphone" if phone.strip() == "2" else "android")
    if phone.strip() == "2" or cfg.get("PUSHOVER_USER"):
        print("Loud alarms on iPhone use Pushover (pushover.net): your User Key, and an API Token from 'Create an Application'.")
        pu = _ask("Pushover User Key (Enter to skip)", cfg.get("PUSHOVER_USER", ""))
        pt = _ask("Pushover API Token (Enter to skip)", cfg.get("PUSHOVER_TOKEN", "")) if pu else ""
        set_config("PUSHOVER_USER", pu)
        set_config("PUSHOVER_TOKEN", pt)
    sides = _ask("Check every night: A = Appellate, O = Original, J = Jalpaiguri (e.g. A or A,O)", cfg.get("AUTO_SIDES", "A"))
    auto = _sides(sides) or ["A"]
    set_config("AUTO_SIDES", ",".join(auto))
    set_config("ASK_SIDES", ",".join(s for s in "OJ" if s not in auto))
    main_mac = _yes("Is this your main Mac (switched on during court hours)?", cfg.get("LISTENER", "1") == "1")
    set_config("LISTENER", "1" if main_mac else "0")
    set_config("AUTO_OPEN_BOARD", "1" if (main_mac and _yes("Open the display board by itself at about 10:15 on court days?",
                                                          cfg.get("AUTO_OPEN_BOARD", "1") == "1")) else "0")
    cfg = load_config()
    tg = telegram_from(cfg)
    st = State(tg).load()
    sync_ntfy(cfg, st)
    st.save()
    services(load_config())
    cfg = load_config()
    lines = ["✅ Case Monitor is set up on <b>%s</b> for <b>%s</b>." % (_e(cfg["DEVICE_NAME"]), _e(", ".join(cfg["names"]))),
             "Send /help to see what you can ask."]
    if cfg.get("PHONE") == "android":
        lines.append("Alarm app: install ntfy and subscribe to <code>%s</code>" % _e(cfg.get("NTFY_TOPIC", "")))
    tg.send("\n".join(lines))
    notify(cfg, "Case Monitor test\nIf this reaches your alarm app, alarms work.")
    print("\nDone. A test message is on your phone.")
    if cfg.get("PHONE") == "android":
        print("ntfy topic for the phone app: %s" % cfg.get("NTFY_TOPIC", ""))
    if main_mac:
        print("Board Watcher: install Tampermonkey in Chrome, then open  http://127.0.0.1:8766/watcher.user.js  and click Install.")


def open_board():
    for app in ("Google Chrome", "Google Chrome Beta", "Chromium", "Microsoft Edge"):
        if subprocess.run(["open", "-Ra", app], capture_output=True).returncode == 0:
            subprocess.run(["open", "-a", app, BOARD_URL])
            return app
    subprocess.run(["open", BOARD_URL])
    return "your default browser"


def main():
    ap = argparse.ArgumentParser(description="Calcutta High Court Case Monitoring System")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("tick")
    r = sub.add_parser("report")
    r.add_argument("--date")
    r.add_argument("--sides", help="e.g. A,O,J (default: the nightly sides)")
    r.add_argument("--dry-run", action="store_true")
    m = sub.add_parser("monthly")
    m.add_argument("--date", help="DDMMYYYY of the monthly list (default: latest known)")
    m.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("simulate")
    s.add_argument("--at", required=True, action="append", help='"YYYY-MM-DD HH:MM" India time (repeatable)')
    b = sub.add_parser("backfill")
    b.add_argument("--from", dest="start", required=True, help="DDMMYYYY")
    b.add_argument("--to", dest="end", help="DDMMYYYY (default: today)")
    b.add_argument("--sides", default="A", help="e.g. A or A,O,J")
    f = sub.add_parser("forget-database")
    f.add_argument("--before", help="DDMMYYYY: delete only lists before this date")
    q = sub.add_parser("ask")
    q.add_argument("question", nargs="+")
    for c in ("stop", "resume", "status", "setup", "setup-telegram", "services", "board", "watcher-settings", "ntfy-topic", "listen"):
        sub.add_parser(c)
    a = ap.parse_args()
    cfg = load_config()
    tg = telegram_from(cfg)

    if a.cmd == "tick":
        time.sleep(random.uniform(0, 20))  # spread out Macs that wake at the same moment
        try:
            tick(cfg, tg=tg)
        except Exception as ex:
            print(dt.datetime.now(IST).isoformat(), "tick error:", repr(ex))
            sys.exit(1)
    elif a.cmd == "report":
        d = a.date or dstr(now_ist().date())
        sides = _sides(a.sides) if a.sides else None
        pub, rows = analyse_day(cfg, d, refresh=True, sides=sides)
        text = full_report_text(rows, d, cfg, sides=sides) if pub else "Cause list for %s is not published yet." % pretty(d)
        path = save_html(rows, d, cfg, sides=sides) if pub else None
        if tg and not a.dry_run:
            tg.send(text)
            if path and rows:
                tg.send_file(path, "Full table with day plans, open in a browser")
        else:
            print(re.sub(r"<[^>]+>", "", text))
            if pub and any(r["side"] == "A" for r in rows):
                print("\nBoard watcher code:\n" + watch_code(rows, d))
        if path:
            print("\nHTML report:", path)
    elif a.cmd == "monthly":
        if a.date:
            md, rows = a.date, monthly_rows(cfg, "A", a.date)
        else:
            found = probe_monthlies(cfg, now_ist().date())
            for side, fd in found:
                monthly_rows(cfg, side, fd)
            md, rows = latest_monthly(cfg, "A", dstr(now_ist().date() + dt.timedelta(days=7)))
        if rows is None:
            print("No monthly list found for %s." % (pretty(a.date) if a.date else "this month"))
            sys.exit(1)
        text = monthly_report_text(rows, "A", md)
        if tg and not a.dry_run:
            tg.send(text)
        print(re.sub(r"<[^>]+>", "", text))
    elif a.cmd == "simulate":
        mem = State(None)  # one in-memory state across the simulated times
        mem.save = lambda: None
        for at in a.at:
            print("\n######## %s (simulated, nothing is sent)" % at)
            tick(cfg, now=now_ist(at), dry=True, state=mem)
    elif a.cmd in ("stop", "resume", "status"):
        st = State(tg).load()
        if a.cmd != "status":
            st.data["paused"] = (a.cmd == "stop")
            st.save()
        print("Status:", "PAUSED" if st.data.get("paused") else "ACTIVE")
        print("ntfy topic:", st.data.get("ntfy") or cfg.get("NTFY_TOPIC", "-"))
        print("Recent jobs:", json.dumps(st.data.get("sent", {}), indent=1))
    elif a.cmd == "backfill":
        backfill(cfg, ddate(a.start), ddate(a.end) if a.end else now_ist().date(), _sides(a.sides) or ["A"])
    elif a.cmd == "forget-database":
        what = "lists before %s" % pretty(a.before) if a.before else "the WHOLE roster database (all stored lists and board history)"
        if input("This deletes %s. Type DELETE to confirm: " % what).strip() != "DELETE":
            print("Nothing deleted.")
        else:
            print("Deleted %s." % roster.forget(a.before))
    elif a.cmd == "listen":
        listen(cfg, tg)
    elif a.cmd == "ask":
        print(re.sub(r"<[^>]+>", "", html.unescape(roster.answer(" ".join(a.question), cfg["names"]))))
    elif a.cmd == "board":
        print("Opening the display board in %s. Type the CAPTCHA there; the Board Watcher box appears "
              "bottom-right." % open_board())
    elif a.cmd == "watcher-settings":
        code = json.dumps({"ntfy": cfg.get("NTFY_TOPIC", ""), "tg": cfg.get("TELEGRAM_BOT_TOKEN", ""),
                           "chat": cfg.get("TELEGRAM_CHAT_ID", "")}, separators=(",", ":"))
        subprocess.run(["pbcopy"], input=code.encode())
        print("Settings code copied. On the display board page, click the code box in the Board Watcher, "
              "press Cmd+A then Cmd+V, and click Save. It fills the ntfy topic, bot token and chat id.")
    elif a.cmd == "ntfy-topic":
        print(cfg.get("NTFY_TOPIC", "(not set yet: it is created on the next background check)"))
    elif a.cmd == "setup":
        setup(cfg)
    elif a.cmd == "services":
        print("Listener on this Mac:", "on" if services(cfg) else "off")
    elif a.cmd == "setup-telegram":
        token = cfg.get("TELEGRAM_BOT_TOKEN") or input("Paste the bot token from @BotFather, then press Enter: ").strip()
        try:
            me = Telegram(token, 0).call("getMe")
        except Exception as ex:
            print("That token did not work (%s). Copy it again from @BotFather and re-run." % ex)
            sys.exit(1)
        print("Token OK: your bot is @%s" % me.get("username"))
        print("Now, on your phone, open @%s in Telegram, tap Start, and send: hello" % me.get("username"))
        print("Waiting for your message (up to 3 minutes)...")
        chats = {}
        for _ in range(60):
            ups = Telegram(token, 0).call("getUpdates", {})
            chats = {str(u["message"]["chat"]["id"]): u["message"]["chat"].get("first_name", "")
                     for u in ups if "message" in u}
            if chats:
                break
            time.sleep(3)
        if not chats:
            print("No message arrived. Send 'hello' to @%s and run this again." % me.get("username"))
            sys.exit(1)
        chat_id = list(chats)[-1]
        set_config("TELEGRAM_BOT_TOKEN", token)
        set_config("TELEGRAM_CHAT_ID", chat_id)
        tg = Telegram(token, chat_id)
        st = State(tg).load()
        sync_ntfy(load_config(), st)
        st.save()
        tg.send("✅ Case Monitor bot connected on <b>%s</b>.\nntfy topic for the phone app: <code>%s</code>"
                % (html.escape(cfg["DEVICE_NAME"]), _e(st.data.get("ntfy", ""))))
        print("Done. Chat id %s (%s) saved to %s." % (chat_id, chats[chat_id], CONFIG))
        print("ntfy topic (subscribe to this in the ntfy app): %s" % st.data.get("ntfy", ""))
    else:
        ap.print_help()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. (Nothing is lost: whatever finished before stopping is kept.)")
        sys.exit(130)
