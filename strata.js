#!/usr/bin/env node
'use strict';

const { Transform } = require('stream');
const http  = require('http');
const https = require('https');

// ── ANSI helpers ──────────────────────────────────────────────────────────────
const C = {
  reset:  '\x1B[0m',
  bold:   '\x1B[1m',
  dim:    '\x1B[2m',
  red:    '\x1B[91m',
  yellow: '\x1B[93m',
  gray:   '\x1B[90m',
  cyan:   '\x1B[96m',
  green:  '\x1B[92m',
};

// ── Layout ────────────────────────────────────────────────────────────────────
//
//  col: 1 │←─ LEFT_W ─→│ COL_MLB │←── MAP_W ──→│ COL_MRB │←─ RIGHT_W ─→│ cols
//  row:
//    1   ╔═══════════╦═════════════════════╦═══════════╗
//    2   ║ INCIDENT  ║   ▌ S T R A T A     ║ INTEL     ║
//    3   ╠═══════════╬═════════════════════╬═══════════╣
//    4+  ║ feed      ║       map           ║ stats     ║
//    MID ╠═══════════╩═════════════════════╩═══════════╣
//   TTL  ║   ▌ POLYMARKET PREDICTIONS                  ║
//   THR  ╠════════════════════════════════════════════╣
//   TC+  ║   trades panel content                     ║
//   BOT  ╚════════════════════════════════════════════╝
//    N   ║ status bar                                 ║
//
const cols = process.stdout.columns || 120;
const rows = process.stdout.rows    || 40;

const LEFT_W  = Math.floor(cols * 0.22);
const MAP_W   = Math.floor(cols * 0.50);
const RIGHT_W = cols - LEFT_W - MAP_W - 4;  // 4 = four border │ chars

// Column landmarks (1-indexed)
const COL_MLB = LEFT_W + 2;           // map-left border
const COL_MRB = LEFT_W + 3 + MAP_W;  // map-right border
const COL_RS  = LEFT_W + 4 + MAP_W;  // right panel content start
const leftOffset = COL_MLB;

// Trades panel height (number of content rows)
const TRADES_H = 7;

// Row landmarks (bottom-up)
const BOTTOM_ROW          = rows - 1;              // ╚═══╝
const TRADES_CONTENT_END  = rows - 2;
const TRADES_CONTENT_START= rows - TRADES_H - 1;
const TRADES_HEADER_ROW   = rows - TRADES_H - 2;   // ╠════╣
const TRADES_TITLE_ROW    = rows - TRADES_H - 3;   // ║ POLYMARKET ║
const MID_SEP_ROW         = rows - TRADES_H - 4;   // ╠══╩══╩══╣

const mapTopLine    = 4;
const mapBottomLine = MID_SEP_ROW - 1;
const mapHeight     = mapBottomLine - mapTopLine + 1;

const canvasWidth   = MAP_W * 2;
const canvasHeight  = (mapHeight - 1) * 4;

// ── State ─────────────────────────────────────────────────────────────────────
const incidents   = [];  // { lat, lon, col, row, headline, summary, event_type, confidence, location, ts, pmUrl }
const feedLines   = [];  // AI event entries: {t, text}
let   totalEvents = 0;
const countrySeen = new Set();
let   lastEventInfo = '';

// Popup state — shown when user clicks a red dot
let popup = null;  // null | { incident, boxCol, boxRow }

// Telegram message feed (structured, from /telegram endpoint)
const telegramMsgs = [];  // [{ts, channel, text, mediaPath, mediaType, expanded}]
const leftPanelRowMap = {};  // terminal row → telegramMsgs index (rebuilt on each draw)

// Claude trade rankings (latest event with polymarket_trades)
let latestClaudeTrades = null;  // { headline, primary: [], secondary: [] }
let tradesPanelScroll = 0;      // scroll offset (rows) for bottom trades panel

// Executed trades log (pushed from Python trade_executor.py)
const executedTrades = [];  // [ { timestamp, event, trade, market, price, status, url } ]

// Polymarket state
const polyState = {
  markets:    [],   // [{id, question, category, yesPrice, noPrice, change}]
  prevPrices: {},   // id → yesPrice
};

// ── Graceful exit ─────────────────────────────────────────────────────────────
function cleanup() {
  if (process.stdin.isTTY && process.stdin.isRaw) process.stdin.setRawMode(false);
  process.stdout.write('\x1B[?1000l\x1B[?1006l\x1B[r\x1B[?6l\x1B[0m\x1B[?25h\n');
  process.exit(0);
}
process.on('SIGINT',  cleanup);
process.on('SIGTERM', cleanup);

// ── Cursor helper ─────────────────────────────────────────────────────────────
function withAbsPos(fn) {
  process.stdout.write('\x1B7\x1B[?6l');
  fn();
  process.stdout.write('\x1B8');
}

// ── Utilities ─────────────────────────────────────────────────────────────────
function wrapText(text, width) {
  const out   = [];
  const words = text.split(/\s+/);
  let   line  = '';
  for (const w of words) {
    const candidate = line ? `${line} ${w}` : w;
    if (candidate.length > width) {
      if (line) out.push(line);
      line = w.length > width ? w.slice(0, width) : w;
    } else {
      line = candidate;
    }
  }
  if (line) out.push(line);
  return out;
}

function getDateTimeStr() {
  const now = new Date();
  const d = now.toISOString().slice(0, 10);         // YYYY-MM-DD UTC
  const t = now.toISOString().slice(11, 19) + 'Z';  // HH:MM:SSZ UTC
  return `${d}  ${t}`;
}

function drawDateTime() {
  withAbsPos(() => {
    const dtStr = getDateTimeStr();
    const col = COL_MLB + 1 + MAP_W - dtStr.length;
    if (col > COL_MLB + 1) {
      process.stdout.write(`\x1B[2;${col}H${C.bold}${dtStr}${C.reset}`);
    }
  });
}

function tryParseJSON(s) {
  try { return JSON.parse(s); } catch { return null; }
}

function httpsGet(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { headers: { 'User-Agent': 'STRATA/1.0' } }, (res) => {
      let body = '';
      res.on('data', chunk => { body += chunk; });
      res.on('end', () => {
        try { resolve(JSON.parse(body)); }
        catch (e) { reject(new Error('parse: ' + body.slice(0, 80))); }
      });
    });
    req.on('error', reject);
    req.setTimeout(8000, () => { req.destroy(); reject(new Error('timeout')); });
  });
}

