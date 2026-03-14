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
const incidents   = [];
const feedLines   = [];
let   totalEvents = 0;
const countrySeen = new Set();
let   lastEventInfo = '';

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
    let r = r0;
    for (let i = Math.max(0, feedLines.length - visible); i < feedLines.length && r <= r1; i++) {
      const line  = feedLines[i];
      const color = line.startsWith('[') ? C.gray : C.yellow;
      process.stdout.write(`\x1B[${r};${xCol}H${color}${line.slice(0, W).padEnd(W)}${C.reset}`);
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

    r++;
    put('─'.repeat(W), C.dim);
    r++;
    put(' Total Events', C.dim);
    put(`  ${totalEvents}`, `${C.yellow}${C.bold}`);
    r++;
    put(' Countries', C.dim);
    put(`  ${countrySeen.size}`, `${C.yellow}${C.bold}`);
    r++;

    const level      = totalEvents === 0 ? 'LOW' : totalEvents < 5 ? 'MEDIUM' : 'HIGH';
    const levelColor = level === 'LOW' ? C.green : level === 'MEDIUM' ? C.yellow : C.red;
    put(' Alert Level', C.dim);
    put(`  ● ${level}`, `${levelColor}${C.bold}`);
    r++;

    if (lastEventInfo) {
      put(' Last Event', C.dim);
      for (const l of wrapText(lastEventInfo, W - 3)) {
        put(`  ${l}`, C.gray);
      }
    }
  });
}

