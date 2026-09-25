// ==UserScript==
// @name         Calcutta HC Board Watcher (Case Monitor)
// @namespace    casemonitor
// @version      3.0
// @description  Watches the official Calcutta HC display board (after YOU enter the CAPTCHA), knows each court's day plan, and pushes phone alerts when your item is near, on, or when its heading closes before your item.
// @match        https://display.calcuttahighcourt.gov.in/principal.php*
// @match        https://display.calcuttahighcourt.gov.in/jalpaiguri.php*
// @match        https://display.calcuttahighcourt.gov.in/portblair.php*
// @grant        GM_xmlhttpRequest
// @connect      ntfy.sh
// @connect      api.telegram.org
// @connect      api.pushover.net
// @connect      127.0.0.1
// @connect      localhost
// @updateURL    http://127.0.0.1:8766/watcher.user.js
// @downloadURL  http://127.0.0.1:8766/watcher.user.js
// ==/UserScript==

/*
 * How it works
 * - You open the official board and type the CAPTCHA yourself, as normal.
 * - The official page then refreshes itself every 15 seconds. This script only
 *   reads the table the page has already shown; it makes no requests to the
 *   High Court server and does not touch the CAPTCHA.
 * - Each morning it loads the "board watcher code" (your items plus each court's day plan:
 *   headings, item ranges, the times the Bench note gives them) by itself from the Case Monitor
 *   program on this Mac. On a Mac without the program, paste the code from Telegram instead.
 * - Alerts: 20 / 10 / 5 items away, "your item is ON", and, if the board leaves your
 *   heading before reaching your item, "heading closed - probably not reached".
 *   A planned move (to another heading at the time the note gives, or to fixed
 *   matters at their time) is not treated as a skip.
 * - It keeps a small record of how each court moved (only the changes, 30 days)
 *   and sends a summary at 5 PM.
 * - When courts rise early (a bar resolution / notice sent to the bot), or the board stops
 *   changing late in the day, it stops watching for the day instead of reporting an error.
 * - It also hands the whole board (every court) to the Case Monitor program on this Mac
 *   (http://127.0.0.1:8766, this computer only), so you can ask the Telegram bot
 *   "what is running in court 12" about any court.
 */