// ── Border / chrome ───────────────────────────────────────────────────────────
function drawBorders() {
  withAbsPos(() => {
    const out = [];
    const mv  = (r, c) => `\x1B[${r};${c}H`;
    const seg = (n)    => '═'.repeat(Math.max(0, n));

    // Row 1 — top border
    out.push(mv(1, 1));
    out.push(`${C.dim}╔${seg(LEFT_W)}╦${seg(MAP_W)}╦${seg(RIGHT_W)}╗${C.reset}`);

    // Row 2 — panel titles
    out.push(mv(2, 1));
    out.push(`${C.dim}║${C.reset}${C.red}${C.bold}${'  INCIDENT FEED'.padEnd(LEFT_W)}${C.reset}`);
    out.push(mv(2, COL_MLB));
    const strataTitle = '  ▌ S T R A T A';
    const dtStr = getDateTimeStr();
    out.push(`${C.dim}║${C.reset}${C.red}${C.bold}${strataTitle}${C.reset}${C.bold}${dtStr.padStart(MAP_W - strataTitle.length)}${C.reset}`);
    out.push(mv(2, COL_MRB));
    out.push(`${C.dim}║${C.reset}${C.cyan}${C.bold}${'  INTEL STATS'.padEnd(RIGHT_W)}${C.reset}`);
    out.push(mv(2, cols));
    out.push(`${C.dim}║${C.reset}`);

    // Row 3 — section separator
    out.push(mv(3, 1));
    out.push(`${C.dim}╠${seg(LEFT_W)}╬${seg(MAP_W)}╬${seg(RIGHT_W)}╣${C.reset}`);

    // Content rows — vertical borders for 3-column layout
    for (let r = mapTopLine; r <= mapBottomLine; r++) {
      out.push(`${mv(r, 1)}${C.dim}║${C.reset}`);
      out.push(`${mv(r, COL_MLB)}${C.dim}║${C.reset}`);
      out.push(`${mv(r, COL_MRB)}${C.dim}║${C.reset}`);
      out.push(`${mv(r, cols)}${C.dim}║${C.reset}`);
    }

    // MID_SEP_ROW — close 3-column layout, open trades panel
    out.push(mv(MID_SEP_ROW, 1));
    out.push(`${C.dim}╠${seg(LEFT_W)}╩${seg(MAP_W)}╩${seg(RIGHT_W)}╣${C.reset}`);

    // TRADES_TITLE_ROW
    out.push(mv(TRADES_TITLE_ROW, 1));
    out.push(`${C.dim}║${C.reset}${C.yellow}${C.bold}${'  ▌ POLYMARKET PREDICTIONS'.padEnd(cols - 2)}${C.reset}`);
    out.push(mv(TRADES_TITLE_ROW, cols));
    out.push(`${C.dim}║${C.reset}`);

    // TRADES_HEADER_ROW — sub-separator
    out.push(mv(TRADES_HEADER_ROW, 1));
    out.push(`${C.dim}╠${seg(cols - 2)}╣${C.reset}`);

    // Trades content rows — outer borders only
    for (let r = TRADES_CONTENT_START; r <= TRADES_CONTENT_END; r++) {
      out.push(`${mv(r, 1)}${C.dim}║${C.reset}`);
      out.push(`${mv(r, cols)}${C.dim}║${C.reset}`);
    }

    // BOTTOM_ROW — close trades panel
    out.push(mv(BOTTOM_ROW, 1));
    out.push(`${C.dim}╚${seg(cols - 2)}╝${C.reset}`);

    // Status bar outer borders
    out.push(`${mv(rows, 1)}${C.dim}║${C.reset}`);
    out.push(`${mv(rows, cols)}${C.dim}║${C.reset}`);

    process.stdout.write(out.join(''));
  });
}

// ── Status bar ────────────────────────────────────────────────────────────────
const PORT = parseInt(process.env.PORT || '3001', 10);

