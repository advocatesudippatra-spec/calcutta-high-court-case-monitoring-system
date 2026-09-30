"""
AI assistant for the cause list bot: understands any question or instruction and answers from your own
database (cause lists, determinations, roster history, notices, live board) using a small set of tools.

Providers (choose with AI_PROVIDER in config.env; model with AI_MODEL_<PROVIDER>):
  moonshot (default, Kimi), deepseek, openai, gemini, claude
API keys are kept in the macOS Keychain (set with:  casemonitor ai-key moonshot), never in files.
"""
import datetime as dt
import json
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request

import causelist as cl
import roster

KEYCHAIN_SERVICE = "Calcutta High Court Case Monitor"
PROVIDERS = {
    # name: (kind, default base URL, default model)
    "moonshot": ("openai", "https://api.moonshot.ai/v1", "kimi-latest"),
    "deepseek": ("openai", "https://api.deepseek.com", "deepseek-chat"),
    "openai": ("openai", "https://api.openai.com/v1", "gpt-4.1-mini"),
    "gemini": ("openai", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
    "claude": ("anthropic", "https://api.anthropic.com/v1", "claude-sonnet-5"),
}
MAX_TOOL_ROUNDS = 6
HISTORY = {}  # chat id -> recent [(role, text)] so follow-up questions make sense


# ------------------------------------------------------------------ keys (macOS Keychain)
def get_key(provider):
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", provider, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def set_key(provider, key):
    subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", provider, "-w", key],
                   capture_output=True, text=True, check=True)


def delete_key(provider):
    subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", provider], capture_output=True)


def provider_of(cfg):
    p = (cfg.get("AI_PROVIDER") or "moonshot").lower()
    return p if p in PROVIDERS else "moonshot"


def available(cfg):
    return bool(get_key(provider_of(cfg)))


# ------------------------------------------------------------------ tools the AI may use
SCHEMA = """SQLite database of the Calcutta High Court cause lists (dates are text DDMMYYYY; to sort or compare use
substr(date,5,4)||substr(date,3,2)||substr(date,1,2)):
  lists(date, side, kind, indexed, courts, items)                       -- one row per list read (side A=Appellate, O=Original, J=Jalpaiguri)
  courts(date, side, kind, court, bench, time, judges, determination, notes, plan, vc, items)
                                                                        -- every court sitting that day: judges, time, determination (what it takes), Bench note
  items(date, side, kind, court, serial, tagged, case_no, parties, advocates, section, fixed)
                                                                        -- every item: item number, case number, parties, advocates, heading (section)
  board(t, side, court, serial, detail, daily, judges, fetched)         -- display board history (t = unix time; only changes)
  board_now(side, court, serial, detail, daily, judges, fetched, t)     -- latest board reading per court
  notices(id, title, uploaded, kind, dates, courts, judges, url, text, fetched, category)
                                                                        -- High Court notices: sitting, modified determination, holiday, circulars...
kind in lists/courts/items: 'daily', 'sup1'..'sup5' (supplementary), 'monthly'."""