(function () {
  'use strict';

  const ALERT_LEVELS = [20, 10, 5, 0];   // notify when this many items (or fewer) away; 0 = your item is on
  const CHECK_EVERY_MS = 15000;
  const SUMMARY_AT = 17 * 60;            // 5 PM daily summary
  const START_CHECK_AT = 10 * 60 + 50;   // one "court not on board yet" check per court
  const STALE_MIN = 3;                   // board data older than this (minutes) = not updating
  const KEEP_DAYS = 30;
  // principal.php shows Appellate + Original Side; the circuit bench pages have their own boards
  const PAGE_SIDE = /jalpaiguri/i.test(location.pathname) ? 'J' : /portblair/i.test(location.pathname) ? 'P' : '';
  const LS = window.localStorage;
  const TOKEN_RE = /^\d{6,}:[A-Za-z0-9_-]{30,}$/;
  const TOPIC_RE = /^[A-Za-z0-9_-]{6,64}$/;

  const today = () => { const d = new Date(); return `${String(d.getDate()).padStart(2, '0')}${String(d.getMonth() + 1).padStart(2, '0')}${d.getFullYear()}`; };
  const nowMin = () => { const d = new Date(); return d.getHours() * 60 + d.getMinutes(); };
  const fmt = m => { const h = Math.floor(m / 60), mi = m % 60; return `${((h + 11) % 12) + 1}:${String(mi).padStart(2, '0')} ${h < 12 ? 'AM' : 'PM'}`; };
  const jget = (k, d) => { try { return JSON.parse(LS.getItem(k)) ?? d; } catch (e) { return d; } };
  const jset = (k, v) => LS.setItem(k, JSON.stringify(v));

  // ------------------------------------------------------------ settings + watch code
  function code() {
    // v2: {"v":2,"d":"DDMMYYYY","w":[{c,i,k,s,m}],"p":{court:[[first,last,name,start,end]]},"f":{court:[[serial,min]]}}
    // v1 (old): [{"side":"A","court":"35","item":745,"case":"..."}]
    const raw = jget('hcw_watch', []);
    if (Array.isArray(raw)) {
      return { d: '', plans: {}, fixed: {}, watch: raw.map(w => ({ side: (w.side || 'A').toUpperCase(), court: String(w.court), item: +w.item, kase: w.case || '', sec: '', monthly: false })) };
    }
    return {
      d: raw.d || '', plans: raw.p || {}, fixed: raw.f || {},
      watch: (raw.w || []).map(w => ({ side: 'A', court: String(w.c), item: +w.i, kase: w.k || '', sec: w.s || '', monthly: !!w.m })),
    };
  }
  const cfg = () => ({ ntfy: LS.getItem('hcw_ntfy') || '', tgToken: LS.getItem('hcw_tg_token') || '', tgChat: LS.getItem('hcw_tg_chat') || '',
    poUser: LS.getItem('hcw_po_user') || '', poToken: LS.getItem('hcw_po_token') || '' });

  // alert settings come from the program on this Mac: nothing to paste
  let lastSettings = 0;
  function loadSettings() {
    if (Date.now() - lastSettings < 10 * 60e3) return;
    lastSettings = Date.now();
    GM_xmlhttpRequest({ method: 'GET', url: 'http://127.0.0.1:8766/settings', timeout: 8000,
      onload: r => { try { const j = JSON.parse(r.responseText);
        if (j.tg || j.ntfy || j.po_user) {
          LS.setItem('hcw_ntfy', j.ntfy || ''); LS.setItem('hcw_tg_token', j.tg || ''); LS.setItem('hcw_tg_chat', j.chat || '');
          LS.setItem('hcw_po_user', j.po_user || ''); LS.setItem('hcw_po_token', j.po_token || '');
        } } catch (e) {} } });
  }

  // ------------------------------------------------------------ notifications
  function push(title, body, prio) {
    prio = prio || 4;  // ntfy: 3 normal, 4 high, 5 = alarm (can bypass Do Not Disturb)
    const c = cfg();
    if (c.ntfy) {
      GM_xmlhttpRequest({ method: 'POST', url: 'https://ntfy.sh/' + encodeURIComponent(c.ntfy),
        headers: { Title: title, Priority: String(prio), Tags: prio >= 5 ? 'rotating_light,loudspeaker' : 'bell' }, data: body });
    }
    if (c.tgToken && c.tgChat) {
      GM_xmlhttpRequest({ method: 'POST', url: `https://api.telegram.org/bot${c.tgToken}/sendMessage`,
        headers: { 'Content-Type': 'application/json' },
        data: JSON.stringify({ chat_id: c.tgChat, text: title + '\n' + body, disable_notification: prio < 4 }) });
    }
    if (c.poUser && c.poToken) {  // iPhone alarm: priority 2 repeats until acknowledged
      const d = { token: c.poToken, user: c.poUser, title, message: body, priority: prio >= 5 ? '2' : '0' };
      if (prio >= 5) { d.retry = '60'; d.expire = '1800'; d.sound = 'siren'; }
      GM_xmlhttpRequest({ method: 'POST', url: 'https://api.pushover.net/1/messages.json',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, data: new URLSearchParams(d).toString() });
    }
    try { new Notification(title, { body }); } catch (e) { /* desktop notification optional */ }
    log(title + ' - ' + body.split('\n')[0]);
  }
  function once(key, fn) { const k = `hcw_once_${today()}_${key}`; if (LS.getItem(k)) return; LS.setItem(k, '1'); fn(); }

  // ------------------------------------------------------------ board reading
  // Row layout (from the official page's code): cell 0 = court room no. (with an info
  // icon whose onclick carries "SIDE LIST_TYPE ... Bench ID"), cell 1 = judges,
  // cell 2 = running serial ("display_string") + case numbers.
  function readBoard() {
    const rows = document.querySelectorAll('#display-board-table tbody tr');
    const out = [];
    rows.forEach(tr => {
      const td = tr.querySelectorAll('td');
      if (td.length < 3) return;
      const infoEl = td[0].querySelector('[onclick]');
      const info = infoEl ? infoEl.getAttribute('onclick') || '' : '';
      const room = (td[0].textContent.match(/\d+[A-Z]?/) || [''])[0];
      const strong = td[2].querySelector('strong');
      const serialText = (strong ? strong.textContent : td[2].textContent).replace(/ /g, ' ').trim();
      const serial = parseInt((serialText.match(/\d+/) || ['NaN'])[0], 10);
      out.push({
        room, serial, serialText, judges: td[1].textContent.trim(),
        detail: td[2].textContent.replace(/\s+/g, ' ').trim().slice(0, 300),
        side: PAGE_SIDE || (/ORIGINAL/i.test(info) || /^\s*O/i.test(serialText) ? 'O' : 'A'),
        daily: !info || /DAILY/i.test(info),
        listInfo: info.replace(/&nbsp;|_/g, ' ').slice(0, 120),
      });
    });
    return out;
  }
  function fetchedAt() {
    const m = document.body.innerText.match(/Data Fetched On:\s*([0-9]{1,2}:[0-9]{2}\s*[AP]M)/i);
    return m ? m[1] : '';
  }

  // ------------------------------------------------------------ hand the board to the program on this Mac
  let lastSent = '', lastSentAt = 0, bridgeOk = null, riseAt = null, riseWhy = '';
  function shareBoard(board, fetched) {
    const rows = board.map(r => ({ room: r.room, serialText: r.serialText, side: r.side, daily: r.daily, judges: r.judges.slice(0, 120), detail: r.detail }));
    const sig = JSON.stringify(rows.map(r => [r.room, r.side, r.serialText, r.daily]));
    if (sig === lastSent && Date.now() - lastSentAt < 120e3) return;
    GM_xmlhttpRequest({ method: 'POST', url: 'http://127.0.0.1:8766/board', headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ fetched, rows }), timeout: 8000,
      onload: r => {
        bridgeOk = r.status === 200;
        if (bridgeOk) {
          lastSent = sig; lastSentAt = Date.now();
          try { const j = JSON.parse(r.responseText); riseAt = j.rise; riseWhy = j.why || ''; } catch (e) {}
        }
      },
      onerror: () => { bridgeOk = false; }, ontimeout: () => { bridgeOk = false; } });
  }

  // ------------------------------------------------------------ load today's code from the program on this Mac
  let lastFetchTry = 0, autoNote = '';
  function autoLoad() {
    const raw = jget('hcw_watch', null);
    const auto = LS.getItem('hcw_watch_auto') || '';
    const haveToday = raw && !Array.isArray(raw) && raw.d === today();
    const manualToday = haveToday && !auto.startsWith(today());
    if (manualToday) return;  // you pasted today's code yourself: keep it
    const ageMin = auto.startsWith(today()) ? (Date.now() - +(auto.split('|')[1] || 0)) / 60e3 : 1e9;
    if (haveToday && ageMin < 30) return;
    if (Date.now() - lastFetchTry < 60e3) return;
    lastFetchTry = Date.now();
    GM_xmlhttpRequest({ method: 'GET', url: 'http://127.0.0.1:8766/watch', timeout: 120000,
      onload: r => {
        try {
          const j = JSON.parse(r.responseText);
          if (j.ok && j.code && j.code.d === today()) {
            const changed = JSON.stringify(j.code) !== JSON.stringify(raw);
            LS.setItem('hcw_watch', JSON.stringify(j.code));
            LS.setItem('hcw_watch_auto', today() + '|' + Date.now());
            autoNote = `Today's code loaded automatically (${j.code.w.length} matter${j.code.w.length === 1 ? '' : 's'}).`;
            if (changed) { $('hcw-watch').value = JSON.stringify(j.code); log(autoNote); }
          } else if (!haveToday) { autoNote = 'No code for today from the program (' + (j.reason || 'none') + ').'; }
        } catch (e) { /* ignore */ }
      },
      onerror: () => { if (!haveToday) autoNote = 'Could not reach the Case Monitor program on this Mac: paste the code from Telegram.'; },
    });
  }

  // ------------------------------------------------------------ day plan helpers
  function secOf(plan, serial) { return (plan || []).findIndex(s => serial >= s[0] && serial <= s[1]); }
  function secName(plan, i) { return i >= 0 && plan[i] ? plan[i][2] : '?'; }
  function slotText(s) { return s && s.length >= 5 ? ` (${fmt(s[3])}-${fmt(s[4])})` : ''; }
  // Is the board being at `serial` a planned move away from item `item`?
  function plannedMove(plan, fixed, serial, item, now) {
    const fx = (fixed || []).find(f => f[0] === serial);
    if (fx && Math.abs(now - fx[1]) <= 90) return `fixed item ${serial} at ${fmt(fx[1])}`;
    const bi = secOf(plan, serial), mi = secOf(plan, item);
    if (bi < 0 || mi < 0) return '';
    const b = plan[bi], m = plan[mi];
    if (m.length >= 5 && m[3] > now && b.length >= 5 && b[3] <= now && now < b[4]) return `your heading is scheduled from ${fmt(m[3])}`;
    if (m.length >= 5 && m[3] > now && b.length < 5) return `your heading is scheduled from ${fmt(m[3])}`;
    return '';
  }

  // ------------------------------------------------------------ history (only changes)
  function histKey(d) { return 'hcw_hist_' + (d || today()); }
  function record(board, watched) {
    const h = jget(histKey(), {});
    const t = nowMin();
    let changed = false;
    watched.forEach(k => {
      const [side, room] = k.split('|');
      const row = board.find(r => r.room === room && r.side === side);
      const val = row ? [t, isNaN(row.serial) ? null : row.serial, row.daily ? 1 : 0] : [t, null, 0];
      const arr = h[k] || (h[k] = []);
      const last = arr[arr.length - 1];
      if (!last || last[1] !== val[1] || last[2] !== val[2]) { arr.push(val); changed = true; }
    });
    if (changed) jset(histKey(), h);
    return h;
  }
  function pruneHistory() {
    const cutoff = Date.now() - KEEP_DAYS * 864e5;
    Object.keys(LS).forEach(k => {
      const m = k.match(/^hcw_(?:hist|once|item|sent)_(\d{2})(\d{2})(\d{4})/);
      if (m && new Date(+m[3], +m[2] - 1, +m[1]).getTime() < cutoff) LS.removeItem(k);
    });
  }

  // ------------------------------------------------------------ per-item tracking
  function itemState(w) { return jget(`hcw_item_${today()}_${w.court}_${w.item}`, { lastBelow: null, seenOn: false, done: '' }); }
  function saveItem(w, s) { jset(`hcw_item_${today()}_${w.court}_${w.item}`, s); }

  function checkItem(w, row, c, now) {
    const plan = c.plans[w.court] || [], fixed = c.fixed[w.court] || [];
    const st = itemState(w);
    const tag = `${w.side} Court ${w.court}`;
    if (w.monthly ? row.daily : !row.daily) {
      return `${tag}: running ${row.serialText || '-'} (${row.daily ? 'daily' : 'other'} list) | yours ${w.item}${w.monthly ? ' (monthly list)' : ''}: waiting`;
    }
    const gap = w.item - row.serial;
    if (isNaN(gap)) return `${tag}: running ${row.serialText || '-'} | yours ${w.item}`;
    const mySec = secName(plan, secOf(plan, w.item)), boardSec = secName(plan, secOf(plan, row.serial));
    let line = `${tag}: running ${row.serialText} [${boardSec}] | yours ${w.item} [${mySec}] | `;
    if (gap >= 0) {
      if (st.done && st.done !== 'on') { st.done = ''; }  // the board came back below your item: watch again
      st.lastBelow = Math.max(st.lastBelow || 0, row.serial);
      if (gap === 0) st.seenOn = true;
      saveItem(w, st);
      // the tightest level reached; one alert also covers the wider levels already passed
      const level = Math.min(...ALERT_LEVELS.filter(l => gap <= l));
      const k = l => `hcw_sent_${today()}_${w.court}_${w.item}_${l}`;
      if (isFinite(level) && !LS.getItem(k(level))) {
        ALERT_LEVELS.filter(l => l >= level).forEach(l => LS.setItem(k(l), '1'));
        const t = level === 0 ? `NOW: Court ${w.court} item ${w.item}` : `Court ${w.court}: ${gap} items to go`;
        push(t, `${w.kase}\nRunning serial: ${row.serialText}\nYour item: ${w.item}${w.sec ? ' (' + w.sec + ')' : ''}\n${row.judges}`, level <= 5 ? 5 : 4);
      }
      return line + `${gap} away`;
    }
    // the board is past your item
    const planned = plannedMove(plan, fixed, row.serial, w.item, now);
    if (planned) return line + `past it, but planned (${planned}); still watching`;
    if (st.done) return line + st.done;
    const myI = secOf(plan, w.item), bI = secOf(plan, row.serial);
    const lastSeen = st.lastBelow;
    let title, body, prio, verdict;
    if (st.seenOn) {
      verdict = 'called (it was on the board)';
      title = `Court ${w.court}: moved past your item ${w.item}`;
      body = `It was on the board; now at ${row.serialText}.\n${w.kase}`;
      prio = 3;
    } else if (lastSeen !== null && myI >= 0 && bI > myI) {
      verdict = `heading closed at about ${lastSeen}; probably NOT reached`;
      title = `Court ${w.court}: '${mySec}' closed before your item ${w.item}`;
      body = `The board left '${mySec}' at about item ${lastSeen} (${fmt(now)}) and is now at ${row.serialText} ('${boardSec}'${slotText(plan[bI])}).\n` +
        `Your item ${w.item} was most probably NOT reached. Check with the court / case status.\n${w.kase}`;
      prio = 5;
    } else if (lastSeen !== null) {
      verdict = `board jumped ${lastSeen} -> ${row.serial}; may have been called or passed over`;
      title = `Court ${w.court}: board jumped past your item ${w.item}`;
      body = `The board went from ${lastSeen} to ${row.serialText} without showing ${w.item}. It may have been called quickly or passed over.\n${w.kase}`;
      prio = 5;
    } else {
      verdict = bI > myI && myI >= 0 ? `already past when watching began; heading '${mySec}' is over - probably not reached`
        : 'already past when watching began; may have been called';
      title = `Court ${w.court}: board already past your item ${w.item}`;
      body = `When watching began the board was at ${row.serialText} ('${boardSec}'). Your item ${w.item} is in '${mySec}'.\n` +
        (bI > myI && myI >= 0 ? 'The board is in a later heading, so your item was probably not reached.' : 'It may have been called before watching began.') +
        `\n${w.kase}`;
      prio = 4;
    }
    st.done = verdict; saveItem(w, st);
    push(title, body, prio);
    return line + verdict;
  }

  // ------------------------------------------------------------ main check
  let lastFetched = '', lastFetchedChange = Date.now(), captchaSince = 0;
  const LATE_FREEZE = 15 * 60 + 15;       // a board frozen after this time = courts have risen
  function courtHours(now) { return now >= 10 * 60 + 25 && now <= 16 * 60 + 45 && !(riseAt !== null && riseAt > 0 && now >= riseAt); }
  function risenFlag() { return `hcw_risen_${today()}`; }
  function check() {
    const now = nowMin();
    // courts not working today / risen early per a notice sent to the bot
    if (riseAt === 0) { status('No court work today (per the notice sent to the bot). Nothing to watch.'); return; }
    if (riseAt !== null && riseAt > 0 && now >= riseAt + 10) {
      once('risen_notice', () => push('Courts have risen', `Per the notice (${riseWhy || 'early rising'}), courts rose at ${fmt(riseAt)}. Watching stopped for today.`, 3));
      status(`Courts rose at ${fmt(riseAt)} (notice). Watching stopped for today.`); return;
    }
    const captchaVisible = document.querySelector('#captcha_div') &&
      document.querySelector('#captcha_div').offsetParent !== null;
    if (captchaVisible) {
      captchaSince = captchaSince || Date.now();
      if (courtHours(now) && Date.now() - captchaSince > 2 * 60e3) {
        once('captcha_' + Math.floor(Date.now() / 36e5), () => push('Board needs the CAPTCHA', 'The display board is waiting for the CAPTCHA, so nothing is being watched. Type it on the Mac.', 4));
      }
      status('Waiting for you to enter the CAPTCHA on the page...'); return;
    }
    captchaSince = 0;
    const f = fetchedAt();
    if (f !== lastFetched) { lastFetched = f; lastFetchedChange = Date.now(); LS.removeItem(risenFlag()); }
    const stale = f && (Date.now() - lastFetchedChange) > STALE_MIN * 60e3;
    // late in the day a frozen board means the courts have risen (e.g. a condolence resolution): stop, don't alarm
    if (LS.getItem(risenFlag()) || (stale && now >= LATE_FREEZE && (Date.now() - lastFetchedChange) > 10 * 60e3)) {
      if (!LS.getItem(risenFlag())) {
        LS.setItem(risenFlag(), f || '1');
        push('Board stopped for the day', `The display board has not changed since ${f || 'a while'}, so the courts appear to have risen. ` +
          'Watching stopped for today (reload the page if a court is still sitting).', 3);
      }
      status(`Board stopped at ${LS.getItem(risenFlag())}: courts appear to have risen. Watching stopped for today.`);
      return;
    }
    if (stale && courtHours(now) && now < LATE_FREEZE) {
      once('stale_' + f, () => push('Board not updating', `The board's data has been stuck at ${f} for over ${STALE_MIN} minutes. Reload the page (Cmd+R) and type the CAPTCHA again.`, 4));
    }
    const board = readBoard();
    if (board.length && !stale) shareBoard(board, f);
    autoLoad();
    loadSettings();
    const c = code();
    if (!c.watch.length) { status((autoNote || 'Loading today\'s code from the Case Monitor program...') + '\n(Or paste the board watcher code from the 9:30 AM Telegram message.)'); return; }
    if (c.d && c.d !== today()) { status(autoNote || `This code is for ${c.d.slice(0, 2)}-${c.d.slice(2, 4)}. Loading today's code...`); return; }
    if (!board.length) { status('Board table empty (court not sitting yet?)'); return; }
    const mine = c.watch.filter(w => PAGE_SIDE ? w.side === PAGE_SIDE : ['A', 'O'].includes(w.side));
    const keys = [...new Set(mine.map(w => `${w.side}|${w.court}`))];
    record(board, keys);
    const lines = [];
    mine.forEach(w => {
      const row = board.find(r => r.room === w.court && r.side === w.side);
      if (!row) {
        const other = board.find(r => r.room === w.court);
        lines.push(`${w.side} Court ${w.court}: ${other ? `now on the ${other.side === 'O' ? 'Original' : 'Appellate'} Side list (${other.serialText})` : 'not on board'}`);
        if (now >= START_CHECK_AT && now < 13 * 60 && !other) {
          once(`start_${w.court}`, () => push(`Court ${w.court} not on the board yet`, `At ${fmt(now)} Court ${w.court} is not showing on the display board.`, 3));
        }
        return;
      }
      lines.push(checkItem(w, row, c, now));
    });
    status((stale ? `⚠ Board data stuck at ${f}: reload + CAPTCHA\n` : '') + (autoNote ? autoNote + '\n' : '') + lines.join('\n') +
      (bridgeOk === false ? '\n(Telegram questions about the board: the Case Monitor listener is not running on this Mac)' : ''));
    if (now >= SUMMARY_AT) once('summary', () => summary(c));
  }

  // ------------------------------------------------------------ 5 PM summary
  function summary(c) {
    const h = jget(histKey(), {});
    const out = [];
    Object.keys(h).forEach(k => {
      const [side, room] = k.split('|');
      const plan = c.plans[room] || [];
      const segs = [];
      h[k].filter(x => x[1] !== null && x[2] === 1).forEach(([t, s]) => {
        const name = secName(plan, secOf(plan, s));
        const last = segs[segs.length - 1];
        if (last && last.name === name) { last.to = s; last.t2 = t; } else segs.push({ name, from: s, to: s, t1: t, t2: t });
      });
      const items = c.watch.filter(w => w.court === room).map(w => {
        const st = itemState(w);
        return `your ${w.item}: ${st.done || (st.seenOn ? 'called' : st.lastBelow ? 'last seen ' + st.lastBelow + ' before it' : 'no data')}`;
      });
      out.push(`${side} Court ${room}: ` + (segs.length ? segs.map(s => `${s.name} ${s.from}->${s.to} (${fmt(s.t1)}-${fmt(s.t2)})`).join(' · ') : 'no board data') +
        (items.length ? '\n  ' + items.join('; ') : ''));
    });
    if (out.length) push('Court day summary', out.join('\n'), 3);
  }

  // ------------------------------------------------------------ small panel
  const panel = document.createElement('div');
  panel.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:99999;width:360px;background:#fff;color:#111;' +
    'border:2px solid #4f378a;border-radius:10px;padding:10px;font:12px/1.4 system-ui,sans-serif;box-shadow:0 4px 18px rgba(0,0,0,.25)';
  panel.innerHTML = `
    <b>Board Watcher 3</b> <span id=hcw-min style="float:right;cursor:pointer">_</span>
    <div id=hcw-body>
      <pre id=hcw-status style="white-space:pre-wrap;background:#f4f1fa;padding:6px;border-radius:6px;max-height:180px;overflow:auto"></pre>
      <label><b>Paste code here</b> (the board watcher code from Telegram, or the settings code)</label>
      <textarea id=hcw-watch rows=3 style="width:100%"></textarea>
      <details id=hcw-set><summary>Alert settings (filled in by the program on this Mac)</summary>
        <label>ntfy topic</label><input id=hcw-ntfy style="width:100%" placeholder="casemonitor-...">
        <label>Telegram bot token (numbers:letters)</label><input id=hcw-tgt style="width:100%" placeholder="1234567890:AA...">
        <label>Telegram chat id (numbers)</label><input id=hcw-tgc style="width:100%" placeholder="123456789">
      </details>
      <button id=hcw-save>Save</button> <button id=hcw-test>Test alert</button>
      <div id=hcw-msg style="color:#a11;margin-top:4px"></div>
      <div id=hcw-log style="margin-top:6px;color:#555;max-height:80px;overflow:auto"></div>
    </div>`;
  document.body.appendChild(panel);
  const $ = id => panel.querySelector('#' + id);
  function fill() {
    const raw = LS.getItem('hcw_watch') || '';
    $('hcw-watch').value = raw && raw !== '[]' ? raw : '';
    const c = cfg();
    $('hcw-ntfy').value = c.ntfy; $('hcw-tgt').value = c.tgToken; $('hcw-tgc').value = c.tgChat;
    if (!c.ntfy && !c.tgToken) $('hcw-set').open = true;
  }
  loadSettings();
  setTimeout(fill, 1500);
  fill();
  $('hcw-save').onclick = () => {
    $('hcw-msg').textContent = '';
    const txt = $('hcw-watch').value.trim();
    if (txt) {
      let obj;
      try { obj = JSON.parse(txt); } catch (e) { $('hcw-msg').textContent = 'The code is not complete. Copy it again (tap the code in Telegram once).'; return; }
      if (obj && !Array.isArray(obj) && ('ntfy' in obj || 'tg' in obj)) {  // settings code
        $('hcw-ntfy').value = obj.ntfy || ''; $('hcw-tgt').value = obj.tg || ''; $('hcw-tgc').value = obj.chat || '';
        $('hcw-watch').value = LS.getItem('hcw_watch') && LS.getItem('hcw_watch') !== '[]' ? LS.getItem('hcw_watch') : '';
      } else {
        LS.setItem('hcw_watch', JSON.stringify(obj));
        LS.removeItem('hcw_watch_auto');  // your own paste wins over the automatic code for today
        autoNote = '';
      }
    }
    // tidy the alert settings: move a bot token that was pasted into the wrong box
    let n = $('hcw-ntfy').value.trim(), t = $('hcw-tgt').value.trim(), ch = $('hcw-tgc').value.trim();
    if (TOKEN_RE.test(n) && !TOKEN_RE.test(t)) { const x = t; t = n; n = TOPIC_RE.test(x) ? x : ''; }
    if (TOKEN_RE.test(ch) && !TOKEN_RE.test(t)) { t = ch; ch = ''; }
    const probs = [];
    if (n && !TOPIC_RE.test(n)) probs.push('ntfy topic looks wrong');
    if (t && !TOKEN_RE.test(t)) probs.push('bot token looks wrong (it should look like 1234567890:AA...)');
    if (ch && !/^-?\d+$/.test(ch)) probs.push('chat id should be numbers only');
    if (t && !ch) probs.push('chat id is missing');
    $('hcw-ntfy').value = n; $('hcw-tgt').value = t; $('hcw-tgc').value = ch;
    LS.setItem('hcw_ntfy', n); LS.setItem('hcw_tg_token', t); LS.setItem('hcw_tg_chat', ch);
    $('hcw-msg').textContent = probs.length ? 'Check: ' + probs.join('; ') + '. (These normally fill in by themselves from the program on this Mac.)' : 'Saved.';
    check();
  };
  $('hcw-test').onclick = () => push('Board Watcher test', 'If you see this on your phone, alerts work.', 5);
  $('hcw-min').onclick = () => { const b = $('hcw-body'); b.style.display = b.style.display === 'none' ? '' : 'none'; };
  function status(s) { $('hcw-status').textContent = new Date().toLocaleTimeString() + (lastFetched ? `  (board data ${lastFetched})` : '') + '\n' + s; }
  function log(s) { $('hcw-log').innerHTML = `<div>${new Date().toLocaleTimeString()} ${s.replace(/</g, '&lt;')}</div>` + $('hcw-log').innerHTML; }

  try { if (Notification.permission === 'default') Notification.requestPermission(); } catch (e) {}

  // Keep the screen on while this board tab is open and visible (Chrome "wake lock").
  let wakeLock = null;
  async function keepScreenOn() {
    try { if (document.visibilityState === 'visible' && !wakeLock) {
      wakeLock = await navigator.wakeLock.request('screen');
      wakeLock.addEventListener('release', () => { wakeLock = null; });
    } } catch (e) { /* not allowed until you click the page once */ }
  }
  document.addEventListener('visibilitychange', keepScreenOn);
  document.addEventListener('click', keepScreenOn);
  keepScreenOn();
  pruneHistory();
  setInterval(check, CHECK_EVERY_MS);
  check();
})();