// ── Trades / Polymarket panel ─────────────────────────────────────────────────
function drawTradesPanel() {
  withAbsPos(() => {
    const innerW = cols - 2;
    const xCol   = 2;
    const blank  = ' '.repeat(innerW);

    for (let r = TRADES_CONTENT_START; r <= TRADES_CONTENT_END; r++) {
      process.stdout.write(`\x1B[${r};${xCol}H${blank}`);
    }

    let r = TRADES_CONTENT_START;

    if (polyState.markets.length === 0) {
      process.stdout.write(
        `\x1B[${r};${xCol}H${C.dim}  Fetching Polymarket data...${C.reset}`
      );
      return;
    }

    // Column widths
    const CAT_W  = 11;
    const YES_W  = 6;
    const NO_W   = 6;
    const CHG_W  = 7;
    // layout: "  [CAT] │ [MKT] │ [YES] │ [NO] │ [CHG]"
    const FIXED  = 2 + CAT_W + 3 + 3 + YES_W + 3 + NO_W + 3 + CHG_W;
    const MKT_W  = Math.max(8, innerW - FIXED);

    // Header row
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
    process.stdout.write(
      `\x1B[${r};${xCol}H${C.dim}${hdr.slice(0, innerW)}${C.reset}`
    );
    r++;

    for (const m of polyState.markets) {
      if (r > TRADES_CONTENT_END) break;

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

      process.stdout.write(
        `\x1B[${r};${xCol}H` +
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
      r++;
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

  // Mouse scroll (SGR: \x1B[<Pb;Px;PyM  — button 64=scroll-up, 65=scroll-down)
  const s = b.toString();
  const scrollMatch = s.match(/^\x1B\[<(\d+);\d+;\d+M$/);
  if (scrollMatch) {
    const btn = parseInt(scrollMatch[1], 10);
    if (btn === 64) { zoomMap(+1); return; }
    if (btn === 65) { zoomMap(-1); return; }
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
  setTimeout(testEvent, 1200);
  // Start Polymarket polling after UI is ready
  setTimeout(pollPolymarkets, 2000);
}).catch(err => {
  process.stderr.write('MapSCII failed: ' + err + '\n');
});

// ── Public API ────────────────────────────────────────────────────────────────

function processEvent(eventData) {
  const ev = typeof eventData === 'string' ? JSON.parse(eventData) : eventData;
  const coords = resolveLocation(ev.location);

  if (!coords) {
    process.stderr.write(`[STRATA] Cannot resolve location: ${JSON.stringify(ev.location)}\n`);
    return false;
  }

  const pos = latLonToTermPos(coords.lat, coords.lon);
  incidents.push({ lat: coords.lat, lon: coords.lon, col: pos.col, row: pos.row });

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

  if (ts) feedLines.push(`[${ts}] ${locName}`);
  if (headline) feedLines.push(`${C.bold}${headline.slice(0, LEFT_W - 1)}${C.reset}`);
  for (const line of wrapText(headline ? summary : display, LEFT_W - 1)) feedLines.push(line);
  feedLines.push('');
  while (feedLines.length > 300) feedLines.shift();

  totalEvents++;
  // Track country from either format
  const country = typeof ev.location === 'string' ? null : ev.location?.country;
  if (country) countrySeen.add(country);
  // Also track countries from the involved array (Python pipeline)
  if (Array.isArray(ev.involved)) ev.involved.forEach(c => countrySeen.add(c));
  lastEventInfo = ts ? `${locName} · ${ts}` : locName;

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
  feedLines.push(`${C.dim}${msg}${C.reset}`);
  while (feedLines.length > 300) feedLines.shift();
  drawLeftPanel();
  drawBorders();
}

// ── Polymarket API ────────────────────────────────────────────────────────────
//
// Fetches the top active Middle East markets in three categories:
//   MILITARY   – conflict escalation, strikes, ceasefires
//   ECONOMIC   – oil, sanctions, OPEC
//   DIPLOMATIC – nuclear deals, normalization, peace
//
const POLY_QUERIES = [
  // MILITARY
  { term: 'Israel attack Iran',          category: 'MILITARY'   },
  { term: 'Gaza ceasefire 2026',         category: 'MILITARY'   },
  { term: 'Houthi Yemen attack',         category: 'MILITARY'   },
  { term: 'Lebanon Hezbollah war',       category: 'MILITARY'   },
  // ECONOMIC
  { term: 'oil price $100 Middle East',  category: 'ECONOMIC'   },
  { term: 'Iran oil sanctions OPEC',     category: 'ECONOMIC'   },
  { term: 'Saudi Arabia OPEC cut',       category: 'ECONOMIC'   },
  // DIPLOMATIC
  { term: 'Iran nuclear deal agreement', category: 'DIPLOMATIC' },
  { term: 'Saudi Arabia Israel peace',   category: 'DIPLOMATIC' },
  { term: 'Iran US negotiations 2026',   category: 'DIPLOMATIC' },
];

// Max markets displayed per category (keeps panel from overflowing)
const MAX_PER_CAT = 2;

async function refreshPolymarkets() {
  const seen      = new Set();
  const byCat     = { MILITARY: [], ECONOMIC: [], DIPLOMATIC: [] };

  for (const { term, category } of POLY_QUERIES) {
    if (byCat[category].length >= MAX_PER_CAT) continue;

    try {
      const url = 'https://gamma-api.polymarket.com/markets?' +
        'active=true&closed=false&limit=5' +
        '&order=volume&ascending=false' +
        `&search=${encodeURIComponent(term)}`;
      const data = await httpsGet(url);
      if (!Array.isArray(data)) continue;

      for (const m of data) {
        if (seen.has(m.id)) continue;
        const prices = tryParseJSON(m.outcomePrices);
        if (!Array.isArray(prices) || prices.length < 2) continue;
        const yp = parseFloat(prices[0]);
        const np = parseFloat(prices[1]);
        if (isNaN(yp) || isNaN(np)) continue;

        seen.add(m.id);
        const prev = polyState.prevPrices[m.id];
        byCat[category].push({
          id:       m.id,
          question: (m.question || m.title || term).replace(/\?$/, ''),
          category,
          yesPrice: yp,
          noPrice:  np,
          change:   prev !== undefined ? yp - prev : null,
        });
        break; // one result per query term is enough
      }
    } catch (_) {
      // network / parse errors — silently skip
    }
  }

  // Update stored prices for change tracking next cycle
  for (const cat of Object.values(byCat)) {
    for (const m of cat) polyState.prevPrices[m.id] = m.yesPrice;
  }

  // Interleave categories: MIL, ECO, DIP, MIL, ECO, DIP …
  polyState.markets = [];
  const maxLen = Math.max(byCat.MILITARY.length, byCat.ECONOMIC.length, byCat.DIPLOMATIC.length);
  for (let i = 0; i < maxLen; i++) {
    if (byCat.MILITARY[i])   polyState.markets.push(byCat.MILITARY[i]);
    if (byCat.ECONOMIC[i])   polyState.markets.push(byCat.ECONOMIC[i]);
    if (byCat.DIPLOMATIC[i]) polyState.markets.push(byCat.DIPLOMATIC[i]);
  }

  drawTradesPanel();
  drawBorders();
}

function pollPolymarkets() {
  tuiLog('[POLY] Fetching Polymarket data…');
  refreshPolymarkets().catch(e => tuiLog(`[POLY] Error: ${e.message}`));
  setInterval(() => {
    refreshPolymarkets().catch(() => {});
  }, 60_000);
}

// ── HTTP server ────────────────────────────────────────────────────────────────
http.createServer((req, res) => {
  if (req.method !== 'POST') {
    res.writeHead(200); res.end('STRATA is running\n'); return;
  }
  let body = '';
  req.on('data', d => { body += d; });
  req.on('end', () => {
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
  setTimeout(() => tuiLog(`[HTTP] listening on :${PORT}`), 300);
});


module.exports = { processEvent, testEvent };