TOOLS = [
    {"name": "query_database",
     "description": "Run a read-only SQL SELECT on the cause-list database and get rows back (max 60). " + SCHEMA,
     "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
    {"name": "roster_lookup",
     "description": "Ready-made answers in plain words: e.g. 'group 6', 'court 35', 'mentioning 25', 'fixed 35', "
                    "'justice <name>', 'find WPA/1234/2026', 'advocate <name>', 'party <name>', 'running 12', 'board', "
                    "'courts', 'group 6 history', 'notices court 22', 'modified determination', 'holidays', "
                    "'changes'. Add 'tomorrow' or a date like 29/09 if needed.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "my_matters",
     "description": "The user's own matters for a date (DDMMYYYY; default the next list day): court, item, heading, "
                    "chance of being reached and why.",
     "parameters": {"type": "object", "properties": {"date": {"type": "string"}}}},
    {"name": "follow_court",
     "description": "Start sending a message each time an item finishes in a court (until the user's item comes on).",
     "parameters": {"type": "object", "properties": {"court": {"type": "string"}}, "required": ["court"]}},
    {"name": "stop_following",
     "description": "Stop following a court (or all courts if court is empty).",
     "parameters": {"type": "object", "properties": {"court": {"type": "string"}}}},
    {"name": "set_reminder",
     "description": "Send the user a reminder message at a time today or later (time as HH:MM 24-hour, optional date DDMMYYYY).",
     "parameters": {"type": "object", "properties": {"time": {"type": "string"}, "date": {"type": "string"},
                                                     "text": {"type": "string"}}, "required": ["time", "text"]}},
]


def _query_database(sql):
    q = sql.strip().rstrip(";")
    if not re.match(r"(?is)^\s*(SELECT|WITH)\b", q) or re.search(r"(?i)\b(INSERT|UPDATE|DELETE|DROP|ALTER|ATTACH|PRAGMA|CREATE|REPLACE)\b", q):
        return "Only a single read-only SELECT is allowed."
    con = sqlite3.connect("file:%s?mode=ro" % roster.DB, uri=True, timeout=20)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(q).fetchmany(60)
    except Exception as ex:
        return "SQL error: %s" % ex
    finally:
        pass
    out = [dict(r) for r in rows]
    for r in out:
        for k, v in list(r.items()):
            if isinstance(v, str) and len(v) > 700:
                r[k] = v[:700] + "…"
    con.close()
    return json.dumps(out, ensure_ascii=False)[:14000]


def run_tool(name, args, ctx):
    try:
        if name == "query_database":
            return _query_database(args.get("sql", ""))
        if name == "roster_lookup":
            return re.sub(r"<[^>]+>", "", roster.answer(args.get("query", ""), ctx["names"]))[:6000]
        if name == "my_matters":
            return ctx["my_matters"](args.get("date") or "")
        if name == "follow_court":
            return ctx["follow"](str(args.get("court", "")).strip())
        if name == "stop_following":
            return ctx["unfollow"](str(args.get("court", "") or "").strip())
        if name == "set_reminder":
            return ctx["remind"](args.get("time", ""), args.get("date", ""), args.get("text", ""))
    except Exception as ex:
        return "Tool error: %r" % ex
    return "Unknown tool"


# ------------------------------------------------------------------ provider calls
def _post(url, body, headers, timeout=90):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=dict({"Content-Type": "application/json"}, **headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=cl._ssl_ctx()) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError("AI provider error %s: %s" % (e.code, e.read().decode()[:300]))


def _openai_chat(base, key, model, messages, tools):
    body = {"model": model, "messages": messages, "temperature": 0.2,
            "tools": [{"type": "function", "function": t} for t in tools]}
    res = _post(base.rstrip("/") + "/chat/completions", body, {"Authorization": "Bearer " + key})
    msg = res["choices"][0]["message"]
    calls = [(c["id"], c["function"]["name"], json.loads(c["function"].get("arguments") or "{}")) for c in (msg.get("tool_calls") or [])]
    return msg, calls


def _anthropic_chat(base, key, model, system, messages, tools):
    body = {"model": model, "max_tokens": 1500, "system": system, "messages": messages,
            "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]}
    res = _post(base.rstrip("/") + "/messages", body, {"x-api-key": key, "anthropic-version": "2023-06-01"})
    calls = [(b["id"], b["name"], b.get("input") or {}) for b in res.get("content", []) if b.get("type") == "tool_use"]
    text = "".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text")
    return res, calls, text


def system_prompt(cfg, now):
    return ("You are the assistant of a Calcutta High Court advocate (names in the cause list: %s). Today is %s (India). "
            "Answer questions about the court's cause lists, which court or judge takes what, the user's matters and their "
            "chances, the live display board, roster history and High Court notices, using the tools; never guess facts "
            "that the tools can give. Give short, clear answers in plain English (no markdown tables; simple lines are "
            "fine), with court numbers, item numbers and dates. If the data does not show something, say so. You can also "
            "act: follow a court, stop following, set a reminder. The CAPTCHA of the display board is always typed by the "
            "user; never offer to solve it." % (", ".join(cfg["names"]) or "the user", now.strftime("%A %d %B %Y, %I:%M %p")))


def ask(text, cfg, ctx, chat="me", now=None, call=None):
    """Answer one message. ctx: names, my_matters(date), follow(court), unfollow(court), remind(time,date,text).
    call: optional stand-in for the provider (tests)."""
    now = now or dt.datetime.now(roster.IST)
    prov = provider_of(cfg)
    kind, base, model = PROVIDERS[prov]
    base = cfg.get("AI_BASE_%s" % prov.upper(), base)
    model = cfg.get("AI_MODEL_%s" % prov.upper(), model)
    key = get_key(prov) if call is None else "test"
    if not key:
        raise RuntimeError("No API key for %s (set it with: casemonitor ai-key %s)" % (prov, prov))
    hist = HISTORY.setdefault(chat, [])
    sysmsg = system_prompt(cfg, now)
    answer = ""
    if kind == "openai" or call is not None:
        messages = [{"role": "system", "content": sysmsg}] + [{"role": r, "content": t} for r, t in hist[-8:]] + \
                   [{"role": "user", "content": text}]
        for _ in range(MAX_TOOL_ROUNDS):
            msg, calls = (call or _openai_chat)(base, key, model, messages, TOOLS)
            if not calls:
                answer = msg.get("content") or ""
                break
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": msg.get("tool_calls")})
            for cid, name, args in calls:
                messages.append({"role": "tool", "tool_call_id": cid, "content": run_tool(name, args, ctx)})
        else:
            answer = "I could not finish looking that up; please ask more specifically."
    else:
        messages = [{"role": r, "content": t} for r, t in hist[-8:]] + [{"role": "user", "content": text}]
        for _ in range(MAX_TOOL_ROUNDS):
            res, calls, said = _anthropic_chat(base, key, model, sysmsg, messages, TOOLS)
            if not calls:
                answer = said
                break
            messages.append({"role": "assistant", "content": res["content"]})
            messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid,
                                                           "content": run_tool(name, args, ctx)} for cid, name, args in calls]})
        else:
            answer = "I could not finish looking that up; please ask more specifically."
    answer = (answer or "").strip() or "I have no answer for that."
    hist.extend([("user", text), ("assistant", answer)])
    del hist[:-12]
    try:
        con = roster.db()
        con.execute("INSERT INTO ai_log VALUES(?,?,?,?,?)", (time.time(), chat, prov, text, answer))
        con.commit()
    except Exception:
        pass
    return answer