function drawStatusBar() {
  withAbsPos(() => {
    const inner  = cols - 2;
    const left   = `  ${C.green}● HTTP :${PORT}${C.reset}  ${C.dim}[ x ] exit${C.reset}`;
    const visLen = left.replace(/\x1B\[[^m]*m/g, '').length;
    const pad    = ' '.repeat(Math.max(0, inner - visLen));
    process.stdout.write(`\x1B[${rows};2H${left}${pad}`);
  });
}

// ── Left panel — incident feed ────────────────────────────────────────────────
// Feed shows three kinds of entries (newest at bottom):
//   1. "All systems running." — single system status line (gray, once)
//   2. Telegram messages      — orange, collapsible with [ + ] / [ - ]
//   3. AI event analysis      — sep / meta / tag / h1 / body (existing colors)
//
// All other sys/ok/warn pipeline chatter is suppressed.

function buildLeftPanelRows() {
  // Returns [{t, text, tmIdx?}] — ordered oldest→newest, ready to render.
  const W    = LEFT_W;
  const rows = [];

  const pushTm = (tmIdx, lines, type) => {
    for (const l of lines) rows.push({t: type, text: l, tmIdx});
  };

  // ── AI event entries (feedLines: sep, meta, tag, h1, body, sys-filtered) ──
  const aiEntries = feedLines.filter(e => {
    const t = typeof e === 'string' ? 'sys' : e.t;
    if (t === 'sys') return /all systems running/i.test(typeof e === 'string' ? e : e.text);
    return ['sep','meta','tag','h1','body'].includes(t);
  });
  for (const entry of aiEntries) {
    const e = typeof entry === 'string' ? {t:'sys', text:entry} : entry;
    if (e.t === 'h1') {
      for (const l of wrapText(e.text, W - 2)) rows.push({t:'h1', text:l});
    } else if (e.t === 'body') {
      for (const l of wrapText(e.text, W - 3)) rows.push({t:'body', text:'   ' + l});
    } else {
      rows.push(e);
    }
  }

  // ── Telegram messages (interleaved, most recent at bottom) ────────────────
  const tmSlice = telegramMsgs.slice(-40);  // keep last 40 messages
  for (let i = 0; i < tmSlice.length; i++) {
    const tm     = tmSlice[i];
    const tmIdx  = telegramMsgs.length - tmSlice.length + i;  // real index
    const lines  = (tm.text || '').split('\n').filter(Boolean);
    const first  = (lines[0] || '').slice(0, W - 14);  // leave room for prefix
    const prefix = `${tm.ts || ''} @${tm.channel || ''}`;
    const toggle = tm.expanded ? '[-]' : '[+]';
    const head   = `${toggle} ${prefix.slice(0, 16).padEnd(16)} ${first}`;

    // Header row
    rows.push({t: 'tg_head', text: head.slice(0, W), tmIdx});

    if (tm.expanded) {
      // All lines of message body
      for (let li = 1; li < lines.length; li++) {
        for (const l of wrapText(lines[li], W - 4))
          rows.push({t: 'tg_body', text: '    ' + l, tmIdx});
      }
      // Media indicator
      if (tm.mediaType) {
        const icon = tm.mediaType === 'video' ? '🎥' : '📷';
        const fn   = tm.mediaPath ? ` ${tm.mediaPath.split('/').pop()}` : '';
        rows.push({t: 'tg_media', text: `    ${icon}${fn}`, tmIdx});
      }
    }
  }

  return rows;
}

function drawLeftPanel() {
  withAbsPos(() => {
    const W     = LEFT_W;
    const xCol  = 2;
    const r0    = mapTopLine;
    const r1    = mapBottomLine;
    const blank = ' '.repeat(W);

    for (let r = r0; r <= r1; r++) {
      process.stdout.write(`\x1B[${r};${xCol}H${blank}`);
    }

    const visible = r1 - r0 + 1;
    const allRows = buildLeftPanelRows();
    const view    = allRows.slice(Math.max(0, allRows.length - visible));

    // Clear old row map
    for (const k of Object.keys(leftPanelRowMap)) delete leftPanelRowMap[k];

    let r = r0;
    for (const e of view) {
      if (r > r1) break;
      let rendered = '';
      switch (e.t) {
        case 'tg_head':
          // Orange for Telegram messages — leading [+]/[-] is the clickable toggle
          rendered = `\x1B[38;5;214m${e.text.slice(0, W).padEnd(W)}${C.reset}`;
          if (e.tmIdx !== undefined) leftPanelRowMap[r] = e.tmIdx;
          break;
        case 'tg_body':
          rendered = `\x1B[38;5;172m${C.dim}${e.text.slice(0, W).padEnd(W)}${C.reset}`;
          break;
        case 'tg_media':
          rendered = `\x1B[38;5;172m${e.text.slice(0, W).padEnd(W)}${C.reset}`;
          break;
        case 'h1':
          rendered = `${C.bold}${e.text.slice(0, W).padEnd(W)}${C.reset}`;
          break;
        case 'meta':
          rendered = `${C.cyan}${C.dim}${e.text.slice(0, W).padEnd(W)}${C.reset}`;
          break;
        case 'tag':
          rendered = `${C.yellow}${C.dim}${e.text.slice(0, W).padEnd(W)}${C.reset}`;
          break;
        case 'body':
          rendered = `${C.dim}${e.text.slice(0, W).padEnd(W)}${C.reset}`;
          break;
        case 'sep':
          rendered = `${C.dim}${'─'.repeat(W)}${C.reset}`;
          break;
        case 'sys':
          rendered = `${C.gray}${C.dim}${e.text.slice(0, W).padEnd(W)}${C.reset}`;
          break;
        default:
          rendered = `${C.dim}${e.text.slice(0, W).padEnd(W)}${C.reset}`;
      }
      process.stdout.write(`\x1B[${r};${xCol}H${rendered}`);
      r++;
    }
  });
}

// ── Right panel — intel stats ─────────────────────────────────────────────────
function drawRightPanel() {
  withAbsPos(() => {
    const W     = RIGHT_W;
    const xCol  = COL_RS;
    const r0    = mapTopLine;
    const r1    = mapBottomLine;
    const blank = ' '.repeat(W);

    for (let r = r0; r <= r1; r++) {
      process.stdout.write(`\x1B[${r};${xCol}H${blank}`);
    }

    let r = r0;
    const put = (text, color = '') => {
      if (r > r1) return;
      process.stdout.write(`\x1B[${r};${xCol}H${color}${text.slice(0, W).padEnd(W)}${C.reset}`);
      r++;
    };

    // ── Compact stats header ──────────────────────────────────────────────────
    r++;
    const level      = totalEvents === 0 ? 'LOW' : totalEvents < 5 ? 'MEDIUM' : 'HIGH';
    const levelColor = level === 'LOW' ? C.green : level === 'MEDIUM' ? C.yellow : C.red;
    const statsLine  = ` Evts:${totalEvents}  Ctry:${countrySeen.size}  ${levelColor}●${C.reset}${C.dim}`;
    put(statsLine, C.dim);
    put('─'.repeat(W), C.dim);
    r++;

    // ── Trades log ────────────────────────────────────────────────────────────
    put(' TRADES LOG', `${C.cyan}${C.bold}`);
    put('─'.repeat(W), C.dim);

    if (executedTrades.length === 0) {
      put(' (no trades yet)', C.dim);
    } else {
      // Show most-recent trades first, fill available rows
      const trades = [...executedTrades].reverse();
      for (const t of trades) {
        if (r > r1) break;
        const ts     = t.timestamp ? t.timestamp.slice(5, 16).replace('T', ' ') : '??-?? ??:??';
        const status = (t.status || '?').slice(0, 9);
        const scol   = status === 'PLACED'         ? C.green
                     : status === 'DRY_RUN'        ? C.yellow
                     : status.startsWith('ERROR')  ? C.red
                     : C.dim;
        const dir    = (t.trade || '').replace('BUY ', '');
        const dcol   = dir === 'YES' ? C.green : dir === 'NO' ? C.red : C.yellow;
        const prefix = `${C.dim}${ts} ${dcol}${dir.padEnd(4)}${C.reset}${scol}${status.padEnd(10)}${C.reset}`;
        // Prefix visible length: ts(11) + space(1) + dir(4) + space(1) + status(10) = 27
        const marketW = W - 27;
        const market  = (t.market || '').slice(0, marketW);
        process.stdout.write(`\x1B[${r};${xCol}H${prefix}${C.dim}${market}${C.reset}${' '.repeat(Math.max(0, W - 27 - market.length))}`);
        r++;
      }
    }
  });
}

// ── Trades / Polymarket panel ─────────────────────────────────────────────────
function buildTradesPanelLines() {
  // Returns an array of pre-rendered line strings (with ANSI) for the trades panel.
  const innerW = cols - 2;
  const lines  = [];

  const push = (text) => lines.push(text);

  // ── Claude AI trade picks ──────────────────────────────────────────────────
  if (latestClaudeTrades) {
    const all = [
      ...latestClaudeTrades.primary.map(t   => ({ ...t, _sec: 'PRI' })),
      ...latestClaudeTrades.secondary.map(t => ({ ...t, _sec: 'SEC' })),
    ];

    const title = `  ▸ ${latestClaudeTrades.headline}`;
    push(`${C.cyan}${C.bold}${title.slice(0, innerW).padEnd(innerW)}${C.reset}`);

    const R_W = 2, S_W = 3, D_W = 8, P_W = 5, U_W = 11;
    const FIXED = 2 + R_W + 3 + S_W + 3 + D_W + 3 + P_W + 3 + U_W + 3;
    const M_W   = Math.max(8, innerW - FIXED);

    const hdr = '  ' +
      '#'.padEnd(R_W)   + ' │ ' + 'S'.padEnd(S_W)  + ' │ ' +
      'TRADE'.padEnd(D_W) + ' │ ' + 'PRICE'.padStart(P_W) + ' │ ' +
      'URGENCY'.padEnd(U_W) + ' │ ' + 'MARKET';
    push(`${C.dim}${hdr.slice(0, innerW)}${C.reset}`);

    for (const t of all) {
      const secColor   = t._sec === 'PRI' ? C.yellow : C.dim;
      const urgColor   = t.urgency === 'immediate'  ? C.red
                       : t.urgency === 'short-term' ? C.yellow : C.dim;
      const tradeColor = /YES|OVER/i.test(t.trade || '') ? C.green
                       : /NO|UNDER/i.test(t.trade || '') ? C.red : C.yellow;

      const rankStr = String(t.rank || '').padEnd(R_W);
      const secStr  = (t._sec || '').slice(0, S_W).padEnd(S_W);
      const dirStr  = (t.trade || '').slice(0, D_W).padEnd(D_W);
      const prcStr  = t.current_price != null
        ? `${Math.round(t.current_price * 100)}%`.padStart(P_W) : '   — ';
      const urgStr  = (t.urgency || '').slice(0, U_W).padEnd(U_W);
      const mktStr  = (t.market || '').slice(0, M_W);

      push(
        `  ${C.dim}${rankStr}${C.reset}` +
        ` ${C.dim}│${C.reset} ${secColor}${secStr}${C.reset}` +
        ` ${C.dim}│${C.reset} ${tradeColor}${dirStr}${C.reset}` +
        ` ${C.dim}│${C.reset}${C.yellow}${prcStr}${C.reset}` +
        ` ${C.dim}│${C.reset} ${urgColor}${urgStr}${C.reset}` +
        ` ${C.dim}│${C.reset} ${C.gray}${mktStr}${C.reset}`
      );
    }
    return lines;
  }

  // ── Polymarket polling data (fallback) ────────────────────────────────────
  if (polyState.markets.length === 0) {
    push(`${C.dim}  Fetching Polymarket data...${C.reset}`);
    return lines;
  }

  const CAT_W  = 11;
  const YES_W  = 6;
  const NO_W   = 6;
  const CHG_W  = 7;
  const FIXED  = 2 + CAT_W + 3 + 3 + YES_W + 3 + NO_W + 3 + CHG_W;
  const MKT_W  = Math.max(8, innerW - FIXED);

  const hdr = (
    '  ' +
    'CATEGORY'.padEnd(CAT_W) +
    ' │ ' +
    'MARKET'.padEnd(MKT_W) +
    ' │ ' +
    'YES'.padStart(YES_W) +
    ' │ ' +
    'NO'.padStart(NO_W) +
    ' │ ' +
    'CHNG'.padStart(CHG_W)
  );
  push(`${C.dim}${hdr.slice(0, innerW)}${C.reset}`);

  for (const m of polyState.markets) {
    const catColor = m.category === 'MILITARY'   ? C.red
                   : m.category === 'ECONOMIC'   ? C.yellow
                   : C.cyan;

    const yp  = Math.round(m.yesPrice * 100);
    const np  = Math.round(m.noPrice  * 100);
    const cat = m.category.slice(0, CAT_W).padEnd(CAT_W);
    const mkt = (m.question || '').slice(0, MKT_W).padEnd(MKT_W);
    const yes = `${yp}¢`.padStart(YES_W);
    const no  = `${np}¢`.padStart(NO_W);

    let chgStr = '  —  ';
    let chgCol = C.dim;
    if (m.change !== null && m.change !== undefined) {
      const d = Math.round(m.change * 100);
      if (d > 0)      { chgStr = `+${d}¢`; chgCol = C.green; }
      else if (d < 0) { chgStr = `${d}¢`;  chgCol = C.red;   }
      else            { chgStr = ' ±0¢';   chgCol = C.dim;   }
    }
    const chg = chgStr.padStart(CHG_W);

    push(
      `  ${catColor}${cat}${C.reset}` +
      ` ${C.dim}│${C.reset} ` +
      `${C.gray}${mkt}${C.reset}` +
      ` ${C.dim}│${C.reset}` +
      `${C.yellow}${yes}${C.reset}` +
      ` ${C.dim}│${C.reset}` +
      `${C.yellow}${no}${C.reset}` +
      ` ${C.dim}│${C.reset}` +
      `${chgCol}${chg}${C.reset}`
    );
  }
  return lines;
}

function drawTradesPanel() {
  withAbsPos(() => {
    const innerW  = cols - 2;
    const xCol    = 2;
    const blank   = ' '.repeat(innerW);
    const visible = TRADES_CONTENT_END - TRADES_CONTENT_START + 1;

    // Clear content area
    for (let r = TRADES_CONTENT_START; r <= TRADES_CONTENT_END; r++) {
      process.stdout.write(`\x1B[${r};${xCol}H${blank}`);
    }

    const allLines = buildTradesPanelLines();
    const maxScroll = Math.max(0, allLines.length - visible);
    // Clamp scroll
    if (tradesPanelScroll > maxScroll) tradesPanelScroll = maxScroll;
    if (tradesPanelScroll < 0) tradesPanelScroll = 0;

    const visible_lines = allLines.slice(tradesPanelScroll, tradesPanelScroll + visible);
    let r = TRADES_CONTENT_START;
    for (const line of visible_lines) {
      process.stdout.write(`\x1B[${r};${xCol}H${line}`);
      r++;
    }

    // Scroll indicator (top-right corner of content area) when scrollable
    if (allLines.length > visible) {
      const pct  = Math.round(tradesPanelScroll / maxScroll * 100);
      const ind  = `${C.dim} ↕${pct}% ${C.reset}`;
      const indW = 6; // visible chars
      process.stdout.write(`\x1B[${TRADES_CONTENT_START};${cols - indW}H${ind}`);
    }
  });
}

// ── Red dots on map ───────────────────────────────────────────────────────────
function drawDots() {
  withAbsPos(() => {
    for (const { col, row } of incidents) {
      if (col > COL_MLB && col < COL_MRB && row >= mapTopLine && row <= mapBottomLine) {
        process.stdout.write(`\x1B[${row};${col}H${C.red}${C.bold}⬤${C.reset}`);
      }
    }
  });
}

// ── Event popup (shown on dot click) ─────────────────────────────────────────
const POPUP_W = 54;  // inner text width

function drawPopup() {
  if (!popup) return;
  withAbsPos(() => {
    const { inc, boxCol, boxRow } = popup;
    const W = POPUP_W;
    const bar  = '═'.repeat(W + 2);
    const thin = '─'.repeat(W + 2);

    const titleTag  = inc.event_type ? `[${inc.event_type}] ` : '';
    const titleLine = `${titleTag}${inc.ts}`;
    const bodyLines = wrapText(inc.headline || inc.summary, W);
    const metaLine  = `${inc.location}${inc.confidence ? `  ·  conf: ${inc.confidence}` : ''}`;

    const allLines = [titleLine, '', ...bodyLines, '', metaLine];
    const H = allLines.length + (inc.pmUrl ? 4 : 3);  // borders + optional URL row

    // Clamp so popup stays inside terminal
    const pc = Math.min(Math.max(boxCol - 2, COL_MLB + 1), cols - W - 5);
    let   pr = Math.min(Math.max(boxRow - Math.floor(H / 2), mapTopLine), mapBottomLine - H + 1);

    // Top border
    process.stdout.write(`\x1B[${pr};${pc}H${C.yellow}╔${bar}╗${C.reset}`);
    pr++;

    // Content lines
    for (const line of allLines) {
      const padded = line.slice(0, W).padEnd(W);
      const color  = line === titleLine ? `${C.yellow}${C.bold}` : C.reset;
      process.stdout.write(`\x1B[${pr};${pc}H${C.yellow}║${C.reset} ${color}${padded}${C.reset} ${C.yellow}║${C.reset}`);
      pr++;
    }

    // URL row (if available)
    if (inc.pmUrl) {
      process.stdout.write(`\x1B[${pr};${pc}H${C.yellow}╠${thin}╣${C.reset}`);
      pr++;
      const linkLabel = '  ⬡  POLYMARKET  →  ' + inc.pmUrl;
      // OSC 8 terminal hyperlink — works in iTerm2, Warp, etc.
      const hyperlink = `\x1B]8;;${inc.pmUrl}\x1B\\${C.cyan}${C.bold}${linkLabel.slice(0, W).padEnd(W)}${C.reset}\x1B]8;;\x1B\\`;
      process.stdout.write(`\x1B[${pr};${pc}H${C.yellow}║${C.reset} ${hyperlink} ${C.yellow}║${C.reset}`);
      pr++;
    }

    // Bottom: dismiss hint
    process.stdout.write(`\x1B[${pr};${pc}H${C.yellow}╠${thin}╣${C.reset}`);
    pr++;
    const hint = '  click anywhere to dismiss'.padEnd(W);
    process.stdout.write(`\x1B[${pr};${pc}H${C.yellow}║${C.reset}${C.dim}${hint}${C.reset} ${C.yellow}║${C.reset}`);
    pr++;
    process.stdout.write(`\x1B[${pr};${pc}H${C.yellow}╚${bar}╝${C.reset}`);
  });
}

function dismissPopup() {
  popup = null;
  // Repaint map area to erase the popup
  mapscii._draw();
  setImmediate(() => {
    drawBorders();
    drawLeftPanel();
    drawRightPanel();
    drawTradesPanel();
    drawDots();
    drawStatusBar();
  });
}

function handleMapClick(cx, cy) {
  // If popup is open, any click dismisses it
  if (popup) { dismissPopup(); return; }

  // Find nearest incident dot within 2-cell radius
  let best = null, bestDist = 3;
  for (const inc of incidents) {
    const d = Math.abs(inc.col - cx) + Math.abs(inc.row - cy);
    if (d < bestDist && inc.col > COL_MLB && inc.col < COL_MRB &&
        inc.row >= mapTopLine && inc.row <= mapBottomLine) {
      best = inc; bestDist = d;
    }
  }
  if (best) {
    popup = { inc: best, boxCol: cx, boxRow: cy };
    drawPopup();
  }
}

// ── MapSCII config ────────────────────────────────────────────────────────────
const path = require('path');
const mapsciiRoot = path.join(__dirname, 'node_modules', 'mapscii', 'src');
const mapConfig = require(path.join(mapsciiRoot, 'config'));
mapConfig.delimeter = `\n\r\x1B[${leftOffset}C`;
mapConfig.zoomStep  = 0.05;

// ── Location resolver ─────────────────────────────────────────────────────────
const CITIES = {
  'jerusalem': [31.768, 35.214],    'tel aviv': [32.085, 34.781],
  'haifa': [32.794, 34.989],        'gaza': [31.354, 34.308],
  'ramallah': [31.899, 35.206],     'eilat': [29.558, 34.952],
  'beirut': [33.888, 35.495],       'sidon': [33.563, 35.371],
  'damascus': [33.510, 36.292],     'aleppo': [36.202, 37.160],
  'homs': [34.736, 36.709],         'raqqa': [35.953, 39.003],
  'baghdad': [33.341, 44.401],      'basra': [30.508, 47.783],
  'mosul': [36.340, 43.130],        'erbil': [36.191, 44.009],
  'tehran': [35.696, 51.423],       'isfahan': [32.661, 51.680],
  'natanz': [33.724, 51.726],       'tabriz': [38.080, 46.291],
  'bushehr': [28.968, 50.838],      'fordow': [34.882, 50.568],
  'cairo': [30.044, 31.236],        'alexandria': [31.200, 29.919],
  'riyadh': [24.688, 46.722],       'jeddah': [21.543, 39.173],
  'dhahran': [26.260, 50.150],      'dubai': [25.204, 55.270],
  'abu dhabi': [24.453, 54.377],    'doha': [25.286, 51.533],
  'muscat': [23.588, 58.393],       'amman': [31.955, 35.945],
  'ankara': [39.921, 32.854],       'istanbul': [41.013, 28.948],
  'izmir': [38.419, 27.129],        'sanaa': [15.369, 44.191],
  'aden': [12.775, 45.036],         'hudaydah': [14.798, 42.955],
  'kabul': [34.528, 69.172],        'islamabad': [33.738, 73.084],
  'karachi': [24.861, 67.010],      'lahore': [31.558, 74.352],
  'moscow': [55.751, 37.618],       'st. petersburg': [59.939, 30.316],
  'kyiv': [50.450, 30.523],         'kharkiv': [49.994, 36.231],
  'mariupol': [47.099, 37.543],     'zaporizhzhia': [47.838, 35.143],
  'london': [51.507, -0.128],       'paris': [48.857, 2.347],
  'berlin': [52.520, 13.405],       'brussels': [50.850, 4.352],
  'washington': [38.907, -77.037],  'new york': [40.713, -74.006],
  'pentagon': [38.871, -77.056],    'langley': [38.951, -77.146],
  'beijing': [39.905, 116.391],     'shanghai': [31.230, 121.474],
  'pyongyang': [39.019, 125.738],   'taipei': [25.032, 121.565],
  'mogadishu': [2.046, 45.342],     'nairobi': [-1.292, 36.822],
  'tripoli': [32.902, 13.180],      'khartoum': [15.500, 32.560],
  'addis ababa': [9.024, 38.747],
};

const COUNTRIES = {
  'israel': [31.768, 35.214],       'palestine': [31.952, 35.233],
  'iran': [32.427, 53.688],         'iraq': [33.224, 43.679],
  'syria': [34.802, 38.997],        'lebanon': [33.872, 35.862],
  'jordan': [30.586, 36.238],       'egypt': [26.820, 30.802],
  'saudi arabia': [23.886, 45.079], 'uae': [23.424, 53.848],
  'qatar': [25.355, 51.184],        'bahrain': [26.067, 50.558],
  'kuwait': [29.378, 47.990],       'oman': [21.513, 55.923],
  'yemen': [15.552, 48.516],        'turkey': [38.964, 35.243],
  'russia': [61.524, 105.319],      'ukraine': [48.379, 31.165],
  'united states': [37.090, -95.713], 'usa': [37.090, -95.713],
  'china': [35.861, 104.196],       'taiwan': [23.698, 120.961],
  'north korea': [40.339, 127.510], 'south korea': [35.908, 127.767],
  'pakistan': [30.376, 69.345],     'afghanistan': [33.934, 67.710],
  'india': [20.594, 78.962],        'libya': [26.335, 17.229],
  'sudan': [12.863, 30.218],        'somalia': [5.152, 46.200],
  'ethiopia': [9.145, 40.490],      'kenya': [-0.023, 37.906],
  'nigeria': [9.082, 8.676],        'france': [46.227, 2.213],
  'germany': [51.166, 10.452],      'uk': [55.378, -3.436],
  'united kingdom': [55.378, -3.436],
};

function resolveLocation(loc) {
  if (!loc) return null;
  // Handle both string (Python pipeline) and object (legacy) location formats
  const candidates = typeof loc === 'string'
    ? [loc]
    : [loc.name, loc.region, loc.country].filter(Boolean);
  for (const s of candidates) {
    const key = s.toLowerCase();
    for (const [k, v] of Object.entries(CITIES)) {
      if (key.includes(k)) return { lat: v[0], lon: v[1] };
    }
    for (const [k, v] of Object.entries(COUNTRIES)) {
      if (key.includes(k)) return { lat: v[0], lon: v[1] };
    }
  }
  return null;
}

// ── Geo → terminal cell (Mercator, matches MapSCII tile math) ─────────────────
function latLonToTermPos(lat, lon) {
  const zoom   = (mapscii && mapscii.zoom)   ? mapscii.zoom   : 2.59;
  const center = (mapscii && mapscii.center) ? mapscii.center : { lat: 28.581, lon: 38.182 };
  const world  = 256 * Math.pow(2, zoom);

  const xOf = l => (l + 180) / 360 * world;
  const yOf = l => {
    const s = Math.sin(l * Math.PI / 180);
    return (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * world;
  };

  const cx = canvasWidth  / 2 + (xOf(lon) - xOf(center.lon));
  const cy = canvasHeight / 2 + (yOf(lat) - yOf(center.lat));

  return {
    col: Math.floor(cx / 2) + leftOffset + 1,
    row: Math.floor(cy / 4) + mapTopLine,
  };
}

// ── Initial screen setup ──────────────────────────────────────────────────────
process.stdout.write('\x1B[?25l\x1B[2J\x1B[H');
drawBorders();
drawStatusBar();
setInterval(drawDateTime, 1000);
process.stdout.write(`\x1B[${mapTopLine};${mapBottomLine}r`);

// ── Transform stream: strip clear-screen, shift map right, redraw UI ──────────
const mapStream = new Transform({
  transform(chunk, encoding, callback) {
    let data = chunk.toString();
    data = data.replace(/\x1B\[2J/g, '');
    data = data.replace(/\x1B\[\?6h/g, `\x1B[?6h\x1B[H\x1B[${leftOffset}C`);
    callback(null, data);
    if (data.includes('\x1B[?6h')) {
      setImmediate(() => {
        drawBorders();
        drawLeftPanel();
        drawRightPanel();
        drawTradesPanel();
        drawDots();
      });
    }
  },
});
mapStream.pipe(process.stdout);

// ── MapSCII init ──────────────────────────────────────────────────────────────
const Mapscii = require(path.join(mapsciiRoot, 'Mapscii'));

const _origInit = Mapscii.prototype.init;
Mapscii.prototype.init = async function () {
  this.center = { lat: 28.581, lon: 38.182 };
  return _origInit.call(this);
};
Mapscii.prototype._getFooter = function () { return ''; };
Mapscii.prototype.notify    = function () {};

const mapscii = new Mapscii({
  initialZoom: 2.59,
  output: mapStream,
  size: { width: canvasWidth, height: canvasHeight },
  headless: true,
});

// ── Zoom helper ───────────────────────────────────────────────────────────────
function zoomMap(direction) {
  // direction: +1 = zoom in, -1 = zoom out
  if (!mapscii) return;
  mapscii.zoomBy(direction * mapConfig.zoomStep);
  mapscii._draw();
  // Recalculate dot positions from stored lat/lon
  for (const inc of incidents) {
    const pos = latLonToTermPos(inc.lat, inc.lon);
    inc.col = pos.col;
    inc.row = pos.row;
  }
}

// ── Pan helper ────────────────────────────────────────────────────────────────
function panMap(dLat, dLon) {
  if (!mapscii) return;
  const step = 20 / Math.pow(2, mapscii.zoom);
  mapscii.center.lat = Math.max(-85, Math.min(85, mapscii.center.lat + dLat * step));
  mapscii.center.lon = ((mapscii.center.lon + dLon * step) + 540) % 360 - 180;
  mapscii._draw();
  for (const inc of incidents) {
    const pos = latLonToTermPos(inc.lat, inc.lon);
    inc.col = pos.col;
    inc.row = pos.row;
  }
}

// ── Keyboard input ────────────────────────────────────────────────────────────
function handleKey(buf) {
  const b = Buffer.isBuffer(buf) ? buf : Buffer.from(buf);
  if (b[0] === 0x03 || b[0] === 0x78 || b[0] === 0x58) cleanup();

  // + or = → zoom in; - → zoom out
  if (b[0] === 0x2B || b[0] === 0x3D) { zoomMap(+1); return; }
  if (b[0] === 0x2D)                  { zoomMap(-1); return; }

  // Arrow keys → pan map
  const ks = b.toString();
  if (ks === '\x1B[A') { panMap(+1,  0); return; }  // up
  if (ks === '\x1B[B') { panMap(-1,  0); return; }  // down
  if (ks === '\x1B[C') { panMap( 0, +1); return; }  // right
  if (ks === '\x1B[D') { panMap( 0, -1); return; }  // left

  // SGR mouse events: \x1B[<Btn;Col;RowM (press) or m (release)
  const s = b.toString();
  const mouseMatch = s.match(/^\x1B\[<(\d+);(\d+);(\d+)([Mm])$/);
  if (mouseMatch) {
    const btn     = parseInt(mouseMatch[1], 10);
    const mx      = parseInt(mouseMatch[2], 10);
    const my      = parseInt(mouseMatch[3], 10);
    const isPress = mouseMatch[4] === 'M';

    // Route scroll to bottom panel when cursor is hovering there
    const inBottomPanel = my >= TRADES_TITLE_ROW && my <= TRADES_CONTENT_END;
    if (btn === 64) {
      if (inBottomPanel) { tradesPanelScroll = Math.max(0, tradesPanelScroll - 1); drawTradesPanel(); }
      else { zoomMap(+1); }
      return;
    }
    if (btn === 65) {
      if (inBottomPanel) { tradesPanelScroll++; drawTradesPanel(); }
      else { zoomMap(-1); }
      return;
    }
    if (btn === 0 && isPress) {
      // Check if click is in the left panel (telegram expand/collapse)
      if (mx >= 2 && mx <= LEFT_W + 1 && leftPanelRowMap[my] !== undefined) {
        const idx = leftPanelRowMap[my];
        if (telegramMsgs[idx]) {
          telegramMsgs[idx].expanded = !telegramMsgs[idx].expanded;
          drawLeftPanel();
          drawBorders();
        }
        return;
      }
      handleMapClick(mx, my);
      return;
    }
  }
}

function claimStdin() {
  if (!process.stdin.isTTY) return;
  process.stdin.removeAllListeners('data');
  process.stdin.setRawMode(true);
  process.stdin.resume();
  // Enable SGR mouse reporting for scroll events
  process.stdout.write('\x1B[?1000h\x1B[?1006h');
  process.stdin.on('data', handleKey);
}

claimStdin();

mapscii.init().then(() => {
  claimStdin();
  setTimeout(() => {
    drawBorders();
    drawLeftPanel();
    drawRightPanel();
    drawTradesPanel();
    drawStatusBar();
  }, 150);
}).catch(err => {
  tuiLog('[!] MapSCII failed: ' + err);
});

// ── Public API ────────────────────────────────────────────────────────────────

function processEvent(eventData) {
  const ev = typeof eventData === 'string' ? JSON.parse(eventData) : eventData;
  const coords = resolveLocation(ev.location);

  if (!coords) {
    tuiLog(`[!] cannot resolve location: ${typeof ev.location === 'string' ? ev.location : JSON.stringify(ev.location)}`);
    return false;
  }

  const pos = latLonToTermPos(coords.lat, coords.lon);

  // Handle both Python string location and legacy object location
  const locName = typeof ev.location === 'string'
    ? ev.location
    : (ev.location?.name || ev.location?.country || 'Unknown');

  const ts = ev.timestamp
    ? new Date(ev.timestamp).toISOString().slice(0, 16).replace('T', ' ') + 'Z'
    : '';

  // Python pipeline sends headline + summary; use headline as title if present
  const headline = ev.headline || '';
  const summary  = ev.summary || `Event ${ev.event_id}`;
  const display  = headline || summary;

  feedLines.push({t:'sep',  text:'─'.repeat(LEFT_W)});
  if (ts) feedLines.push({t:'meta', text:`${ts}  ${locName}`});
  if (ev.event_type) feedLines.push({t:'tag',  text:`[${(ev.event_type||'').toUpperCase()}]  conf:${ev.confidence||'?'}`});
  if (headline)      feedLines.push({t:'h1',   text:headline});
  for (const line of wrapText(headline ? summary : display, LEFT_W - 3)) feedLines.push({t:'body', text:line});
  while (feedLines.length > 400) feedLines.shift();

  totalEvents++;
  // Track country from either format
  const country = typeof ev.location === 'string' ? null : ev.location?.country;
  if (country) countrySeen.add(country);
  // Also track countries from the involved array (Python pipeline)
  if (Array.isArray(ev.involved)) ev.involved.forEach(c => countrySeen.add(c));
  lastEventInfo = ts ? `${locName} · ${ts}` : locName;

  // Capture Claude's AI trade rankings if present
  const pt = ev.polymarket_trades;
  if (pt && ((pt.primary && pt.primary.length) || (pt.secondary && pt.secondary.length))) {
    latestClaudeTrades = {
      headline:  headline || locName,
      primary:   pt.primary   || [],
      secondary: pt.secondary || [],
    };
  }

  // Store full event data so dot can show popup on click
  const pmUrl = pt?.primary?.[0]?.url || pt?.secondary?.[0]?.url || '';
  incidents.push({
    lat: coords.lat, lon: coords.lon, col: pos.col, row: pos.row,
    headline, summary, event_type: (ev.event_type || '').toUpperCase(),
    confidence: ev.confidence || '', location: locName, ts, pmUrl,
  });

  drawDots();
  drawLeftPanel();
  drawRightPanel();
  drawTradesPanel();
  drawBorders();
  return true;
}

function testEvent() {
  processEvent({
    event_id: '71862778',
    timestamp: '2026-02-27T00:04:00Z',
    confirmed: true,
    event_type: 'other',
    summary: "U.S. State Department authorized departure of non-emergency government personnel and family members from Mission Israel due to safety risks, with Ambassador Huckabee urging staff to leave 'TODAY'. Taken amid heightened US-Iran tensions and a massive US military buildup including the USS Gerald R. Ford carrier heading to Israeli coast, one day before joint US-Israeli strikes on Iran.",
    location: {
      name: 'U.S. Embassy Jerusalem / Mission Israel',
      country: 'Israel',
      region: 'Jerusalem District',
      facility_type: 'other',
      precision: 'high',
    },
    groups_involved: [],
    weapons_used: [],
    casualties: { killed: null, injured: null, confidence: 'unknown' },
  });
}

// ── TUI logging ───────────────────────────────────────────────────────────────
function tuiLog(msg) {
  for (const raw of msg.split('\n')) {
    const text = raw.trimEnd();
    if (!text) continue;

    // Classify
    let t = 'sys';
    if      (/^\s*[✓✔]/.test(text))                  t = 'ok';
    else if (/^\s*\[!]/.test(text))                   t = 'warn';
    else if (/^\s*\+\s+@|\d{2}:\d{2}\s+@/.test(text)) t = 'msg';
    else if (/^\s*[─═]{4}/.test(text))                t = 'sep';

    // Suppress pipeline chatter — only keep AI event markers and "All systems running."
    // AI event markers written by processEvent (sep/meta/tag/h1/body) bypass tuiLog entirely.
    // tuiLog receives system/pipeline messages; we only want "All systems running." from those.
    if (['sys', 'ok', 'warn', 'msg'].includes(t)) {
      if (!/all systems running/i.test(text)) continue;  // suppress everything else
    }

    feedLines.push({t, text});
  }
  while (feedLines.length > 400) feedLines.shift();
  drawLeftPanel();
  drawBorders();
}

// ── Polymarket data (pushed from Python pipeline via /markets endpoint) ───────
//
// Python's polymarket.py already fetches and keyword-filters the right markets.
// strata.js receives them via POST /markets and classifies by category for display.

function classifyMarket(question) {
  const q = question.toLowerCase();
  if (/oil|opec|crude|brent|sanction|strait|energy|barrel/.test(q)) return 'ECONOMIC';
  if (/nuclear|deal|peace|normaliz|hostage|negotiat|diplomac|arms deal|accord/.test(q)) return 'DIPLOMATIC';
  return 'MILITARY';
}

function ingestMarkets(rawMarkets) {
  const newMarkets = rawMarkets.slice(0, TRADES_H - 1).map(m => {
    const id  = m.condition_id || m.question || '';
    const yp  = parseFloat((m.prices || [])[0] || '0');
    const np  = parseFloat((m.prices || [])[1] || '0');
    const prev = polyState.prevPrices[id];
    const change = (prev !== undefined) ? yp - prev : null;
    polyState.prevPrices[id] = yp;
    return {
      id,
      question: (m.question || '').replace(/\?$/, ''),
      category: classifyMarket(m.question || ''),
      yesPrice: yp,
      noPrice:  np,
      change,
    };
  });

  // Sort: MILITARY first, then ECONOMIC, then DIPLOMATIC
  const order = { MILITARY: 0, ECONOMIC: 1, DIPLOMATIC: 2 };
  newMarkets.sort((a, b) => order[a.category] - order[b.category]);
  polyState.markets = newMarkets;
  drawTradesPanel();
  drawBorders();
}

// ── Executed trades (pushed from Python trade_executor.py via /trades) ────────
function ingestTrades(rawTrades) {
  if (!Array.isArray(rawTrades) || rawTrades.length === 0) return;
  // Merge by timestamp+market key to avoid duplicates on restart
  const existing = new Set(executedTrades.map(t => `${t.timestamp}|${t.market}`));
  for (const t of rawTrades) {
    const key = `${t.timestamp}|${t.market}`;
    if (!existing.has(key)) {
      executedTrades.push(t);
      existing.add(key);
    }
  }
  // Cap at 200 most recent
  if (executedTrades.length > 200) executedTrades.splice(0, executedTrades.length - 200);
  drawRightPanel();
  drawBorders();
}

// ── HTTP server ────────────────────────────────────────────────────────────────
http.createServer((req, res) => {
  if (req.method !== 'POST') {
    res.writeHead(200); res.end('STRATA is running\n'); return;
  }
  let body = '';
  req.on('data', d => { body += d; });
  req.on('end', () => {
    // /markets — receive Python's filtered conflict markets
    if (req.url === '/markets') {
      try {
        const data = JSON.parse(body);
        const markets = Array.isArray(data) ? data : [data];
        ingestMarkets(markets);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, count: polyState.markets.length }));
      } catch (e) {
        res.writeHead(400); res.end('Bad JSON\n');
      }
      return;
    }

    // /trades — receive executed trade results from Python trade_executor.py
    if (req.url === '/trades') {
      try {
        const data   = JSON.parse(body);
        const trades = Array.isArray(data) ? data : [data];
        ingestTrades(trades);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, count: executedTrades.length }));
      } catch (e) {
        res.writeHead(400); res.end('Bad JSON\n');
      }
      return;
    }

    // /log — route a text message to the left feed panel
    if (req.url === '/log') {
      try {
        const data = JSON.parse(body);
        tuiLog(typeof data.msg === 'string' ? data.msg : JSON.stringify(data));
      } catch (_) {
        tuiLog(body.trim());
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
      return;
    }

    // /telegram — structured Telegram message (ts, channel, text, mediaPath, mediaType)
    if (req.url === '/telegram') {
      try {
        const d = JSON.parse(body);
        telegramMsgs.push({
          ts:        d.ts        || '',
          channel:   d.channel   || '',
          text:      d.text      || '',
          mediaPath: d.media_path || null,
          mediaType: d.media_type || null,
          expanded:  false,
        });
        while (telegramMsgs.length > 200) telegramMsgs.shift();
        drawLeftPanel();
        drawBorders();
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, count: telegramMsgs.length }));
      } catch (e) {
        res.writeHead(400); res.end('Bad JSON\n');
      }
      return;
    }

    // Default — process conflict events
    try {
      const data   = JSON.parse(body);
      const events = Array.isArray(data) ? data : [data];
      const ok     = events.filter(e => processEvent(e)).length;
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ processed: ok, total: events.length }));
    } catch (e) {
      res.writeHead(400); res.end('Bad JSON\n');
    }
  });
}).listen(PORT, () => {
  setTimeout(() => tuiLog('  All systems running.'), 300);
});


module.exports = { processEvent, testEvent };
