/**
 * SMC Trading Dashboard — v4.0
 *
 * Changes over v3.1 (AMD v2 integration):
 *   1.  oscillator_data now sourced from API (real close-delta, not client-side proxy)
 *   2.  MSS level drawn on chart as a purple horizontal line
 *   3.  Divergence candle markers drawn on chart at divergence_indices positions
 *   4.  AMDPhaseCard shows MSS level price + divergence badge with index range
 *   5.  PDZ hard-gate now also catches "hard suppression" warning text from v4 engine
 *   6.  Version badge updated to v4.0
 *   7.  "Confluence" tab now groups AMD reasons separately with an AMD sub-header
 *   8.  computeRRTargets ratios updated to [1.5, 2.0, 3.0] to match min_rr=1.8
 *   9.  buildOscillatorData replaced: uses API data when available (preferred),
 *       falls back to client-side close-delta for mock mode
 *  10.  Chart MSS marker now uses mss_level (if set) instead of manip close
 */

import { Fragment, useEffect, useRef, useState, useCallback } from "react";
import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
} from "lightweight-charts";

const API = "http://localhost:8000";

const TF_OPTIONS      = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];
const COUNT_OPTIONS   = [100, 200, 300, 500];
const SYMBOL_OPTIONS  = [
  "EURUSD","GBPUSD","USDJPY","USDCHF","AUDUSD","NZDUSD","USDCAD",
  "GBPJPY","EURJPY","EURGBP","XAUUSD",
  "BTCUSD","USOIL","USTECH","ETHUSD","XAGUSD","US30","SPX500","UK100",
];

// ── Colour palette ─────────────────────────────────────────────────────────────
const C = {
  bg:        "#0a0c10",
  surface:   "#111318",
  surface2:  "#161a22",
  border:    "#1d2231",
  border2:   "#252c3a",
  muted:     "#5a6480",
  muted2:    "#7a84a0",
  text:      "#dde3f0",
  textDim:   "#8d97b0",
  green:     "#20d68a",
  greenDim:  "#0d3d27",
  red:       "#f0476a",
  redDim:    "#4a1020",
  amber:     "#f5a623",
  amberDim:  "#4a2e08",
  blue:      "#4a9eff",
  blueDim:   "#0d2540",
  purple:    "#9b6dff",
  purpleDim: "#221040",
  teal:      "#0fd4c0",
  tealDim:   "#042e2a",
  orange:    "#ff7c42",
  orangeDim: "#401a08",
  fvgBull:   "rgba(32,214,138,0.50)",
  fvgBear:   "rgba(240,71,106,0.50)",
  obBull:    "rgba(15,212,192,0.55)",
  obBear:    "rgba(255,124,66,0.55)",
  ote:       "rgba(155,109,255,0.55)",
};

const AMD_COLORS = {
  ACCUMULATION: { bg: "#0f1f18", border: C.greenDim, text: C.green, label: "ACCUM" },
  MANIPULATION: { bg: "#241906", border: C.amberDim, text: C.amber, label: "MANIP" },
  DISTRIBUTION: { bg: "#0a1a2a", border: C.blueDim, text: C.blue, label: "DIST" },
  UNKNOWN:      { bg: C.surface, border: C.border, text: C.muted2, label: "UNKNOWN" },
};

const SETUP_COLORS = {
  LH_SELL:          C.red,
  HL_BUY:           C.green,
  CHOCH_REVERSAL:   C.purple,
  BOS_CONTINUATION: C.blue,
};

const AMD_BOX = {
  accumulation: {
    fill: "rgba(245,166,35,0.13)",
    stroke: "rgba(245,166,35,0.55)",
    label: "ACCUM",
    labelColor: C.amber,
  },
  manipulation: {
    fill: "rgba(240,71,106,0.12)",
    stroke: "rgba(240,71,106,0.50)",
    label: "MANIP",
    labelColor: C.red,
  },
  distribution: {
    fill: "rgba(32,214,138,0.12)",
    stroke: "rgba(32,214,138,0.50)",
    label: "DIST",
    labelColor: C.green,
  },
};

// ── Helpers ────────────────────────────────────────────────────────────────────
const fmt    = (n, d = 5) => (n == null || n === 0 ? "—" : Number(n).toFixed(d));
const fmtPct = (n)        => (n == null ? "—" : Number(n).toFixed(1) + "%");
const pipSize = (sym) => {
  const s = (sym || "").toUpperCase();
  if (s.includes("JPY") || s.includes("XAU") || s.includes("GOLD")
    || s.includes("XAG") || s.includes("OIL") || s.includes("WTI")) return 0.01;
  if (s.includes("BTC") || ["US30", "USTECH", "NAS100", "SPX500", "UK100"].includes(s)) return 1;
  if (s.includes("ETH")) return 0.01;
  return 0.0001;
};
const pipDiff = (a, b, sym) => {
  if (a == null || b == null || a === 0 || b === 0) return "—";
  return (Math.abs(a - b) / pipSize(sym)).toFixed(1) + "p";
};
const priceDecimals = (sym) => {
  const s = (sym || "").toUpperCase();
  if (s.includes("XAU") || s.includes("GOLD") || s.includes("XAG")
    || s.includes("OIL") || s.includes("WTI") || s.includes("BTC") || s.includes("ETH")) return 2;
  if (["US30", "USTECH", "NAS100", "SPX500", "UK100"].includes(s)) return 2;
  if (s.includes("JPY")) return 3;
  return 5;
};
const fmtPrice = (n, sym) => fmt(n, priceDecimals(sym));
const clk = () => new Date().toLocaleTimeString();

function manualTradeLevels(direction, data) {
  if (!data?.candles?.length) return null;
  const entry = Number(data.current_price);
  if (!Number.isFinite(entry) || entry <= 0) return null;

  const minimumRR = Math.max(Number(data.rr_ratio) || 0, 1.8);
  const buffer = pipSize(data.symbol);
  const ensureRiskReward = (stopLoss, takeProfit) => {
    const risk = Math.abs(entry - stopLoss) + buffer;
    const reward = Math.abs(takeProfit - entry);
    const adjustedReward = Math.max(reward, risk * minimumRR * 1.02);
    return direction === "BUY"
      ? { entry, stopLoss, takeProfit: entry + adjustedReward }
      : { entry, stopLoss, takeProfit: entry - adjustedReward };
  };

  const stopLoss = Number(data.stop_loss);
  const takeProfit = Number(data.take_profit);
  const signalLevelsValid = data.signal === direction && (direction === "BUY"
    ? stopLoss < entry && entry < takeProfit
    : takeProfit < entry && entry < stopLoss);
  if (signalLevelsValid) return ensureRiskReward(stopLoss, takeProfit);

  const candles = data.candles;
  const ranges = candles.slice(1).map((candle, index) => {
    const previousClose = candles[index].close;
    return Math.max(
      candle.high - candle.low,
      Math.abs(candle.high - previousClose),
      Math.abs(candle.low - previousClose),
    );
  });
  const recentRanges = ranges.slice(-14);
  const atr = recentRanges.length
    ? recentRanges.reduce((sum, value) => sum + value, 0) / recentRanges.length
    : 0;
  if (!Number.isFinite(atr) || atr <= 0) return null;

  const riskDistance = atr * 1.5;
  const fallbackStop = direction === "BUY" ? entry - riskDistance : entry + riskDistance;
  const fallbackTarget = direction === "BUY" ? entry + riskDistance : entry - riskDistance;
  return ensureRiskReward(fallbackStop, fallbackTarget);
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function Badge({ label, color = "gray", small }) {
  const cols = {
    green:  { bg: C.greenDim,  fg: C.green },
    red:    { bg: C.redDim,    fg: C.red },
    amber:  { bg: C.amberDim,  fg: C.amber },
    blue:   { bg: C.blueDim,   fg: C.blue },
    purple: { bg: C.purpleDim, fg: C.purple },
    teal:   { bg: C.tealDim,   fg: C.teal },
    orange: { bg: C.orangeDim, fg: C.orange },
    gray:   { bg: C.surface2,  fg: C.muted2 },
  };
  const s = cols[color] || cols.gray;
  return (
    <span style={{
      background: s.bg, color: s.fg,
      padding: small ? "1px 7px" : "2px 10px",
      borderRadius: 99, fontSize: small ? 10 : 11,
      fontWeight: 700, letterSpacing: 0.4, display: "inline-block",
    }}>{label}</span>
  );
}

function Stat({ label, value, color, sub, mono = true }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      <span style={{ fontSize: 10, color: C.muted, textTransform: "uppercase", letterSpacing: 1 }}>{label}</span>
      <span style={{
        fontSize: 14, fontWeight: 700,
        color: color || C.text,
        fontFamily: mono ? "'JetBrains Mono','Fira Code',monospace" : "inherit",
      }}>{value}</span>
      {sub && <span style={{ fontSize: 10, color: C.muted2 }}>{sub}</span>}
    </div>
  );
}

function SecTitle({ children, right }) {
  return (
    <div style={{
      display: "flex", justifyContent: "space-between", alignItems: "center",
      fontSize: 10, color: C.muted, textTransform: "uppercase", letterSpacing: 1,
      marginBottom: 12, borderBottom: `1px solid ${C.border}`, paddingBottom: 6,
    }}>
      <span>{children}</span>
      {right && <span style={{ color: C.muted2, fontSize: 10 }}>{right}</span>}
    </div>
  );
}

function Bar({ value, max = 100, color }) {
  const pct = Math.min(Math.max((value / max) * 100, 0), 100);
  return (
    <div style={{ width: "100%", height: 4, background: C.border2, borderRadius: 99, overflow: "hidden" }}>
      <div style={{ width: `${pct}%`, height: "100%", borderRadius: 99, background: color || C.blue, transition: "width .4s" }} />
    </div>
  );
}

function CheckRow({ label, passed, detail }) {
  return (
    <div style={{
      display: "flex", alignItems: "flex-start", gap: 8, fontSize: 11,
      padding: "5px 0", borderBottom: `1px solid ${C.border}`,
    }}>
      <span style={{
        minWidth: 16, fontWeight: 800, fontSize: 13, lineHeight: "16px",
        color: passed ? C.green : C.red,
      }}>{passed ? "✓" : "✗"}</span>
      <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
        <span style={{ color: passed ? C.text : C.muted2, fontWeight: passed ? 600 : 400 }}>{label}</span>
        {detail && <span style={{ fontSize: 10, color: C.muted, fontFamily: "monospace" }}>{detail}</span>}
      </div>
    </div>
  );
}

function SignalGlyph({ signal }) {
  const cfg = {
    BUY:  { symbol: "BUY",  color: C.green,  glow: "0 0 24px rgba(32,214,138,0.5)" },
    SELL: { symbol: "SELL", color: C.red,    glow: "0 0 24px rgba(240,71,106,0.5)" },
    WAIT: { symbol: "WAIT", color: C.amber,  glow: "0 0 12px rgba(245,166,35,0.3)" },
  };
  const c = cfg[signal] || cfg.WAIT;
  return (
    <div style={{
      fontSize: 36, fontWeight: 900, lineHeight: 1,
      color: c.color, textShadow: c.glow, letterSpacing: -1,
    }}>{c.symbol}</div>
  );
}

function AMDPhaseCard({ amd, symbol }) {
  if (!amd) return null;
  const cfg = AMD_COLORS[amd.phase] || AMD_COLORS.UNKNOWN;
  const biasColor = amd.bias === "bearish" ? "red" : amd.bias === "bullish" ? "green" : "gray";

  return (
    <div style={{ ...cardSt, background: cfg.bg, borderColor: cfg.border }}>
      <SecTitle right={amd.phase_confidence != null ? `${(amd.phase_confidence * 100).toFixed(0)}% conf` : null}>
        AMD Phase
      </SecTitle>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 12 }}>
        <div style={{ fontSize: 24, fontWeight: 900, fontFamily: "monospace", color: cfg.text }}>
          {cfg.label}
        </div>
        {amd.bias && <Badge label={amd.bias.toUpperCase()} color={biasColor} small />}
        {amd.distribution_started && <Badge label="DIST LIVE" color="green" small />}
        {amd.mss_detected && <Badge label="MSS" color="purple" small />}
        {amd.divergence_detected && <Badge label="DIVERGENCE" color="red" small />}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
        <Stat
          label="Accum Low"
          value={fmtPrice(amd.accumulation_range?.low, symbol)}
          color={C.green}
        />
        <Stat
          label="Accum High"
          value={fmtPrice(amd.accumulation_range?.high, symbol)}
          color={C.green}
          sub={amd.accumulation_range?.bars ? `${amd.accumulation_range.bars} bars` : ""}
        />
        <Stat
          label="Manip Type"
          value={amd.manipulation?.direction?.replace(/_/g, " ").toUpperCase() || "—"}
          color={C.amber}
          mono={false}
        />
        <Stat
          label="Manip Extreme"
          value={fmtPrice(amd.manipulation?.extreme, symbol)}
          color={C.red}
        />
        <Stat
          label="Tight SL"
          value={fmtPrice(amd.manipulation_sl, symbol)}
          color={C.red}
        />
        <Stat
          label="Entry Zone"
          value={amd.entry_zone_low && amd.entry_zone_high ? `${fmtPrice(amd.entry_zone_low, symbol)} – ${fmtPrice(amd.entry_zone_high, symbol)}` : "—"}
          color={C.blue}
        />
        {amd.mss_detected && (
          <Stat
            label="MSS Level"
            value={fmtPrice(amd.mss_level, symbol)}
            color="#d070ff"
            sub="Structural shift confirmed"
          />
        )}
        {amd.divergence_detected && amd.divergence_indices?.length >= 2 && (
          <Stat
            label="Div Range"
            value={`Bar ${amd.divergence_indices[0]} → ${amd.divergence_indices[1]}`}
            color={C.red}
            sub="Trapped momentum pivot"
            mono={false}
          />
        )}
      </div>

      {amd.distribution_started && (
        <div style={{ marginTop: 10, fontSize: 11, color: C.blue, background: C.blueDim, borderRadius: 6, padding: "5px 8px" }}>
          Distribution leg started. Wait for pullback to the AMD entry zone.
        </div>
      )}
      {(() => {
        const rrTargets = amd.rr_targets || computeRRTargets(amd.entry_zone_high, amd.manipulation_sl);
        if (!rrTargets) return null;
        return (
          <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
            {Object.entries(rrTargets).map(([label, price]) => (
              <div key={label} style={{
                background: C.greenDim,
                border: `1px solid ${C.green}40`,
                borderRadius: 6,
                padding: "3px 8px",
                fontSize: 10,
                fontFamily: "monospace",
              }}>
                <span style={{ color: C.muted }}>{label} {"->"} </span>
                <span style={{ color: C.green, fontWeight: 700 }}>{fmtPrice(price, symbol)}</span>
              </div>
            ))}
          </div>
        );
      })()}
      {!!amd.notes?.length && (
        <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 3 }}>
          {amd.notes.slice(0, 4).map((note, i) => (
            <div key={i} style={{ fontSize: 10, color: C.muted2, lineHeight: 1.4 }}>
              - {note}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function AMDSetupCard({ data }) {
  if (!data?.amd || data.amd.phase === "UNKNOWN") return null;
  const setupColor = SETUP_COLORS[data.setup_type] || C.muted2;
  const directionText = data.signal === "SELL" ? "SOUTH" : data.signal === "BUY" ? "NORTH" : "pending";

  let text = `${data.setup_type || "Waiting for clear AMD setup."} ${data.amd.notes?.[0] || ""}`.trim();
  if (data.signal === "SELL" && data.setup_type === "LH_SELL") {
    text = "Major trend BEARISH -> pullback formed LH -> stops were manipulated above -> distribution is aiming SOUTH. Sell from FVG inside LH zone, SL above manipulation wick, TP at next sell-side pool.";
  } else if (data.signal === "BUY" && data.setup_type === "HL_BUY") {
    text = "Major trend BULLISH -> pullback formed HL -> stops were manipulated below -> distribution is aiming NORTH. Buy from FVG inside HL zone, SL below manipulation wick, TP at next buy-side pool.";
  }

  return (
    <div style={{ ...cardSt, marginBottom: 12, borderColor: C.blueDim }}>
      <SecTitle right={directionText}>AMD Setup Logic</SecTitle>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 8 }}>
        {data.setup_type && (
          <span style={{
            color: setupColor, background: `${setupColor}22`, border: `1px solid ${setupColor}55`,
            borderRadius: 6, padding: "2px 8px", fontSize: 11, fontWeight: 800, fontFamily: "monospace",
          }}>
            {data.setup_type.replace(/_/g, " ")}
          </span>
        )}
        {data.amd.manipulation?.extreme && (
          <span style={{ color: C.muted2, fontSize: 11, fontFamily: "monospace" }}>
            Manip extreme: {fmtPrice(data.amd.manipulation.extreme, data.symbol)}
          </span>
        )}
      </div>
      <div style={{ fontSize: 12, color: C.textDim, lineHeight: 1.6 }}>{text}</div>
    </div>
  );
}

// Chart band helper
function addBand(chart, t0, t1, top, bot, color, lStyle, title, refs) {
  const mkLine = (val, title) => {
    const s = chart.addSeries(LineSeries, {
      color, lineWidth: 1, lineStyle: lStyle,
      crosshairMarkerVisible: false,
      lastValueVisible: false,
      priceLineVisible: false,
      title: title || "",
    });
    s.setData([{ time: t0, value: val }, { time: t1, value: val }]);
    refs.current.push(s);
  };
  mkLine(top, title);
  mkLine(bot, "");
}

function computeRRTargets(entry, sl, ratios = [1.5, 2.0, 3.0]) {
  if (!entry || !sl) return null;
  const risk = Math.abs(entry - sl);
  const isBearish = entry < sl;
  return ratios.reduce((targets, ratio) => {
    targets[`${ratio}RR`] = isBearish ? entry - risk * ratio : entry + risk * ratio;
    return targets;
  }, {});
}

function buildOscillatorData(ohlcData, rawOsc) {
  // Prefer API-provided oscillator data (real close-delta from amd_phases v2)
  if (rawOsc?.length) {
    return rawOsc.map((d) => ({
      time: Math.floor(new Date(d.time).getTime() / 1000),
      value: d.value,
    }));
  }
  // Fallback: client-side 14-bar close delta (mock mode)
  return ohlcData.map((c, i) => {
    if (i < 14) return { time: c.time, value: 0 };
    return { time: c.time, value: c.close - ohlcData[i - 14].close };
  });
}

function drawAMDBoxes(canvas, chart, candleSeries, amd, ohlcData) {
  if (!canvas || !chart || !candleSeries || !amd || !ohlcData?.length) return;

  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const priceToY = (price) => {
    const coord = candleSeries.priceToCoordinate(price);
    return coord != null ? coord : null;
  };
  const timeToX = (unixSec) => {
    const coord = chart.timeScale().timeToCoordinate(unixSec);
    return coord != null ? coord : null;
  };

  const drawBox = (tStart, tEnd, pHigh, pLow, cfg, extraLabel = null) => {
    const x1 = timeToX(tStart);
    const x2 = timeToX(tEnd);
    const y1 = priceToY(pHigh);
    const y2 = priceToY(pLow);
    if (x1 == null || x2 == null || y1 == null || y2 == null) return {};

    const left = Math.min(x1, x2);
    const right = Math.max(x1, x2);
    const top = Math.min(y1, y2);
    const bottom = Math.max(y1, y2);
    const boxW = right - left;
    const boxH = bottom - top;
    if (boxW < 2 || boxH < 2) return { left, right, top, bottom };

    ctx.fillStyle = cfg.fill;
    ctx.fillRect(left, top, boxW, boxH);
    ctx.strokeStyle = cfg.stroke;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 3]);
    ctx.strokeRect(left + 0.5, top + 0.5, boxW - 1, boxH - 1);
    ctx.setLineDash([]);

    ctx.font = "bold 11px 'JetBrains Mono', monospace";
    ctx.fillStyle = cfg.labelColor;
    ctx.fillText(cfg.label, left + 6, top + 15, Math.max(0, boxW - 12));

    if (extraLabel) {
      ctx.font = "bold 10px 'JetBrains Mono', monospace";
      ctx.fillText(extraLabel, left + 6, top + 28, Math.max(0, boxW - 12));
    }

    return { left, right, top, bottom };
  };

  const acc = amd.accumulation_range;
  const manip = amd.manipulation;

  if (acc && ohlcData[acc.start_index] && ohlcData[acc.end_index]) {
    drawBox(ohlcData[acc.start_index].time, ohlcData[acc.end_index].time, acc.high, acc.low, AMD_BOX.accumulation);
  }

  if (acc && manip && ohlcData[acc.end_index] && ohlcData[manip.candle_index]) {
    const isBearhunt = manip.direction === "bearish_hunt";
    const boxHigh = isBearhunt ? manip.extreme : acc.high;
    const boxLow = isBearhunt ? acc.low : manip.extreme;
    drawBox(
      ohlcData[acc.end_index].time,
      ohlcData[manip.candle_index].time,
      boxHigh,
      boxLow,
      AMD_BOX.manipulation,
    );
  }

  if (manip && amd.distribution_started && ohlcData[manip.candle_index]) {
    const lastCandle = ohlcData[ohlcData.length - 1];
    const isBearhunt = manip.direction === "bearish_hunt";
    const distHigh = isBearhunt
      ? (amd.entry_zone_high || acc?.high || manip.close)
      : (amd.entry_zone_high || manip.extreme * 1.02);
    const distLow = isBearhunt
      ? (amd.entry_zone_low || manip.extreme * 0.98)
      : (amd.entry_zone_low || acc?.low || manip.close);

    if (distHigh && distLow) {
      const distBounds = drawBox(
        ohlcData[manip.candle_index].time,
        lastCandle.time,
        distHigh,
        distLow,
        AMD_BOX.distribution,
      );
      const entry = amd.entry_zone_high || distHigh;

      if (entry && distBounds.right != null) {
        const entryY = priceToY(entry);
        if (entryY != null) {
          const txt = `${isBearhunt ? "SHORT" : "LONG"} ${fmt(entry)}`;
          ctx.font = "bold 11px 'JetBrains Mono', monospace";
          const tw = ctx.measureText(txt).width;
          ctx.fillStyle = isBearhunt ? "rgba(240,71,106,0.85)" : "rgba(32,214,138,0.85)";
          ctx.beginPath();
          if (ctx.roundRect) ctx.roundRect(distBounds.right + 4, entryY - 10, tw + 12, 18, 4);
          else ctx.rect(distBounds.right + 4, entryY - 10, tw + 12, 18);
          ctx.fill();
          ctx.fillStyle = "#ffffff";
          ctx.fillText(txt, distBounds.right + 10, entryY + 3);
        }
      }
    }
  }

}

// ── Higher-timeframe levels + session breaks ──────────────────────────────────
const TF_SEC     = { M1: 60, M5: 300, M15: 900, M30: 1800, H1: 3600, H4: 14400, D1: 86400, W1: 604800 };
const LEVEL_TFS  = ["M15", "M30", "H1", "H4", "D1", "W1"];
const LEVEL_COLORS = { M15: C.teal, M30: C.blue, H1: C.purple, H4: C.orange, D1: C.amber, W1: C.red };

const SESSIONS = [
  { name: "TOKYO",    tz: "Asia/Tokyo",       hour: 9, color: C.amber },
  { name: "LONDON",   tz: "Europe/London",    hour: 8, color: C.blue },
  { name: "NEW YORK", tz: "America/New_York", hour: 8, color: C.teal },
];

const tzFormatters = {};
// Seconds the given time zone is ahead of UTC at instant `ms`.
function tzOffsetSec(tz, ms) {
  const f = tzFormatters[tz] ||= new Intl.DateTimeFormat("en-US", {
    timeZone: tz, hourCycle: "h23",
    year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric",
  });
  const p = Object.fromEntries(f.formatToParts(new Date(ms)).map(x => [x.type, Number(x.value)]));
  const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute);
  return Math.round((asUtc - Math.floor(ms / 60000) * 60000) / 1000);
}
// UTC ms of a wall-clock time in `tz`.
function zonedToUtcMs(tz, y, m, d, h) {
  const guess = Date.UTC(y, m, d, h);
  let ms = guess - tzOffsetSec(tz, guess) * 1000;
  ms = guess - tzOffsetSec(tz, ms) * 1000;    // second pass settles DST edges
  return ms;
}
// MT5 timestamps are broker server time (NY + 7h on most brokers, incl. MetaQuotes-Demo), not UTC.
const chartOffsetSec = (ms, serverTime) => (serverTime ? tzOffsetSec("America/New_York", ms) + 7 * 3600 : 0);

// Index of the candle whose [time, time + tfSec) contains t, or -1 (weekend / gap / out of range).
function candleAt(ohlc, t, tfSec) {
  let lo = 0, hi = ohlc.length - 1, idx = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (ohlc[mid].time <= t) { idx = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return idx >= 0 && t < ohlc[idx].time + tfSec ? idx : -1;
}

function drawSessionBreaks(canvas, chart, ohlc, { tfSec, serverTime }) {
  if (!canvas || !chart || !ohlc?.length || !tfSec) return;
  const ctx = canvas.getContext("2d");
  const axisH = 28;
  const firstDay = Math.floor(ohlc[0].time / 86400) - 1;
  const lastDay  = Math.floor(ohlc[ohlc.length - 1].time / 86400) + 1;
  const stack = {};

  for (let day = firstDay; day <= lastDay; day++) {
    const date = new Date(day * 86400000);
    SESSIONS.forEach((s) => {
      const realMs = zonedToUtcMs(s.tz, date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate(), s.hour);
      const t = realMs / 1000 + chartOffsetSec(realMs, serverTime);
      const idx = candleAt(ohlc, t, tfSec);
      if (idx < 0) return;
      const x = chart.timeScale().timeToCoordinate(ohlc[idx].time);
      if (x == null || x < 0 || x > canvas.width) return;

      const row = stack[Math.round(x)] = (stack[Math.round(x)] || 0) + 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, 0);
      ctx.lineTo(Math.round(x) + 0.5, canvas.height - axisH);
      ctx.strokeStyle = s.color + "80";
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 4]);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.font = "bold 9px 'JetBrains Mono', monospace";
      ctx.fillStyle = s.color;
      ctx.fillText(s.name, x + 4, 12 * row);
    });
  }
}

function drawOverlay(canvas, chart, candleSeries, amd, ohlcData, sessions) {
  if (!canvas) return;
  canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
  drawAMDBoxes(canvas, chart, candleSeries, amd, ohlcData);
  if (sessions?.enabled) drawSessionBreaks(canvas, chart, ohlcData, sessions);
}

// Previous completed + current (forming) HTF candle. `bars` are {time (sec), high, low}, oldest first.
function htfLevels(bars, tf, nowSec) {
  if (!bars.length) return { prev: null, cur: null };
  const last = bars[bars.length - 1];
  const periodEnd = last.time + (tf === "W1" ? 5 * 86400 : TF_SEC[tf]);   // forex week closes Friday
  return nowSec < periodEnd
    ? { prev: bars[bars.length - 2] || null, cur: last }
    : { prev: last, cur: null };
}

function groupWeekly(daily) {
  const weeks = [];
  daily.forEach((b) => {
    const dow = (new Date(b.time * 1000).getUTCDay() + 6) % 7;      // Monday = 0
    const start = Math.floor(b.time / 86400) * 86400 - dow * 86400;
    const w = weeks[weeks.length - 1];
    if (w && w.time === start) { w.high = Math.max(w.high, b.high); w.low = Math.min(w.low, b.low); }
    else weeks.push({ time: start, high: b.high, low: b.low });
  });
  return weeks;
}

const loadPref = (key, fallback) => {
  try { const v = localStorage.getItem(key); return v == null ? fallback : JSON.parse(v); }
  catch { return fallback; }
};
const savePref = (key, value) => {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch {
    // Storage can be blocked (private mode); the chart works without it.
  }
};

// ── Session colour helper ─────────────────────────────────────────────────────
function sessionColor(session) {
  if (!session) return "gray";
  if (session.includes("Overlap")) return "green";
  if (session.includes("London") || session.includes("New York")) return "blue";
  if (session.includes("Tokyo") || session.includes("Sydney")) return "amber";
  return "red"; // Off-Hours
}

// ── Bot control panel (polls /bot/status, start/stop the scan loop) ────────────
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString() : "—");

function BotPanel({ onClosePosition, onUpdatePosition, onShowPosition, orderDrafts, setOrderDrafts }) {
  const [bot,     setBot]     = useState(null);
  const [busy,    setBusy]    = useState(false);
  const [fundsBusy, setFundsBusy] = useState(false);
  const [fundsAmount, setFundsAmount] = useState("1000");
  const [fundsMessage, setFundsMessage] = useState(null);
  const [closeBusy, setCloseBusy] = useState(null);
  const [updateBusy, setUpdateBusy] = useState(null);
  const [orderMessage, setOrderMessage] = useState(null);
  const [botErr,  setBotErr]  = useState(null);
  const [open,    setOpen]    = useState(false);

  const fetchBot = useCallback(async () => {
    try {
      const res = await fetch(`${API}/bot/status`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const nextBot = await res.json();
      setBot(nextBot);
      setOrderDrafts(current => {
        const positions = nextBot.open_positions || [];
        const nextDrafts = Object.fromEntries(positions.map(position => [
          position.id,
          current[position.id] || {
            sl: String(position.sl),
            tp: String(position.tp),
            lots: String(position.lots),
          },
        ]));
        const unchanged = Object.keys(current).length === positions.length
          && positions.every(position => current[position.id] === nextDrafts[position.id]);
        return unchanged ? current : nextDrafts;
      });
      setBotErr(null);
    } catch (e) {
      setBot(null);
      setBotErr(`API unreachable (${e.message})`);
    }
  }, [setOrderDrafts]);

  useEffect(() => {
    const first = setTimeout(fetchBot, 0);
    const t = setInterval(fetchBot, 5_000);
    return () => { clearTimeout(first); clearInterval(t); };
  }, [fetchBot]);

  const toggle = async () => {
    setBusy(true); setBotErr(null);
    try {
      const res = await fetch(`${API}/bot/${bot?.running ? "stop" : "start"}`, { method: "POST" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      await fetchBot();
    } catch (e) { setBotErr(e.message); }
    finally     { setBusy(false); }
  };

  const adjustFunds = async (action) => {
    const amount = Number(fundsAmount);
    if (!Number.isFinite(amount) || amount < 0.01) {
      setFundsMessage({ type: "error", text: "Enter an amount of at least $0.01." });
      return;
    }
    setFundsBusy(true);
    setFundsMessage(null);
    try {
      const res = await fetch(`${API}/bot/paper-account/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      await fetchBot();
      setFundsMessage({
        type: "success",
        text: `Simulated ${action === "deposit" ? "deposit" : "withdrawal"} of $${amount.toFixed(2)} complete.`,
      });
    } catch (e) {
      setFundsMessage({ type: "error", text: e.message });
    } finally {
      setFundsBusy(false);
    }
  };

  const closePosition = async (position) => {
    const price = position.mark_price ?? position.entry;
    if (!window.confirm(
      `Close paper ${position.direction} ${position.symbol} ${position.timeframe} at approximately ${fmtPrice(price, position.symbol)}?`,
    )) return;
    setCloseBusy(position.id);
    setOrderMessage(null);
    try {
      await onClosePosition(position.id);
      await fetchBot();
      setOrderMessage({ type: "success", text: `${position.symbol} paper order closed.` });
    } catch (e) {
      setOrderMessage({ type: "error", text: e.message });
    } finally {
      setCloseBusy(null);
    }
  };

  const running = !!bot?.running;
  const paper   = bot?.paper || {};
  const openPositions = bot?.open_positions || [];
  const recentClosed = bot?.recent_closed || [];
  const rows    = bot?.rows || [];
  const ready   = rows.filter(r => r.ready);
  const pending = rows.filter(r => !r.ready && (r.signal === "BUY" || r.signal === "SELL"));

  const editOrderDraft = (position, field, value) => {
    setOrderDrafts(current => ({
      ...current,
      [position.id]: { ...current[position.id], [field]: value },
    }));
  };

  const stepOrderDraft = (position, field, sign) => {
    const current = orderDrafts[position.id];
    if (!current) return;
    const step = field === "lots" ? 0.01 : pipSize(position.symbol);
    const digits = field === "lots" ? 2 : priceDecimals(position.symbol);
    editOrderDraft(position, field, String((Number(current[field]) + sign * step).toFixed(digits)));
  };

  const updatePosition = async (position) => {
    const draft = orderDrafts[position.id];
    const values = {
      stop_loss: Number(draft?.sl),
      take_profit: Number(draft?.tp),
      lots: Number(draft?.lots),
    };
    if (!Object.values(values).every(Number.isFinite)) {
      setOrderMessage({ type: "error", text: "Enter valid SL, TP, and lot values." });
      return;
    }
    setUpdateBusy(position.id);
    setOrderMessage(null);
    try {
      const updated = await onUpdatePosition(position, values);
      setOrderDrafts(current => ({
        ...current,
        [position.id]: {
          sl: String(updated.sl),
          tp: String(updated.tp),
          lots: String(updated.lots),
        },
      }));
      await fetchBot();
      setOrderMessage({ type: "success", text: `${position.symbol} order updated.` });
    } catch (e) {
      setOrderMessage({ type: "error", text: e.message });
    } finally {
      setUpdateBusy(null);
    }
  };

  return (
    <div id="market-bot-panel" className="bot-panel" style={{ ...cardSt, marginBottom: 14, borderColor: running ? C.greenDim : C.border }}>
      <div className="bot-panel-summary" style={{ display: "flex", gap: 18, flexWrap: "wrap", alignItems: "center" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{
            width: 9, height: 9, borderRadius: 99,
            background: running ? C.green : C.muted,
            boxShadow: running ? `0 0 8px ${C.green}` : "none",
          }} />
          <span style={{ fontSize: 13, fontWeight: 800, color: C.text }}>Market Bot</span>
          <Badge small label={running ? "RUNNING" : "STOPPED"} color={running ? "green" : "gray"} />
          {bot && <Badge small label={bot.mode?.toUpperCase()} color={bot.mode === "paper" ? "blue" : "amber"} />}
          {bot?.running_in === "cli" && <Badge small label="IN TERMINAL" color="purple" />}
        </div>

        {bot && <>
          <Stat label="Cycles"    value={bot.cycles} />
          <Stat label="Last scan" value={fmtTime(bot.last_scan_at)} color={C.muted2} />
          <Stat label="Next scan" value={running ? fmtTime(bot.next_scan_at) : "—"} color={C.muted2} />
          <Stat label="Equity"    value={paper.equity != null ? `$${Number(paper.equity).toFixed(2)}` : "—"} />
          <Stat label="Net P&L"   value={`${(paper.net_pnl || 0) >= 0 ? "+" : ""}$${Number(paper.net_pnl || 0).toFixed(2)}`}
                color={(paper.net_pnl || 0) >= 0 ? C.green : C.red} />
          <Stat label="Trades"    value={`${paper.trades || 0} · ${fmtPct((paper.win_rate || 0) * 100)} win`} color={C.muted2} />
          <Stat label="MT5"       value={bot.mt5_connected ? "connected" : "offline"} color={bot.mt5_connected ? C.green : C.red} mono={false} />
        </>}

        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button onClick={() => setOpen(o => !o)} style={{
            ...btnSt, background: "transparent", color: C.muted2, border: `1px solid ${C.border}`,
          }}>{open ? "Hide details" : "Details"}</button>
          <button onClick={toggle} disabled={busy || !bot} style={{
            ...btnSt, border: "none", padding: "7px 18px", fontSize: 12, fontWeight: 700, color: "#fff",
            background: running ? C.red : C.green,
            opacity: busy || !bot ? 0.5 : 1,
          }}>
            {busy ? "…" : running ? "■ Stop Bot" : "▶ Start Bot"}
          </button>
        </div>
      </div>

      {botErr && <div style={{ marginTop: 10, fontSize: 12, color: C.red }}>✗ {botErr}</div>}
      {bot?.risk?.halted && <div style={{ marginTop: 10, fontSize: 12, color: C.red, fontWeight: 700 }}>⛔ Trading halted: {bot.risk.halt_reason}</div>}

      {bot && (
        <div className="paper-account">
          <div className="paper-account-heading">
            <div>
              <div className="paper-account-title">Demo account <Badge label="VIRTUAL FUNDS" color="teal" small /></div>
              <div className="paper-account-note">
                Paper simulation only · Latest price mark {fmtTime(bot.last_scan_at)}
              </div>
            </div>
            <div className="paper-account-metrics">
              <Stat label="Balance" value={`$${Number(paper.balance ?? paper.starting_balance ?? 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`} color={C.text} />
              <Stat label="Equity" value={`$${Number(paper.equity ?? 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`} color={C.blue} />
              <Stat label="Floating P&L" value={`${Number(paper.floating_pnl || 0) >= 0 ? "+" : ""}$${Number(paper.floating_pnl || 0).toFixed(2)}`} color={Number(paper.floating_pnl || 0) >= 0 ? C.green : C.red} />
              <Stat label="Open orders" value={openPositions.length} color={C.text} />
            </div>
            <div className="paper-funding-controls">
              <input
                aria-label="Virtual account amount"
                type="number"
                min="0.01"
                step="100"
                value={fundsAmount}
                onChange={e => setFundsAmount(e.target.value)}
              />
              <button onClick={() => adjustFunds("deposit")} disabled={fundsBusy || Number(fundsAmount) < 0.01} className="fund-deposit">
                {fundsBusy ? "…" : "Deposit"}
              </button>
              <button onClick={() => adjustFunds("withdraw")} disabled={fundsBusy || Number(fundsAmount) < 0.01} className="fund-withdraw">
                {fundsBusy ? "…" : "Withdraw"}
              </button>
            </div>
          </div>
          {fundsMessage && (
            <div className={`funds-message ${fundsMessage.type}`}>{fundsMessage.text}</div>
          )}
          {orderMessage && (
            <div className={`funds-message ${orderMessage.type}`}>{orderMessage.text}</div>
          )}

          <div className="order-status-section">
            <SecTitle right={`${openPositions.length} open`}>Order status</SecTitle>
            {openPositions.length === 0 ? (
              <div className="empty-orders">No open paper orders. Orders you place will appear here.</div>
            ) : (
              <div className="order-table-wrap">
                <table className="order-table">
                  <thead><tr>
                    <th>Status</th><th>Side</th><th>Symbol / TF</th><th>Entry</th><th>Mark</th><th>SL</th><th>TP</th><th>Lots</th><th>Floating P&L</th><th>Action</th>
                  </tr></thead>
                  <tbody>{openPositions.map(p => {
                    const draft = orderDrafts[p.id] || {
                      sl: String(p.sl), tp: String(p.tp), lots: String(p.lots),
                    };
                    const draftChanged = Number(draft.sl) !== Number(p.sl)
                      || Number(draft.tp) !== Number(p.tp)
                      || Number(draft.lots) !== Number(p.lots);
                    return (
                      <Fragment key={p.id}>
                        <tr>
                          <td><Badge label="OPEN" color="blue" small /></td>
                          <td className={p.direction === "BUY" ? "order-buy" : "order-sell"}>{p.direction}</td>
                          <td>{p.symbol} · {p.timeframe}</td>
                          <td>{fmtPrice(p.entry, p.symbol)}</td>
                          <td>{fmtPrice(p.mark_price ?? p.entry, p.symbol)}</td>
                          <td className="order-sell">{fmtPrice(p.sl, p.symbol)}</td>
                          <td className="order-buy">{fmtPrice(p.tp, p.symbol)}</td>
                          <td>{Number(p.lots).toFixed(2)}</td>
                          <td className={Number(p.unrealized_pnl || 0) >= 0 ? "order-buy" : "order-sell"}>
                            {Number(p.unrealized_pnl || 0) >= 0 ? "+" : ""}${Number(p.unrealized_pnl || 0).toFixed(2)}
                          </td>
                          <td>
                            <button
                              className="order-chart-button"
                              onClick={() => onShowPosition(p)}
                            >
                              Chart
                            </button>
                            <button className="order-close-button" disabled={closeBusy === p.id} onClick={() => closePosition(p)}>
                              {closeBusy === p.id ? "Closing…" : "Close"}
                            </button>
                          </td>
                        </tr>
                        <tr className="order-edit-row">
                          <td colSpan="10">
                            <div className="order-edit-controls">
                              {[
                                ["sl", "SL", p.sl],
                                ["tp", "TP", p.tp],
                              ].map(([field, label, original]) => (
                                <div className="order-edit-field" key={field}>
                                  <label htmlFor={`order-${p.id}-${field}`}>{label}</label>
                                  <button
                                    type="button"
                                    className="order-step-button"
                                    aria-label={`Decrease ${label}`}
                                    onClick={() => stepOrderDraft(p, field, -1)}
                                  >−</button>
                                  <input
                                    id={`order-${p.id}-${field}`}
                                    type="number"
                                    step={pipSize(p.symbol)}
                                    value={draft[field]}
                                    onChange={e => editOrderDraft(p, field, e.target.value)}
                                  />
                                  <button
                                    type="button"
                                    className="order-step-button"
                                    aria-label={`Increase ${label}`}
                                    onClick={() => stepOrderDraft(p, field, 1)}
                                  >+</button>
                                  <small>was {fmtPrice(original, p.symbol)}</small>
                                </div>
                              ))}
                              <div className="order-edit-field lots">
                                <label htmlFor={`order-${p.id}-lots`}>LOTS</label>
                                <button
                                  type="button"
                                  className="order-step-button"
                                  aria-label="Decrease lots"
                                  onClick={() => stepOrderDraft(p, "lots", -1)}
                                >−</button>
                                <input
                                  id={`order-${p.id}-lots`}
                                  type="number"
                                  min="0.01"
                                  step="0.01"
                                  value={draft.lots}
                                  onChange={e => editOrderDraft(p, "lots", e.target.value)}
                                />
                                <button
                                  type="button"
                                  className="order-step-button"
                                  aria-label="Increase lots"
                                  onClick={() => stepOrderDraft(p, "lots", 1)}
                                >+</button>
                              </div>
                              <button
                                className="order-update-button"
                                disabled={!draftChanged || updateBusy === p.id}
                                onClick={() => updatePosition(p)}
                              >
                                {updateBusy === p.id ? "Updating…" : "Update"}
                              </button>
                            </div>
                          </td>
                        </tr>
                      </Fragment>
                    );
                  })}</tbody>
                </table>
              </div>
            )}
            {recentClosed.length > 0 && (
              <div className="recent-order-status">
                <span>RECENTLY CLOSED</span>
                {recentClosed.slice(0, 3).map(p => (
                  <div key={p.id} className="recent-order-row">
                    <Badge label="CLOSED" color="gray" small />
                    <strong className={p.direction === "BUY" ? "order-buy" : "order-sell"}>{p.direction}</strong>
                    <span>{p.symbol} · {p.timeframe}</span>
                    <span>{p.exit_reason || "closed"}</span>
                    <strong className={Number(p.pnl) >= 0 ? "order-buy" : "order-sell"}>
                      {Number(p.pnl) >= 0 ? "+" : ""}${Number(p.pnl).toFixed(2)}
                    </strong>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {open && bot && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: 14, marginTop: 14 }}>
          <div>
            <SecTitle right={`${bot.open_positions.length} open`}>Open positions</SecTitle>
            {bot.open_positions.length === 0
              ? <div style={{ fontSize: 11, color: C.muted }}>No open trades.</div>
              : bot.open_positions.map(p => (
                <div key={p.id} style={{ display: "flex", gap: 10, fontSize: 11, fontFamily: "monospace", padding: "4px 0", borderBottom: `1px solid ${C.border}` }}>
                  <span style={{ color: p.direction === "BUY" ? C.green : C.red, fontWeight: 700, minWidth: 34 }}>{p.direction}</span>
                  <span style={{ color: C.text, minWidth: 90 }}>{p.symbol} {p.timeframe}</span>
                  <span style={{ color: C.muted2 }}>@ {fmtPrice(p.entry, p.symbol)}</span>
                  <span style={{ color: C.red }}>SL {fmtPrice(p.sl, p.symbol)}</span>
                  <span style={{ color: C.green }}>TP {fmtPrice(p.tp, p.symbol)}</span>
                  <span style={{ color: C.muted, marginLeft: "auto" }}>{p.lots} lots</span>
                </div>
              ))}

            <div style={{ marginTop: 14 }}>
              <SecTitle right={`${bot.recent_closed.length} shown`}>Recent closed</SecTitle>
              {bot.recent_closed.length === 0
                ? <div style={{ fontSize: 11, color: C.muted }}>No closed trades yet.</div>
                : bot.recent_closed.slice(0, 6).map(p => (
                  <div key={p.id} style={{ display: "flex", gap: 10, fontSize: 11, fontFamily: "monospace", padding: "4px 0", borderBottom: `1px solid ${C.border}` }}>
                    <span style={{ color: p.direction === "BUY" ? C.green : C.red, fontWeight: 700, minWidth: 34 }}>{p.direction}</span>
                    <span style={{ color: C.text, minWidth: 90 }}>{p.symbol} {p.timeframe}</span>
                    <span style={{ color: C.muted2 }}>{p.exit_reason}</span>
                    <span style={{ color: p.pnl >= 0 ? C.green : C.red, marginLeft: "auto" }}>
                      {p.pnl >= 0 ? "+" : ""}{Number(p.pnl).toFixed(2)} ({Number(p.r_multiple).toFixed(2)}R)
                    </span>
                  </div>
                ))}
            </div>
          </div>

          <div>
            <SecTitle right={`${bot.watchlist.length} symbols × ${bot.timeframes.length} TFs · every ${bot.interval_seconds}s`}>Last scan</SecTitle>
            {rows.length === 0
              ? <div style={{ fontSize: 11, color: C.muted }}>No scan yet — start the bot.</div>
              : rows.map(r => (
                <div key={`${r.symbol}-${r.timeframe}`} style={{ display: "flex", gap: 10, fontSize: 11, padding: "4px 0", borderBottom: `1px solid ${C.border}`, alignItems: "baseline" }}>
                  <span style={{ fontFamily: "monospace", color: C.text, minWidth: 90 }}>{r.symbol} {r.timeframe}</span>
                  <span style={{
                    fontWeight: 700, minWidth: 34,
                    color: r.signal === "BUY" ? C.green : r.signal === "SELL" ? C.red : C.amber,
                  }}>{r.signal}</span>
                  <span style={{ color: r.ready ? C.green : C.muted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {r.ready ? `READY · ${r.action}` : (r.block_reasons?.[0] || r.error || "—")}
                  </span>
                </div>
              ))}
            <div style={{ marginTop: 8, fontSize: 10, color: C.muted }}>
              {ready.length} ready · {pending.length} waiting for entry
              {bot.upcoming_news?.length > 0 && ` · ${bot.upcoming_news.length} high-impact news in 24h`}
            </div>
            {bot.errors?.length > 0 && (
              <div style={{ marginTop: 8, fontSize: 11, color: C.red }}>
                {bot.errors.slice(0, 3).map((e, i) => <div key={i}>✗ {typeof e === "string" ? e : JSON.stringify(e)}</div>)}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Main Component ─────────────────────────────────────────────────────────────
export default function SMCDashboard() {
  const [symbol,   setSymbol]  = useState("XAUUSD");
  const [tf,       setTf]      = useState("M15");
  const [count,    setCount]   = useState(200);
  const [useMock,  setMock]    = useState(false);
  const [spread,   setSpread]  = useState("");        // user-entered spread pips
  const [data,     setData]    = useState(null);
  const [riskData, setRiskData]= useState(null);
  const [manualBusy, setManualBusy] = useState(false);
  const [manualMessage, setManualMessage] = useState(null);
  const [loading,  setLoad]    = useState(false);
  const [err,      setErr]     = useState(null);
  const [autoRefresh, setAuto] = useState(false);
  const [lastUpdate, setLast]  = useState(null);
  const [activeTab, setTab]    = useState("confluence");
  const [levelTfs, setLevelTfs]         = useState(() => loadPref("smc.levelTfs", []));
  const [levelCurrent, setLevelCurrent] = useState(() => loadPref("smc.levelCurrent", false));
  const [showSessions, setShowSessions] = useState(() => loadPref("smc.showSessions", false));
  const [levelsOpen, setLevelsOpen]     = useState(false);
  const [chartLayersOpen, setChartLayersOpen] = useState(false);
  const [chartLayers, setChartLayers] = useState({
    structure: false,
    liquidity: false,
    tradePlan: false,
    amd: false,
  });
  const [htf, setHtf]                   = useState({ symbol: null, byTf: {} });
  const [levelsErr, setLevelsErr]       = useState(null);
  const [openOrders, setOpenOrders]     = useState([]);
  const [orderDrafts, setOrderDrafts]   = useState({});
  const [orderActionMessage, setOrderActionMessage] = useState(null);

  const chartRef   = useRef(null);
  const oscRef     = useRef(null);
  const chartInst  = useRef(null);
  const oscInst    = useRef(null);
  const candleSer  = useRef(null);
  const oscSer     = useRef(null);
  const canvasRef  = useRef(null);
  const markerApi  = useRef(null);
  const divSer     = useRef(null);
  const seriesRefs = useRef([]);
  const ohlcRef    = useRef([]);
  const amdRef     = useRef(null);
  const syncingRef = useRef(false);
  const timerRef   = useRef(null);
  const riskTimer  = useRef(null);
  const levelSer   = useRef([]);
  const orderLinesRef = useRef([]);
  const orderDragRef = useRef(null);
  const levelsBox  = useRef(null);
  const chartLayersBox = useRef(null);
  const chartLayersRef = useRef(chartLayers);
  const sessRef    = useRef({ enabled: false, tfSec: 0, serverTime: false });

  // ── Fetch analysis ──────────────────────────────────────────────────────────
  const fetchData = useCallback(async () => {
    setLoad(true); setErr(null);
    try {
      let url = `${API}/analyze?symbol=${symbol}&timeframe=${tf}&count=${count}&use_mock=${useMock}`;
      if (spread !== "" && !isNaN(Number(spread))) url += `&spread_pips=${Number(spread)}`;
      const res  = await fetch(url);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      setData(await res.json());
      setLast(clk());
    } catch (e) { setErr(e.message); }
    finally     { setLoad(false); }
  }, [symbol, tf, count, useMock, spread]);

  // ── Fetch risk status (separate lightweight poll) ───────────────────────────
  const fetchRisk = useCallback(async () => {
    try {
      const res = await fetch(`${API}/risk/status`);
      if (res.ok) setRiskData(await res.json());
    } catch {
      // Risk status is optional; keep the dashboard usable if that endpoint is unavailable.
    }
  }, []);

  const placeManualPaperTrade = useCallback(async (direction) => {
    const levels = manualTradeLevels(direction, data);
    if (!levels) {
      setManualMessage({ type: "error", text: "Cannot create manual levels without valid current market data." });
      return;
    }
    const { entry, stopLoss, takeProfit } = levels;
    const overridesSignal = data.signal !== direction;
    if (!window.confirm(
      `Open a manual PAPER ${direction} on ${data.symbol} ${data.timeframe}?\n` +
      `Entry: ${entry} | SL: ${stopLoss} | TP: ${takeProfit}` +
      (overridesSignal ? `\nThis overrides the current ${data.signal} signal.` : ""),
    )) return;

    setManualBusy(true);
    setManualMessage(null);
    try {
      const res = await fetch(`${API}/bot/manual-trade`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: data.symbol,
          timeframe: data.timeframe,
          direction,
          signal: data.signal,
          entry,
          stop_loss: stopLoss,
          take_profit: takeProfit,
          bar_time: data.candles[data.candles.length - 1].time,
          setup: overridesSignal ? "MANUAL_OVERRIDE" : (data.setup_type || "MANUAL"),
          confidence: data.confidence || 0,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      const p = body.position;
      setManualMessage({
        type: "success",
        text: `Opened paper ${direction} ${p.symbol} ${p.timeframe} · ${p.lots} lots · #${p.id}`,
      });
      fetchRisk();
    } catch (e) {
      setManualMessage({ type: "error", text: e.message });
    } finally {
      setManualBusy(false);
    }
  }, [data, fetchRisk]);

  const fetchOpenOrders = useCallback(async () => {
    try {
      const res = await fetch(`${API}/bot/status`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      setOpenOrders(body.open_positions || []);
    } catch (e) {
      setOrderActionMessage({ type: "error", text: `Could not refresh paper orders: ${e.message}` });
    }
  }, []);

  const closeOrder = useCallback(async (positionId) => {
    const res = await fetch(`${API}/bot/positions/${encodeURIComponent(positionId)}/close`, {
      method: "POST",
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
    setOrderActionMessage({
      type: "success",
      text: `Closed paper order ${body.position.symbol} ${body.position.direction}.`,
    });
    await fetchOpenOrders();
    fetchRisk();
  }, [fetchOpenOrders, fetchRisk]);

  const updateOrder = useCallback(async (position, values) => {
    const orderBarTime = data?.symbol === position.symbol && data?.timeframe === position.timeframe
      ? data.candles?.[data.candles.length - 1]?.time
      : null;
    const res = await fetch(`${API}/bot/positions/${encodeURIComponent(position.id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...values,
        bar_time: orderBarTime || new Date().toISOString(),
      }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
    setOrderActionMessage({
      type: "success",
      text: `SL, TP, and lots updated for ${body.position.symbol}.`,
    });
    await fetchOpenOrders();
    fetchRisk();
    return body.position;
  }, [data, fetchOpenOrders, fetchRisk]);

  const stageOrderLevel = useCallback((position, level, price) => {
    const digits = priceDecimals(position.symbol);
    setOrderDrafts(current => {
      const draft = current[position.id] || {
        sl: String(position.sl),
        tp: String(position.tp),
        lots: String(position.lots),
      };
      return {
        ...current,
        [position.id]: { ...draft, [level]: Number(price).toFixed(digits) },
      };
    });
  }, []);

  const showPositionOnChart = useCallback((position) => {
    setSymbol(position.symbol);
    setTf(position.timeframe);
    document.getElementById("trade-chart")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);

  useEffect(() => {
    const initialFetch = setTimeout(fetchOpenOrders, 0);
    const interval = setInterval(fetchOpenOrders, 5_000);
    return () => {
      clearTimeout(initialFetch);
      clearInterval(interval);
    };
  }, [fetchOpenOrders]);

  // Auto-refresh analysis
  useEffect(() => {
    if (autoRefresh) { timerRef.current = setInterval(fetchData, 30_000); }
    else             { clearInterval(timerRef.current); }
    return () => clearInterval(timerRef.current);
  }, [autoRefresh, fetchData]);

  // Risk status every 10s always
  useEffect(() => {
    const initialRiskFetch = setTimeout(fetchRisk, 0);
    riskTimer.current = setInterval(fetchRisk, 10_000);
    return () => {
      clearTimeout(initialRiskFetch);
      clearInterval(riskTimer.current);
    };
  }, [fetchRisk]);

  // Initial fetch on param change
  useEffect(() => {
    const initialFetch = setTimeout(fetchData, 0);
    return () => clearTimeout(initialFetch);
  }, [fetchData]);

  // ── Levels dropdown: persist choices, close on outside click ────────────────
  useEffect(() => {
    savePref("smc.levelTfs", levelTfs);
    savePref("smc.levelCurrent", levelCurrent);
    savePref("smc.showSessions", showSessions);
  }, [levelTfs, levelCurrent, showSessions]);

  useEffect(() => {
    chartLayersRef.current = chartLayers;
    drawOverlay(
      canvasRef.current,
      chartInst.current,
      candleSer.current,
      chartLayers.amd ? amdRef.current : null,
      ohlcRef.current,
      sessRef.current,
    );
  }, [chartLayers]);

  useEffect(() => {
    if (!levelsOpen && !chartLayersOpen) return;
    const onDown = (e) => {
      if (levelsBox.current && !levelsBox.current.contains(e.target)) setLevelsOpen(false);
      if (chartLayersBox.current && !chartLayersBox.current.contains(e.target)) setChartLayersOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [levelsOpen, chartLayersOpen]);

  // Session breaks live on the canvas overlay; redraw when toggled
  const chartTf    = data?.timeframe || tf;
  const serverTime = !!data?.mt5_connected && !useMock;
  useEffect(() => {
    sessRef.current = {
      enabled: showSessions && TF_SEC[chartTf] < TF_SEC.D1,
      tfSec: TF_SEC[chartTf],
      serverTime,
    };
    drawOverlay(
      canvasRef.current,
      chartInst.current,
      candleSer.current,
      chartLayersRef.current.amd ? amdRef.current : null,
      ohlcRef.current,
      sessRef.current,
    );
  }, [showSessions, chartTf, serverTime]);

  // Fetch the few higher-timeframe candles needed for the selected high/low levels
  useEffect(() => {
    if (!levelTfs.length) return;
    let cancelled = false;
    const needed = [...new Set(levelTfs.map(t => (t === "W1" ? "D1" : t)))];
    Promise.all(needed.map(async (t) => {
      const res = await fetch(`${API}/candles?symbol=${symbol}&timeframe=${t}&count=${t === "D1" ? 15 : 5}&use_mock=${useMock}`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      return [t, (await res.json()).candles];
    }))
      .then((entries) => { if (!cancelled) { setHtf({ symbol, byTf: Object.fromEntries(entries) }); setLevelsErr(null); } })
      .catch((e) => { if (!cancelled) setLevelsErr(e.message); });
    return () => { cancelled = true; };
  }, [symbol, useMock, levelTfs, data]);

  // ── Chart ───────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!chartRef.current) return;
    if (!chartInst.current) {
      chartInst.current = createChart(chartRef.current, {
        layout: { background: { color: C.bg }, textColor: C.muted2, fontFamily: "monospace", fontSize: 11 },
        grid:   { vertLines: { color: C.border }, horzLines: { color: C.border } },
        crosshair: {
          mode: CrosshairMode.Normal,
          vertLine: { color: C.border2, labelBackgroundColor: C.surface2 },
          horzLine: { color: C.border2, labelBackgroundColor: C.surface2 },
        },
        rightPriceScale: { borderColor: C.border, scaleMargins: { top: 0.08, bottom: 0.08 } },
        timeScale: { borderColor: C.border, timeVisible: true, secondsVisible: false },
        width: chartRef.current.clientWidth,
        height: 390,
      });
      candleSer.current = chartInst.current.addSeries(CandlestickSeries, {
        upColor: C.green, downColor: C.red,
        borderUpColor: C.green, borderDownColor: C.red,
        wickUpColor: C.green, wickDownColor: C.red,
      });
      const ro = new ResizeObserver(() => {
        if (chartInst.current && chartRef.current) {
          const w = chartRef.current.clientWidth;
          chartInst.current.resize(w, 390);
          if (canvasRef.current) {
            canvasRef.current.width = w;
            canvasRef.current.height = 390;
          }
        }
        if (oscInst.current && oscRef.current) {
          oscInst.current.resize(oscRef.current.clientWidth, 120);
        }
        drawOverlay(
          canvasRef.current,
          chartInst.current,
          candleSer.current,
          chartLayersRef.current.amd ? amdRef.current : null,
          ohlcRef.current,
          sessRef.current,
        );
      });
      ro.observe(chartRef.current);
      chartInst.current.timeScale().subscribeVisibleTimeRangeChange(() => {
        drawOverlay(
          canvasRef.current,
          chartInst.current,
          candleSer.current,
          chartLayersRef.current.amd ? amdRef.current : null,
          ohlcRef.current,
          sessRef.current,
        );
      });
    }

    if (!oscInst.current && oscRef.current) {
      oscInst.current = createChart(oscRef.current, {
        layout: { background: { color: C.bg }, textColor: C.muted2, fontFamily: "monospace", fontSize: 10 },
        grid:   { vertLines: { color: C.border }, horzLines: { color: C.border } },
        crosshair: {
          mode: CrosshairMode.Normal,
          vertLine: { color: C.border2, labelBackgroundColor: C.surface2 },
          horzLine: { color: C.border2, labelBackgroundColor: C.surface2 },
        },
        rightPriceScale: { borderColor: C.border, scaleMargins: { top: 0.1, bottom: 0.1 } },
        timeScale: { borderColor: C.border, timeVisible: true, secondsVisible: false, visible: false },
        width: oscRef.current.clientWidth,
        height: 120,
      });
      oscSer.current = oscInst.current.addSeries(HistogramSeries, {
        color: C.blue,
        priceFormat: { type: "price", precision: 5, minMove: 0.00001 },
        priceLineVisible: false,
        lastValueVisible: false,
      });

      chartInst.current.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        if (!range || !oscInst.current || syncingRef.current) return;
        syncingRef.current = true;
        oscInst.current.timeScale().setVisibleLogicalRange(range);
        syncingRef.current = false;
      });
      oscInst.current.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        if (!range || !chartInst.current || syncingRef.current) return;
        syncingRef.current = true;
        chartInst.current.timeScale().setVisibleLogicalRange(range);
        syncingRef.current = false;
      });
    }

    if (!data?.candles?.length) return;

    const sorted = [...data.candles].sort((a, b) => new Date(a.time) - new Date(b.time));
    const ohlc   = sorted.map((c) => ({
      time:  Math.floor(new Date(c.time).getTime() / 1000),
      open: c.open, high: c.high, low: c.low, close: c.close,
    }));
    ohlcRef.current = ohlc;
    amdRef.current = data.amd || null;
    if (canvasRef.current && chartRef.current) {
      canvasRef.current.width = chartRef.current.clientWidth;
      canvasRef.current.height = 390;
    }
    candleSer.current.setData(ohlc);

    if (oscSer.current) {
      const oscData = buildOscillatorData(ohlc, data.oscillator_data);
      oscSer.current.setData(oscData.map((d) => ({
        ...d,
        color: d.value >= 0 ? C.green : C.red,
      })));

      if (divSer.current) {
        try { oscInst.current.removeSeries(divSer.current); } catch {
          // The divergence series can already be gone after a quick refresh.
        }
        divSer.current = null;
      }
      if (oscInst.current && data.amd?.divergence_detected && data.amd?.divergence_indices?.length >= 2) {
        const [i1, i2] = data.amd.divergence_indices;
        if (ohlc[i1] && ohlc[i2] && oscData[i1] && oscData[i2]) {
          divSer.current = oscInst.current.addSeries(LineSeries, {
            color: C.red,
            lineWidth: 2,
            lineStyle: LineStyle.Dashed,
            crosshairMarkerVisible: false,
            lastValueVisible: false,
            priceLineVisible: false,
            title: "DIV",
          });
          divSer.current.setData([
            { time: ohlc[i1].time, value: oscData[i1].value },
            { time: ohlc[i2].time, value: oscData[i2].value },
          ]);
        }
      }
    }

    // Clear overlays
    seriesRefs.current.forEach((s) => {
      try { chartInst.current.removeSeries(s); } catch {
        // Some chart series can already be detached after rapid refresh/resizes.
      }
    });
    seriesRefs.current = [];

    const t0 = ohlc[0].time;
    const t1 = ohlc[ohlc.length - 1].time;
    const drawLegacyZones = false;

    const addH = (val, color, lw, ls, title) => {
      if (!val || val === 0) return;
      const s = chartInst.current.addSeries(LineSeries, {
        color, lineWidth: lw, lineStyle: ls,
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        priceLineVisible: false,
        title,
      });
      s.setData([{ time: t0, value: val }, { time: t1, value: val }]);
      seriesRefs.current.push(s);
    };

    if (chartLayers.structure) {
      addH(data.equilibrium, C.muted, 1, LineStyle.Dotted, "EQ 50%");
      addH(data.bos_level, C.blue, 2, LineStyle.LargeDashed, "BOS");
      addH(data.choch_level, C.purple, 2, LineStyle.LargeDashed, "CHoCH");
    }

    // 2. FVGs
    drawLegacyZones && (data.fvgs || []).forEach((fvg) => {
      if (fvg.filled) return;
      const isBull = fvg.direction === "bullish";
      const color  = isBull ? C.fvgBull : C.fvgBear;
      const ls     = fvg.mitigated ? LineStyle.SparseDotted : LineStyle.Dashed;
      const lbl    = fvg.mitigated
        ? (isBull ? "FVG▲~" : "FVG▼~")
        : (isBull ? "FVG▲"  : "FVG▼");
      addBand(chartInst.current, t0, t1, fvg.top, fvg.bottom, color, ls, lbl, seriesRefs);
    });

    // 3. Order Blocks
    drawLegacyZones && (data.order_blocks || []).forEach((ob) => {
      if (ob.broken) return;
      const color = ob.direction === "bullish" ? C.obBull : C.obBear;
      addBand(chartInst.current, t0, t1, ob.top, ob.bottom, color, LineStyle.Solid,
        ob.direction === "bullish" ? "OB▲" : "OB▼", seriesRefs);
    });

    // 4. OTE
    if (drawLegacyZones && data.ote_low != null && data.ote_high != null)
      addBand(chartInst.current, t0, t1, data.ote_high, data.ote_low, C.ote,
        LineStyle.LargeDashed, "OTE 62–79%", seriesRefs);

    // 6. Liquidity levels
    if (chartLayers.liquidity) {
      (data.liquidity_levels || []).filter(l => !l.swept).slice(0, 4).forEach((lvl) => {
        const isHigh = lvl.level_type === "equal_high";
        addH(lvl.price, isHigh ? C.amber : C.purple, 1, LineStyle.SparseDotted, "");
      });
      addH(data.nearest_buy_side, C.amber, 1, LineStyle.SparseDotted, "");
      addH(data.nearest_sell_side, C.purple, 1, LineStyle.SparseDotted, "");
    }

    // 7. Entry / SL / TP — only when signal is active and values are valid
    const hasSignal = data.signal !== "WAIT";
    if (chartLayers.tradePlan && hasSignal && Array.isArray(data.entry_zone)) {
      addBand(chartInst.current, t0, t1,
        data.entry_zone[1], data.entry_zone[0], C.blue, LineStyle.Dashed, "Entry", seriesRefs);
      const entryMid = (data.entry_zone[0] + data.entry_zone[1]) / 2;
      addH(entryMid, C.blue, 2, LineStyle.Solid, "Entry Mid");
    }
    // Guard: stop_loss and take_profit can be 0 on WAIT signal
    if (chartLayers.tradePlan && hasSignal && data.stop_loss && data.stop_loss !== 0) addH(data.stop_loss, C.red, 2, LineStyle.Dashed, "SL");
    if (chartLayers.tradePlan && hasSignal && data.take_profit && data.take_profit !== 0) addH(data.take_profit, C.green, 2, LineStyle.Dashed, "TP");

    // 8. Exec levels from scalp layer (spread-adjusted, dashed)
    if (chartLayers.tradePlan && hasSignal && data.exec_entry) addH(data.exec_entry, C.blue, 1, LineStyle.SparseDotted, "");
    if (chartLayers.tradePlan && hasSignal && data.exec_sl) addH(data.exec_sl, C.red, 1, LineStyle.SparseDotted, "");
    if (chartLayers.tradePlan && hasSignal && data.exec_tp) addH(data.exec_tp, C.green, 1, LineStyle.SparseDotted, "");

    // 9. AMD phase levels
    if (chartLayers.amd && data.amd?.manipulation?.extreme) {
      addH(data.amd.manipulation.extreme, C.amber, 2, LineStyle.Dashed, "Manip");
    }
    // MSS confirmed level (purple horizontal)
    if (chartLayers.amd && data.amd?.mss_level) {
      addH(data.amd.mss_level, "#d070ff", 2, LineStyle.LargeDashed, "MSS Level");
    }
    if (chartLayers.amd && data.amd?.manipulation_sl) {
      addH(data.amd.manipulation_sl, C.red, 1, LineStyle.SparseDotted, "AMD SL", false);
    }
    if (chartLayers.amd && data.amd?.accumulation_range) {
      addH(data.amd.accumulation_range.high, C.green, 1, LineStyle.Dotted, "Acc H");
      addH(data.amd.accumulation_range.low,  C.green, 1, LineStyle.Dotted, "Acc L");
    }
    if (chartLayers.amd && data.amd?.entry_zone_low && data.amd?.entry_zone_high) {
      addBand(chartInst.current, t0, t1,
        data.amd.entry_zone_high, data.amd.entry_zone_low, C.blue, LineStyle.SparseDotted, "AMD Entry", seriesRefs);
    }

    // 10. Nearest liquidity targets

    // 11. Swing markers
    const markers = [];
    if (chartLayers.structure) {
      (data.swing_highs || []).slice(-4).forEach((s) => {
        const time = ohlc[s.index]?.time;
        if (!time || !s.label) return;
        markers.push({ time, position: "aboveBar", color: s.label === "HH" ? C.red : "#5c1428", shape: "arrowDown", text: s.label });
      });
      (data.swing_lows || []).slice(-4).forEach((s) => {
        const time = ohlc[s.index]?.time;
        if (!time || !s.label) return;
        markers.push({ time, position: "belowBar", color: s.label === "LL" ? C.green : "#145c3c", shape: "arrowUp", text: s.label });
      });
    }
    if (chartLayers.amd && data.amd?.manipulation?.candle_index != null) {
      const mssTime = ohlc[data.amd.manipulation.candle_index]?.time;
      if (mssTime) {
        const isBearhunt = data.amd.manipulation.direction === "bearish_hunt";
        markers.push({
          time: mssTime,
          position: isBearhunt ? "aboveBar" : "belowBar",
          color: "#d070ff",
          shape: isBearhunt ? "arrowDown" : "arrowUp",
          text: "MANIP",
        });
      }
    }
    // MSS level marker (confirmed structural shift candle)
    if (chartLayers.amd && data.amd?.mss_detected && data.amd?.mss_level) {
      // Find closest candle to MSS level (proxy: scan post-manip candles)
      const manipIdx = data.amd?.manipulation?.candle_index ?? 0;
      let bestIdx = -1, bestDiff = Infinity;
      for (let i = manipIdx + 1; i < ohlc.length; i++) {
        // MSS for bearish = candle whose high ≈ mss_level
        const ref = data.amd.manipulation?.direction === "bearish_hunt"
          ? ohlc[i]?.high ?? 0
          : ohlc[i]?.low ?? 0;
        const diff = Math.abs(ref - data.amd.mss_level);
        if (diff < bestDiff) { bestDiff = diff; bestIdx = i; }
        if (i > manipIdx + 30) break;
      }
      if (bestIdx >= 0 && ohlc[bestIdx]) {
        const isBearhunt = data.amd.manipulation?.direction === "bearish_hunt";
        markers.push({
          time: ohlc[bestIdx].time,
          position: isBearhunt ? "aboveBar" : "belowBar",
          color: "#a040ff",
          shape: isBearhunt ? "arrowDown" : "arrowUp",
          text: "MSS",
        });
      }
    }
    // Divergence pivot markers
    if (chartLayers.amd && data.amd?.divergence_detected && data.amd?.divergence_indices?.length >= 2) {
      const [, di2] = data.amd.divergence_indices;
      const isBearhunt = data.amd.manipulation?.direction === "bearish_hunt";
      if (ohlc[di2]) {
        markers.push({
          time: ohlc[di2].time,
          position: isBearhunt ? "aboveBar" : "belowBar",
          color: C.red,
          shape: isBearhunt ? "arrowDown" : "arrowUp",
          text: "DIV",
        });
      }
    }
    if (markers.length) {
      markers.sort((a, b) => a.time - b.time);
      if (!markerApi.current) { markerApi.current = createSeriesMarkers(candleSer.current, markers); }
      else { markerApi.current.setMarkers(markers); }
    } else if (markerApi.current) { markerApi.current.setMarkers([]); }

    chartInst.current.timeScale().fitContent();
    oscInst.current?.timeScale().fitContent();
    setTimeout(() => {
      drawOverlay(
        canvasRef.current,
        chartInst.current,
        candleSer.current,
        chartLayersRef.current.amd ? data.amd : null,
        ohlc,
        sessRef.current,
      );
    }, 60);
  }, [data, chartLayers]);

  useEffect(() => {
    const chart = chartInst.current;
    orderLinesRef.current.forEach(({ series }) => {
      try { chart?.removeSeries(series); } catch {
        // Chart refreshes can detach a series before this effect runs.
      }
    });
    orderLinesRef.current = [];

    const candles = ohlcRef.current;
    if (!chart || candles.length < 2 || !data?.symbol) return undefined;

    const matchingOrders = openOrders.filter(
      p => p.status === "open" && p.mode === "paper"
        && p.symbol === data.symbol && p.timeframe === data.timeframe,
    );
    const firstTime = candles[0].time;
    const lastTime = candles[candles.length - 1].time;
    matchingOrders.forEach(position => {
      [["sl", position.sl, C.red], ["tp", position.tp, C.green]].forEach(([level, price, color]) => {
        if (!Number.isFinite(Number(price)) || Number(price) <= 0) return;
        const series = chart.addSeries(LineSeries, {
          color,
          lineWidth: 2,
          lineStyle: LineStyle.Dashed,
          crosshairMarkerVisible: false,
          lastValueVisible: true,
          priceLineVisible: false,
          title: level.toUpperCase(),
        });
        series.setData([
          { time: firstTime, value: Number(price) },
          { time: lastTime, value: Number(price) },
        ]);
        orderLinesRef.current.push({
          positionId: position.id,
          position,
          symbol: position.symbol,
          timeframe: position.timeframe,
          level,
          price: Number(price),
          series,
          firstTime,
          lastTime,
          barTime: data.candles?.[data.candles.length - 1]?.time,
        });
      });
    });

    return () => {
      orderLinesRef.current.forEach(({ series }) => {
        try { chart.removeSeries(series); } catch {
          // Ignore series already removed during a chart refresh.
        }
      });
      orderLinesRef.current = [];
    };
  }, [openOrders, data?.symbol, data?.timeframe, data?.candles]);

  useEffect(() => {
    const container = chartRef.current;
    if (!container) return undefined;

    const positionFromEvent = (event) => {
      const bounds = container.getBoundingClientRect();
      return event.clientY - bounds.top;
    };
    const onPointerDown = (event) => {
      if (event.button !== 0 || !candleSer.current || orderDragRef.current) return;
      const y = positionFromEvent(event);
      const line = orderLinesRef.current
        .map(candidate => ({
          candidate,
          coordinate: candleSer.current.priceToCoordinate(candidate.price),
        }))
        .filter(({ coordinate }) => coordinate != null)
        .map(item => ({ ...item, distance: Math.abs(item.coordinate - y) }))
        .filter(item => item.distance <= 10)
        .sort((a, b) => a.distance - b.distance)[0]?.candidate;
      if (!line) return;

      event.preventDefault();
      event.stopPropagation();
      orderDragRef.current = { line, pointerId: event.pointerId };
      container.setPointerCapture(event.pointerId);
      container.style.cursor = "ns-resize";
    };
    const onPointerMove = (event) => {
      const drag = orderDragRef.current;
      if (!drag) {
        const y = positionFromEvent(event);
        const overOrderLine = orderLinesRef.current.some(candidate => {
          const coordinate = candleSer.current?.priceToCoordinate(candidate.price);
          return coordinate != null && Math.abs(coordinate - y) <= 10;
        });
        container.style.cursor = overOrderLine ? "ns-resize" : "";
        return;
      }
      if (event.pointerId !== drag.pointerId || !candleSer.current) return;

      const price = candleSer.current.coordinateToPrice(positionFromEvent(event));
      if (price == null || !Number.isFinite(price) || price <= 0) return;
      const roundedPrice = Number(price.toFixed(priceDecimals(drag.line.symbol)));
      drag.line.price = roundedPrice;
      drag.line.series.setData([
        { time: drag.line.firstTime, value: roundedPrice },
        { time: drag.line.lastTime, value: roundedPrice },
      ]);
      event.preventDefault();
      event.stopPropagation();
    };
    const onPointerUp = (event) => {
      const drag = orderDragRef.current;
      if (!drag || event.pointerId !== drag.pointerId) return;
      orderDragRef.current = null;
      container.style.cursor = "";
      if (container.hasPointerCapture(event.pointerId)) {
        container.releasePointerCapture(event.pointerId);
      }
      stageOrderLevel(drag.line.position, drag.line.level, drag.line.price);
    };

    container.addEventListener("pointerdown", onPointerDown, true);
    container.addEventListener("pointermove", onPointerMove, true);
    container.addEventListener("pointerup", onPointerUp, true);
    container.addEventListener("pointercancel", onPointerUp, true);
    return () => {
      container.removeEventListener("pointerdown", onPointerDown, true);
      container.removeEventListener("pointermove", onPointerMove, true);
      container.removeEventListener("pointerup", onPointerUp, true);
      container.removeEventListener("pointercancel", onPointerUp, true);
      container.style.cursor = "";
      orderDragRef.current = null;
    };
  }, [stageOrderLevel, openOrders, data?.symbol, data?.timeframe, data?.candles]);

  // ── Higher-timeframe high / low lines ───────────────────────────────────────
  useEffect(() => {
    const chart = chartInst.current;
    levelSer.current.forEach((s) => {
      try { chart?.removeSeries(s); } catch {
        // Series can already be detached after a quick refresh.
      }
    });
    levelSer.current = [];

    const ohlc = ohlcRef.current;
    if (!chart || ohlc.length < 2 || !levelTfs.length || htf.symbol !== data?.symbol) return;

    const n      = ohlc.length;
    const tEnd   = ohlc[n - 1].time;
    const nowMs  = Date.now();
    const nowSec = nowMs / 1000 + chartOffsetSec(nowMs, serverTime);

    const addLevel = (bar, tf, current) => {
      let i = ohlc.findIndex(c => c.time >= bar.time);
      if (i < 0 || i > n - 2) i = n - 2;          // period starts past the chart's last bar
      [["H", bar.high], ["L", bar.low]].forEach(([, value]) => {
        const s = chart.addSeries(LineSeries, {
          color: LEVEL_COLORS[tf],
          lineWidth: current ? 1 : 2,
          lineStyle: current ? LineStyle.Dotted : LineStyle.Dashed,
          crosshairMarkerVisible: false,
          lastValueVisible: false,
          priceLineVisible: false,
          title: "",
          autoscaleInfoProvider: () => null,      // far-away weekly levels must not squash the chart
        });
        s.setData([{ time: ohlc[i].time, value }, { time: tEnd, value }]);
        levelSer.current.push(s);
      });
    };

    levelTfs.filter(t => TF_SEC[t] > TF_SEC[chartTf]).forEach((t) => {
      const raw = htf.byTf[t === "W1" ? "D1" : t];
      if (!raw?.length) return;
      let bars = raw.map(c => ({ time: Math.floor(Date.parse(c.time) / 1000), high: c.high, low: c.low }));
      if (t === "W1") bars = groupWeekly(bars);
      const { prev, cur } = htfLevels(bars, t, nowSec);
      if (prev) addLevel(prev, t, false);
      if (cur && levelCurrent) addLevel(cur, t, true);
    });
  }, [htf, levelTfs, levelCurrent, data, chartTf, serverTime]);

  // ── Derived ─────────────────────────────────────────────────────────────────
  const trendColor  = !data ? "gray" : data.trend === "BULLISH" ? "green" : data.trend === "BEARISH" ? "red" : "gray";
  const signalColor = !data ? "gray" : data.signal === "BUY" ? "green" : data.signal === "SELL" ? "red" : "amber";
  const manualBuyAvailable = !!manualTradeLevels("BUY", data);
  const manualSellAvailable = !!manualTradeLevels("SELL", data);
  const amdPhaseColor = !data?.amd ? "gray" : data.amd.phase === "ACCUMULATION" ? "green" : data.amd.phase === "MANIPULATION" ? "amber" : data.amd.phase === "DISTRIBUTION" ? "blue" : "gray";
  const confColor   = !data ? C.muted : data.confidence > 0.65 ? C.green : data.confidence > 0.45 ? C.amber : C.red;
  const entryMid    = data?.entry_zone?.length === 2 ? ((data.entry_zone[0] + data.entry_zone[1]) / 2).toFixed(5) : null;
  const riskPip     = data ? pipDiff(data.current_price, data.stop_loss, data.symbol) : "—";
  const rewardPip   = data ? pipDiff(data.take_profit, data.current_price, data.symbol) : "—";

  // Build check rows — failed first so they're immediately visible
  const allChecks = (() => {
    if (!data?.scalp_filter) return [];
    const failed = (data.scalp_filter.failed_checks || []).map(l => ({ label: l, passed: false }));
    const passed = (data.scalp_filter.passed_checks || []).map(l => ({ label: l, passed: true }));
    return [...failed, ...passed].map(({ label, passed }) => {
      const parenIdx = label.indexOf("(");
      const name   = parenIdx > -1 ? label.slice(0, parenIdx).trim() : label;
      const detail = parenIdx > -1 ? label.slice(parenIdx + 1, -1)   : null;
      return { label: name.replace(/_/g, " "), detail, passed };
    });
  })();

  // PDZ gate detection — v4 engine uses "hard suppression active" wording
  const pdZgateBlocked = data?.warnings?.some(
    w => w.toLowerCase().includes("premium zone") || w.toLowerCase().includes("discount zone") || w.toLowerCase().includes("hard suppression")
  );

  const scrollToSection = (id) => document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });

  // ── Render ──────────────────────────────────────────────────────────────────
  return (
    <div className="dashboard-shell">
      <aside className="dashboard-sidebar">
        <div className="sidebar-brand"><span className="sidebar-brand-mark">S</span><span>SMC<br />ENGINE</span></div>
        <div className="sidebar-label">WORKSPACE</div>
        <button className="sidebar-link active" onClick={() => scrollToSection("dashboard-workspace")}>
          <span>▦</span> Dashboard
        </button>
        <button className="sidebar-link" onClick={() => scrollToSection("market-bot-panel")}>
          <span>◉</span> Market Bot
        </button>
        <button className="sidebar-link" onClick={() => scrollToSection("analysis-details")}>
          <span>⌁</span> Analysis
        </button>
        <div className="sidebar-note">
          <span className="status-dot" />
          Paper trading
          <small>Orders are simulated</small>
        </div>
      </aside>
      <main className="dashboard-main">

      {/* ── Header ── */}
      <div className="dashboard-topbar" style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 16 }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <h1 style={{ margin: 0, fontSize: 20, fontWeight: 800, letterSpacing: -0.5 }}>SMC Engine</h1>
            <Badge label="v4.0" color="blue" small />
            {data && <Badge label={data.mt5_connected ? "● MT5 Live" : "○ Mock"} color={data.mt5_connected ? "green" : "gray"} small />}
            {riskData?.halted && <Badge label="⛔ HALTED" color="red" small />}
          </div>
          <p style={{ margin: "3px 0 0", fontSize: 12, color: C.muted }}>
            Smart Money Concepts · Structure · Liquidity · Displacement · Execution · Risk
          </p>
        </div>
        <div style={{ textAlign: "right", fontSize: 11, color: C.muted }}>
          {lastUpdate && <div>Updated: {lastUpdate}</div>}
          {data && <div style={{ color: C.muted2 }}>{data.candles?.length} candles · {data.timeframe}</div>}
          {riskData && (
            <div style={{ color: riskData.daily_pnl >= 0 ? C.green : C.red, fontWeight: 700, fontFamily: "monospace" }}>
              Daily P&L: {riskData.daily_pnl >= 0 ? "+" : ""}{riskData.daily_pnl_pct?.toFixed(2)}%
            </div>
          )}
        </div>
      </div>

      {/* ── Controls ── */}
      <div className="dashboard-controls" style={{
        display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center",
        background: C.surface, borderRadius: 10, padding: "10px 14px",
        border: `1px solid ${C.border}`, marginBottom: 14,
      }}>
        <select value={symbol} onChange={e => setSymbol(e.target.value)} style={selSt}>
          {SYMBOL_OPTIONS.map(s => <option key={s}>{s}</option>)}
        </select>

        <div style={{ display: "flex", gap: 3 }}>
          {TF_OPTIONS.map(t => (
            <button key={t} onClick={() => setTf(t)} style={{
              ...btnSt,
              background: tf === t ? C.blue : "transparent",
              color:      tf === t ? "#fff" : C.muted2,
              border:     `1px solid ${tf === t ? C.blue : C.border}`,
            }}>{t}</button>
          ))}
        </div>

        <select value={count} onChange={e => setCount(Number(e.target.value))} style={{ ...selSt, width: 80 }}>
          {COUNT_OPTIONS.map(n => <option key={n} value={n}>{n}c</option>)}
        </select>

        {/* Spread input */}
        <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
          <input
            type="number" step="0.1" min="0" max="20"
            placeholder="Spread"
            value={spread}
            onChange={e => setSpread(e.target.value)}
            style={{
              ...selSt, width: 72,
              color: spread ? C.text : C.muted,
            }}
          />
          <span style={{ fontSize: 10, color: C.muted }}>pips</span>
        </div>

        {/* Levels dropdown: HTF high/low + session breaks */}
        <div ref={levelsBox} style={{ position: "relative" }}>
          {(() => {
            const active = levelTfs.length > 0 || showSessions;
            return (
              <button onClick={() => setLevelsOpen(o => !o)} style={{
                ...btnSt,
                background: active ? C.blueDim : "transparent",
                color:      active ? C.blue : C.muted2,
                border:     `1px solid ${active ? C.blue : C.border}`,
              }}>
                Levels{active ? ` (${levelTfs.length + (showSessions ? 1 : 0)})` : ""} ▾
              </button>
            );
          })()}
          {levelsOpen && (
            <div style={{
              position: "absolute", top: "calc(100% + 6px)", left: 0, zIndex: 50, width: 240,
              background: C.surface2, border: `1px solid ${C.border2}`, borderRadius: 10,
              padding: 12, boxShadow: "0 10px 28px rgba(0,0,0,0.5)",
            }}>
              <div style={ddLabelSt}>High / Low of timeframe</div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 6, marginBottom: 8 }}>
                {LEVEL_TFS.map((t) => {
                  const disabled = TF_SEC[t] <= TF_SEC[tf];
                  const checked  = levelTfs.includes(t);
                  return (
                    <label key={t} title={disabled ? `Not shown on a ${tf} chart — pick a lower chart timeframe` : undefined}
                      style={{ ...chkSt, opacity: disabled ? 0.35 : 1, cursor: disabled ? "not-allowed" : "pointer" }}>
                      <input type="checkbox" checked={checked} disabled={disabled} style={{ accentColor: LEVEL_COLORS[t] }}
                        onChange={() => setLevelTfs(cur => (checked ? cur.filter(x => x !== t) : [...cur, t]))} />
                      <span style={{ color: checked ? LEVEL_COLORS[t] : C.muted2, fontWeight: checked ? 700 : 400 }}>{t}</span>
                    </label>
                  );
                })}
              </div>
              <label style={{ ...chkSt, fontSize: 11 }}>
                <input type="checkbox" checked={levelCurrent} onChange={e => setLevelCurrent(e.target.checked)} style={{ accentColor: C.blue }} />
                Also show current (forming) candle
              </label>
              <div style={{ fontSize: 10, color: C.muted, marginTop: 4, lineHeight: 1.4 }}>
                Dashed = previous candle (PDH / PDL, PWH / PWL…) · dotted = current
              </div>

              <div style={{ borderTop: `1px solid ${C.border2}`, margin: "10px 0" }} />

              <div style={ddLabelSt}>Sessions</div>
              <label style={{ ...chkSt, opacity: TF_SEC[tf] >= TF_SEC.D1 ? 0.35 : 1 }}>
                <input type="checkbox" checked={showSessions} onChange={e => setShowSessions(e.target.checked)}
                  disabled={TF_SEC[tf] >= TF_SEC.D1} style={{ accentColor: C.blue }} />
                Show session breaks
              </label>
              <div style={{ display: "flex", gap: 8, marginTop: 5, fontSize: 10 }}>
                {SESSIONS.map(s => <span key={s.name} style={{ color: s.color }}>│ {s.name}</span>)}
              </div>

              {levelsErr && <div style={{ marginTop: 8, fontSize: 11, color: C.red }}>✗ Levels: {levelsErr}</div>}
            </div>
          )}
        </div>

        <label style={chkSt}>
          <input type="checkbox" checked={useMock} onChange={e => setMock(e.target.checked)} style={{ accentColor: C.blue }} />
          Mock
        </label>
        <label style={chkSt}>
          <input type="checkbox" checked={autoRefresh} onChange={e => setAuto(e.target.checked)} style={{ accentColor: C.blue }} />
          Auto 30s
        </label>

        <div style={{ display: "flex", alignItems: "center", gap: 6, marginLeft: 4 }}>
          <span style={{ fontSize: 10, color: C.muted2, fontWeight: 700 }}>MANUAL PAPER</span>
          <button
            onClick={() => placeManualPaperTrade("BUY")}
            disabled={manualBusy || !manualBuyAvailable}
            style={{
              ...btnSt, color: C.green, background: C.greenDim,
              border: `1px solid ${C.green}55`,
              opacity: manualBusy || !manualBuyAvailable ? 0.45 : 1,
            }}
          >{manualBusy ? "…" : "Buy"}</button>
          <button
            onClick={() => placeManualPaperTrade("SELL")}
            disabled={manualBusy || !manualSellAvailable}
            style={{
              ...btnSt, color: C.red, background: C.redDim,
              border: `1px solid ${C.red}55`,
              opacity: manualBusy || !manualSellAvailable ? 0.45 : 1,
            }}
          >{manualBusy ? "…" : "Sell"}</button>
        </div>

        <button onClick={fetchData} disabled={loading} style={{
          ...btnSt, marginLeft: "auto",
          background: loading ? C.surface2 : C.blue, color: "#fff",
          border: "none", padding: "7px 16px", fontSize: 12, fontWeight: 700,
          opacity: loading ? 0.6 : 1,
        }}>
          {loading ? "Analyzing…" : "⟳ Analyze"}
        </button>
      </div>
      {manualMessage && (
        <div style={{
          margin: "-5px 0 12px", fontSize: 11, lineHeight: 1.4,
          color: manualMessage.type === "success" ? C.green : C.red,
        }}>{manualMessage.text}</div>
      )}

      {/* ── Bot ── */}
      <BotPanel
        onClosePosition={closeOrder}
        onUpdatePosition={updateOrder}
        onShowPosition={showPositionOnChart}
        orderDrafts={orderDrafts}
        setOrderDrafts={setOrderDrafts}
      />

      {err && (
        <div style={{
          background: C.redDim, color: "#ffb3c0", padding: "10px 14px",
          borderRadius: 8, marginBottom: 14, fontSize: 12, border: `1px solid ${C.red}22`,
        }}>
          ✗ {err} — ensure <code style={{ background: C.surface, padding: "1px 5px", borderRadius: 4 }}>uvicorn api:app --reload --port 8000</code> is running.
        </div>
      )}

      <section className="workspace-grid" id="dashboard-workspace">
        <div className="workspace-chart">
      {/* ── Chart ── */}
      <div id="trade-chart" className="chart-panel" style={{ background: C.bg, borderRadius: 12, border: `1px solid ${C.border}`, overflow: "hidden", marginBottom: 14, position: "relative" }}>
        {data && (
          <div style={{
            display: "flex", gap: 12, padding: "9px 14px",
            borderBottom: `1px solid ${C.border}`, flexWrap: "wrap", alignItems: "center",
          }}>
            <span style={{ fontSize: 15, fontWeight: 800, fontFamily: "monospace" }}>{data.symbol}</span>
            <span style={{ fontSize: 15, fontWeight: 700, fontFamily: "monospace", color: C.blue }}>{fmtPrice(data.current_price, data.symbol)}</span>
            <Badge label={data.trend} color={trendColor} />
            {data.last_event && <Badge label={data.last_event} color="blue" />}
            <Badge label={data.signal} color={signalColor} />
            {data.amd?.phase && <Badge label={`AMD ${data.amd.phase}`} color={amdPhaseColor} />}
            {data.amd?.mss_detected && <Badge label="MSS" color="purple" small />}
            {data.amd?.divergence_detected && <Badge label="DIVERGENCE" color="red" small />}
            {data.setup_type && <Badge label={data.setup_type.replace(/_/g, " ")} color="purple" small />}
            {/* Session badge — coloured by liquidity */}
            {data.scalp_filter?.session && (
              <Badge label={data.scalp_filter.session} color={sessionColor(data.scalp_filter.session)} small />
            )}
            <span style={{ fontSize: 11, color: C.muted, fontFamily: "monospace" }}>
              {(data.confidence * 100).toFixed(0)}% conf · {data.confluence_score}/100
            </span>
            {data.displacement_direction && (
              <span style={{ fontSize: 11, fontFamily: "monospace", color: data.displacement_direction === "bullish" ? C.green : C.red }}>
                ⇒ {data.displacement_direction} ({(data.displacement_strength * 100).toFixed(0)}%)
              </span>
            )}
            {/* ready_to_trade badge */}
            {data.signal !== "WAIT" && (
              <Badge
                label={data.ready_to_trade ? "✓ READY" : "⛔ BLOCKED"}
                color={data.ready_to_trade ? "green" : "red"}
                small
              />
            )}
            <div ref={chartLayersBox} style={{ position: "relative", marginLeft: "auto" }}>
              <button
                onClick={() => setChartLayersOpen((open) => !open)}
                aria-expanded={chartLayersOpen}
                style={{
                  ...btnSt,
                  color: C.muted2,
                  border: `1px solid ${C.border}`,
                  background: chartLayersOpen ? C.surface2 : "transparent",
                  whiteSpace: "nowrap",
                }}
              >
                Layers ({Object.values(chartLayers).filter(Boolean).length}) ▾
              </button>
              {chartLayersOpen && (
                <div style={{
                  position: "absolute", top: "calc(100% + 6px)", right: 0, zIndex: 50, width: 190,
                  background: C.surface2, border: `1px solid ${C.border2}`, borderRadius: 10,
                  padding: 12, boxShadow: "0 10px 28px rgba(0,0,0,0.5)",
                }}>
                  <div style={ddLabelSt}>Chart layers</div>
                  {[
                    ["tradePlan", "Trade plan"],
                    ["amd", "AMD zones & signals"],
                    ["structure", "Structure & swings"],
                    ["liquidity", "Liquidity levels"],
                  ].map(([key, label]) => (
                    <label key={key} style={{ ...chkSt, display: "flex", margin: "8px 0" }}>
                      <input
                        type="checkbox"
                        checked={chartLayers[key]}
                        onChange={(e) => setChartLayers((current) => ({
                          ...current,
                          [key]: e.target.checked,
                        }))}
                        style={{ accentColor: C.blue }}
                      />
                      {label}
                    </label>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {/* Keep the legend limited to layers that are currently visible. */}
        <div style={{
          display: "flex", gap: 14, padding: "6px 14px",
          borderBottom: `1px solid ${C.border}`, flexWrap: "wrap", alignItems: "center",
        }}>
          <span style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 10, color: C.muted2 }}>
            <span style={{ color: C.green }}>▲</span>/<span style={{ color: C.red }}>▼</span> Price
          </span>
          {chartLayers.tradePlan && (
            <span style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 10, color: C.muted2 }}>
              <span style={{ color: C.blue }}>—</span> Entry
              <span style={{ color: C.green }}>—</span> TP
              <span style={{ color: C.red }}>—</span> SL
            </span>
          )}
          {chartLayers.amd && (
            <span style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 10, color: C.muted2 }}>
              <span style={{ color: C.amber }}>—</span> AMD
              <span style={{ color: C.purple }}>—</span> MSS
            </span>
          )}
          {chartLayers.structure && (
            <span style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 10, color: C.muted2 }}>
              <span style={{ color: C.blue }}>- -</span> Structure
            </span>
          )}
          {chartLayers.liquidity && (
            <span style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 10, color: C.muted2 }}>
              <span style={{ color: C.amber }}>···</span> Liquidity
            </span>
          )}
        </div>
        {orderActionMessage && (
          <div className={`order-chart-message ${orderActionMessage.type}`}>
            {orderActionMessage.text}
          </div>
        )}
        {data && openOrders.some(position => (
          position.symbol === data.symbol && position.timeframe === data.timeframe
        )) && (
          <div className="order-chart-hint">
            Drag an SL or TP line to stage a change, then click Update in Order Status.
          </div>
        )}
        {data && openOrders.length > 0 && !openOrders.some(position => (
          position.symbol === data.symbol && position.timeframe === data.timeframe
        )) && (
          <div className="order-chart-hint">
            Choose Chart on an open order to load its symbol and timeframe here.
          </div>
        )}
        <div ref={chartRef} style={{ width: "100%", height: 390 }} />
        <canvas
          ref={canvasRef}
          width={800}
          height={390}
          style={{
            position: "absolute",
            bottom: 120,
            left: 0,
            width: "100%",
            height: 390,
            pointerEvents: "none",
            zIndex: 10,
          }}
        />
        <div style={{ borderTop: `1px solid ${C.border}`, position: "relative" }}>
          <div style={{
            position: "absolute", top: 4, left: 10, zIndex: 5,
            fontSize: 9, color: C.muted, textTransform: "uppercase", letterSpacing: 1,
            display: "flex", alignItems: "center", gap: 6,
          }}>
            <span>Oscillator</span>
            {data?.amd?.divergence_detected && (
              <span style={{
                color: C.red, fontSize: 9, fontWeight: 700,
                border: `1px solid ${C.red}`, borderRadius: 3, padding: "0 4px",
              }}>
                DIVERGENCE
              </span>
            )}
          </div>
          <div ref={oscRef} style={{ width: "100%", height: 120 }} />
        </div>
      </div>
        </div>

        {data ? (
          <>
            <div className="workspace-center">
              <section className="overview-card execution-overview">
                <div className="overview-card-heading">
                  <span>Execution panel</span>
                  <Badge
                    label={data.ready_to_trade ? "READY" : data.signal === "WAIT" ? "WAIT" : "BLOCKED"}
                    color={data.ready_to_trade ? "green" : data.signal === "WAIT" ? "amber" : "red"}
                    small
                  />
                </div>
                <div className="execution-summary">
                  <div className="quick-action">
                    <span className="eyebrow">Signal</span>
                    <SignalGlyph signal={data.signal} />
                    <span className="quick-score">{data.confluence_score}<small>/100</small></span>
                    <Bar value={data.confidence * 100} color={confColor} />
                    <span className="quick-confidence">{fmtPct(data.confidence * 100)} confidence</span>
                  </div>
                  <div className="execution-levels">
                    <Stat label="Entry" value={fmtPrice(data.exec_entry || data.current_price, data.symbol)} color={C.blue} />
                    <Stat label="Stop loss" value={fmtPrice(data.exec_sl || data.stop_loss, data.symbol)} color={C.red} />
                    <Stat label="Take profit" value={fmtPrice(data.exec_tp || data.take_profit, data.symbol)} color={C.green} />
                    <Stat label="Risk / reward" value={data.rr_ratio > 0 ? `1 : ${data.rr_ratio}` : "—"} color={C.amber} />
                  </div>
                </div>
                <div className="execution-footer">
                  <span>{data.sizing?.allowed ? `${data.sizing.lots?.toFixed(2)} lots` : "Sizing unavailable"}</span>
                  <span>{data.sizing?.allowed ? `Risk $${data.sizing.risk_amount?.toFixed(2)}` : data.sizing?.reason}</span>
                </div>
                {!!data.block_reasons?.length && (
                  <div className="overview-alert">{data.block_reasons[0]}</div>
                )}
              </section>

              <section className="overview-card ote-overview">
                <div className="overview-card-heading"><span>Optimal trade entry</span><span className="eyebrow">OTE ZONE</span></div>
                <div className="ote-range">
                  <div><span>Low</span><strong>{fmtPrice(data.ote_low, data.symbol)}</strong></div>
                  <i />
                  <div><span>High</span><strong>{fmtPrice(data.ote_high, data.symbol)}</strong></div>
                </div>
                <Badge label={data.inside_ote ? "Price inside OTE" : "Price outside OTE"} color={data.inside_ote ? "green" : "gray"} small />
              </section>

              <section className="overview-card amd-overview">
                <div className="overview-card-heading">
                  <span>AMD setup / execution log</span>
                  {data.amd?.phase && <Badge label={data.amd.phase} color={amdPhaseColor} small />}
                </div>
                <div className="overview-log">
                  {data.amd?.accumulation_range && <div><span>•</span> Accumulation range: {fmtPrice(data.amd.accumulation_range.low, data.symbol)} – {fmtPrice(data.amd.accumulation_range.high, data.symbol)}</div>}
                  {data.amd?.manipulation && <div><span>•</span> Manipulation: {data.amd.manipulation.direction?.replace(/_/g, " ")} · {fmtPrice(data.amd.manipulation.extreme, data.symbol)}</div>}
                  {data.amd?.notes?.[0] && <div><span>•</span> {data.amd.notes[0]}</div>}
                  {!data.amd?.accumulation_range && !data.amd?.manipulation && <div>No completed AMD setup detected.</div>}
                </div>
              </section>
            </div>

            <aside className="workspace-right">
              <section className="overview-card risk-overview">
                <div className="overview-card-heading">
                  <span>Risk manager</span>
                  {riskData?.halted && <Badge label="HALTED" color="red" small />}
                </div>
                <div className="risk-metrics">
                  <Stat label="Trades today" value={riskData?.trades_today ?? "—"} color={C.text} />
                  <Stat label="Open trades" value={riskData?.open_trades ?? "—"} color={C.text} />
                  <Stat label="Daily P&L" value={riskData ? `${riskData.daily_pnl >= 0 ? "+" : ""}${fmtPct(riskData.daily_pnl_pct)}` : "—"} color={!riskData ? C.muted2 : riskData.daily_pnl >= 0 ? C.green : C.red} />
                  <Stat label="Loss budget left" value={riskData ? fmtPct((riskData.daily_loss_remaining_pct || 0) * 100) : "—"} color={C.amber} />
                </div>
                <div className="risk-budget-track"><span style={{ width: `${Math.min(100, Math.max(0, (riskData?.daily_loss_remaining_pct || 0) / 0.02 * 100))}%` }} /></div>
                {riskData?.halted && <div className="overview-alert">{riskData.halt_reason}</div>}
              </section>

              <section className="overview-card market-overview">
                <div className="overview-card-heading"><span>Market context</span><span className="eyebrow">{data.timeframe}</span></div>
                <div className="context-list">
                  <div><span>Trend</span><strong>{data.trend}</strong></div>
                  <div><span>Market structure</span><strong>{data.last_event || "—"}</strong></div>
                  <div><span>Session</span><strong>{data.scalp_filter?.session || "—"}</strong></div>
                  <div><span>Spread</span><strong>{data.scalp_filter?.spread_pips == null ? "—" : `${data.scalp_filter.spread_pips.toFixed(1)} pips`}</strong></div>
                  <div><span>FVGs / stop hunts</span><strong>{data.fvgs?.length || 0} / {data.stop_hunts?.length || 0}</strong></div>
                </div>
                {data.warnings?.[0] && <div className="overview-warning">{data.warnings[0]}</div>}
              </section>
            </aside>
          </>
        ) : (
          <div className="workspace-empty">
            {loading ? "Loading market analysis…" : "Market analysis will appear here when the API is connected."}
          </div>
        )}
      </section>

      {data && (
        <>
          {data.amd && (
            <div className="detail-grid detail-grid-amd" style={{ display: "grid", gridTemplateColumns: "minmax(320px, 1.15fr) minmax(280px, 0.85fr)", gap: 12, marginBottom: 12 }}>
              <AMDPhaseCard amd={data.amd} symbol={data.symbol || symbol} />
              <AMDSetupCard data={data} />
            </div>
          )}
          {/* ── Row 1: Signal · Entry · R/R · Structure ── */}
          <div className="detail-grid detail-grid-primary" style={{ display: "grid", gridTemplateColumns: "200px 1fr 1fr 1fr", gap: 12, marginBottom: 12 }}>

            {/* Signal */}
            <div style={cardSt}>
              <SecTitle>Signal</SecTitle>
              <SignalGlyph signal={data.signal} />
              <div style={{ marginTop: 10 }}>
                <Bar value={data.confidence * 100} color={confColor} />
                <div style={{ display: "flex", justifyContent: "space-between", marginTop: 4, fontSize: 10, color: C.muted2 }}>
                  <span>0%</span>
                  <span style={{ color: confColor, fontWeight: 700 }}>{(data.confidence * 100).toFixed(0)}%</span>
                  <span>100%</span>
                </div>
              </div>
              <div style={{ marginTop: 8, fontSize: 11, color: C.muted2 }}>
                Score <span style={{ color: C.text, fontWeight: 700 }}>{data.confluence_score}</span>/100
              </div>
              {/* PDZ gate — fixed: checks actual warning text */}
              {pdZgateBlocked && (
                <div style={{
                  marginTop: 8, fontSize: 10, padding: "3px 8px",
                  background: C.amberDim, color: C.amber, borderRadius: 6,
                }}>
                  ⚠ PDZ gate active
                </div>
              )}
            </div>

            {/* Entry zone */}
            <div style={cardSt}>
              <SecTitle>Entry Zone</SecTitle>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                <Stat label="Zone Low"  value={fmt(data.entry_zone?.[0])} color={C.blue}
                      sub={`${pipDiff(data.entry_zone?.[1], data.entry_zone?.[0], data.symbol)} wide`} />
                <Stat label="Zone High" value={fmt(data.entry_zone?.[1])} color={C.blue}
                      sub={`Mid: ${entryMid}`} />
              </div>
              <div style={{ marginTop: 10, display: "flex", gap: 6, flexWrap: "wrap" }}>
                <Badge label={data.inside_fvg ? "In FVG ✓" : "Not in FVG"} color={data.inside_fvg ? "green" : "gray"} small />
                <Badge label={data.inside_ote ? "In OTE ✓" : "Not in OTE"} color={data.inside_ote ? "purple" : "gray"} small />
                <Badge
                  label={`Retrace ${fmtPct(data.retracement_pct)}`}
                  color={data.retracement_strength === "weak" ? "green" : data.retracement_strength === "strong" ? "red" : "amber"}
                  small
                />
              </div>
            </div>

            {/* R/R */}
            <div style={cardSt}>
              <SecTitle>Risk / Reward</SecTitle>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                <Stat label="Stop Loss"   value={fmt(data.stop_loss)} color={C.red}   sub={`Risk ${riskPip}`} />
                <Stat label="Take Profit" value={data.take_profit && data.take_profit !== 0 ? fmt(data.take_profit) : "—"}
                      color={C.green} sub={`Reward ${rewardPip}`} />
              </div>
              <div style={{
                marginTop: 12, fontSize: 22, fontWeight: 800, fontFamily: "monospace",
                color: data.rr_ratio >= 2.5 ? C.green : data.rr_ratio >= 1.8 ? C.amber : C.red,
              }}>
                {data.rr_ratio > 0 ? `1 : ${data.rr_ratio}` : "—"}
              </div>
              {(() => {
                const rrTargets = data.amd?.rr_targets || computeRRTargets(
                  data.amd?.entry_zone_high || data.entry_zone?.[1],
                  data.amd?.manipulation_sl || data.stop_loss,
                );
                if (!rrTargets) return null;
                return (
                  <div style={{ display: "flex", flexDirection: "column", gap: 3, marginTop: 8 }}>
                    {Object.entries(rrTargets).map(([label, price]) => (
                      <div key={label} style={{ display: "flex", justifyContent: "space-between", fontSize: 10 }}>
                        <span style={{ color: C.muted }}>{label}</span>
                        <span style={{ color: C.green, fontFamily: "monospace" }}>{fmtPrice(price, data.symbol)}</span>
                      </div>
                    ))}
                  </div>
                );
              })()}
            </div>

            {/* Structure */}
            <div style={cardSt}>
              <SecTitle>Market Structure</SecTitle>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                <Stat label="Trend"      value={data.trend}         color={trendColor === "green" ? C.green : trendColor === "red" ? C.red : C.muted} mono={false} />
                <Stat label="Last Event" value={data.last_event || "—"} color={C.blue} mono={false} />
                <Stat label="Premium"    value={fmt(data.premium_zone)}  color={C.orange} />
                <Stat label="EQ 50%"     value={fmt(data.equilibrium)}   color={C.muted2} />
                <Stat label="Discount"   value={fmt(data.discount_zone)} color={C.blue} />
                <Stat label="BOS / CHoCH"
                      value={data.bos_level ? fmt(data.bos_level) : (data.choch_level ? fmt(data.choch_level) : "—")}
                      color={data.bos_level ? C.blue : C.purple} />
              </div>
            </div>
          </div>

          {/* ── Row 2: Liquidity · OTE · Displacement ── */}
          <div className="detail-grid detail-grid-three" style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12, marginBottom: 12 }}>
            <div style={cardSt}>
              <SecTitle>Liquidity Targets</SecTitle>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <Stat label="Buy-Side (EQH)" value={fmt(data.nearest_buy_side)} color={C.amber}
                      sub={data.nearest_buy_side ? `${pipDiff(data.nearest_buy_side, data.current_price, data.symbol)} above` : "None"} />
                <Stat label="Sell-Side (EQL)" value={fmt(data.nearest_sell_side)} color={C.purple}
                      sub={data.nearest_sell_side ? `${pipDiff(data.current_price, data.nearest_sell_side, data.symbol)} below` : "None"} />
              </div>
              <div style={{ marginTop: 10, fontSize: 10, color: C.muted }}>
                {data.liquidity_levels?.length || 0} levels · {data.stop_hunts?.length || 0} stop hunts
              </div>
            </div>

            <div style={cardSt}>
              <SecTitle>OTE Zone (Fib 62–79%)</SecTitle>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <Stat label="OTE Low"  value={fmt(data.ote_low)}  color={C.purple} />
                <Stat label="OTE High" value={fmt(data.ote_high)} color={C.purple}
                      sub={`Width: ${pipDiff(data.ote_high, data.ote_low, data.symbol)}`} />
              </div>
              <div style={{ marginTop: 8 }}>
                <Badge
                  label={data.inside_ote ? "✓ Inside OTE — optimal entry" : "Price outside OTE"}
                  color={data.inside_ote ? "purple" : "gray"} small
                />
              </div>
            </div>

            <div style={cardSt}>
              <SecTitle>Displacement</SecTitle>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <Stat label="Direction"
                      value={data.displacement_direction || "—"}
                      color={data.displacement_direction === "bullish" ? C.green : data.displacement_direction === "bearish" ? C.red : C.muted}
                      mono={false} />
                <Stat label="Strength"
                      value={fmtPct(data.displacement_strength * 100)}
                      color={data.displacement_strength > 0.65 ? C.green : data.displacement_strength > 0.4 ? C.amber : C.red} />
              </div>
              <div style={{ marginTop: 10 }}>
                <Bar value={data.displacement_strength * 100}
                     color={data.displacement_strength > 0.65 ? C.green : data.displacement_strength > 0.4 ? C.amber : C.red} />
              </div>
              <div style={{ marginTop: 8, fontSize: 10, color: C.muted }}>
                {data.fvgs?.length || 0} FVGs · {data.fvgs?.filter(f => !f.filled).length || 0} active
              </div>
            </div>
          </div>

          {/* ── NEW Row 3: Execution Layer (Scalp) ── */}
          <div className="detail-grid detail-grid-three" style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12, marginBottom: 12 }}>

            {/* Scalp verdict */}
            <div style={{
              ...cardSt,
              borderColor: data.ready_to_trade ? C.green : data.signal === "WAIT" ? C.border : C.red,
              borderWidth: 1,
            }}>
              <SecTitle>Execution Verdict</SecTitle>
              <div style={{
                fontSize: 15, fontWeight: 800,
                color: data.ready_to_trade ? C.green : C.red,
                marginBottom: 8,
              }}>
                {data.ready_to_trade ? "✓ READY TO TRADE" : data.signal === "WAIT" ? "— WAITING FOR SETUP" : "⛔ BLOCKED"}
              </div>
              {data.scalp_summary && data.signal !== "WAIT" && (
                <div style={{ fontSize: 11, color: C.muted2, lineHeight: 1.6, fontFamily: "monospace" }}>
                  {data.scalp_summary.replace(/[\u2705\u26D4]/g, "").trim()}
                </div>
              )}
              {data.signal !== "WAIT" && (data.block_reasons || []).filter(r => !r.startsWith("SMC signal")).length > 0 && (
                <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 4 }}>
                  {data.block_reasons.filter(r => !r.startsWith("SMC signal")).map((r, i) => (
                    <div key={i} style={{
                      fontSize: 10, color: C.amber, background: C.amberDim,
                      borderRadius: 4, padding: "2px 7px", fontFamily: "monospace",
                    }}>! {r}</div>
                  ))}
                </div>
              )}
            </div>

            {/* Spread-adjusted exec levels */}
            <div style={cardSt}>
              <SecTitle>Exec Levels (spread-adjusted)</SecTitle>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                <Stat label="Exec Entry" value={data.exec_entry ? fmt(data.exec_entry) : "—"} color={C.blue}
                      sub={data.exec_entry && data.current_price ? `Δ ${pipDiff(data.exec_entry, data.current_price, data.symbol)}` : ""} />
                <Stat label="Exec SL"    value={data.exec_sl ? fmt(data.exec_sl) : "—"} color={C.red}
                      sub={data.exec_sl && data.exec_entry ? `Risk ${pipDiff(data.exec_entry, data.exec_sl, data.symbol)}` : ""} />
                <Stat label="Exec TP"    value={data.exec_tp ? fmt(data.exec_tp) : "—"} color={C.green}
                      sub={data.exec_tp && data.exec_entry ? `Reward ${pipDiff(data.exec_tp, data.exec_entry, data.symbol)}` : ""} />
                <Stat label="Spread"     value={data.scalp_filter?.spread_pips != null ? `${data.scalp_filter.spread_pips.toFixed(1)}p` : "—"}
                      color={data.scalp_filter?.spread_pips > 1.5 ? C.red : C.muted2} />
              </div>
            </div>

            {/* Position sizing */}
            <div style={cardSt}>
              <SecTitle>Position Sizing</SecTitle>
              {data.sizing?.allowed ? (
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                  <Stat label="Lots"       value={data.sizing.lots?.toFixed(2)} color={C.text} />
                  <Stat label="Units"      value={data.sizing.units?.toLocaleString()} color={C.muted2} />
                  <Stat label="Risk $"     value={`$${data.sizing.risk_amount?.toFixed(2)}`} color={C.amber} />
                  <Stat label="Risk %"     value={fmtPct((data.sizing.risk_pct || 0) * 100)} color={C.amber} />
                  <Stat label="SL (pips)"  value={`${data.sizing.sl_pips?.toFixed(1)}p`} color={C.red} />
                  <Stat label="Adj SL"     value={fmt(data.sizing.sl_adjusted)} color={C.red} />
                </div>
              ) : (
                <div style={{ fontSize: 12, color: C.muted2 }}>
                  {data.sizing?.reason || "Not sized — signal is WAIT"}
                </div>
              )}
            </div>
          </div>

          {/* ── Risk Manager Status bar ── */}
          {riskData && (
            <div style={{
              ...cardSt, marginBottom: 12,
              borderColor: riskData.halted ? C.red : C.border,
              display: "flex", gap: 24, flexWrap: "wrap", alignItems: "center",
            }}>
              <div style={{ fontSize: 10, color: C.muted, textTransform: "uppercase", letterSpacing: 1 }}>Risk Manager</div>
              {[
                { label: "Daily P&L",       value: `${riskData.daily_pnl >= 0 ? "+" : ""}${riskData.daily_pnl_pct?.toFixed(2)}%`,
                  color: riskData.daily_pnl >= 0 ? C.green : C.red },
                { label: "Open Trades",     value: riskData.open_trades,       color: C.text },
                { label: "Consec. Losses",  value: riskData.consecutive_losses, color: riskData.consecutive_losses >= 2 ? C.amber : C.text },
                { label: "Trades Today",    value: riskData.trades_today,      color: C.muted2 },
                { label: "Loss Budget Left",value: fmtPct((riskData.daily_loss_remaining_pct || 0) * 100),
                  color: (riskData.daily_loss_remaining_pct || 0) < 0.005 ? C.red : C.muted2 },
              ].map(({ label, value, color }) => (
                <div key={label} style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                  <span style={{ fontSize: 10, color: C.muted, textTransform: "uppercase", letterSpacing: 1 }}>{label}</span>
                  <span style={{ fontSize: 13, fontWeight: 700, fontFamily: "monospace", color }}>{value}</span>
                </div>
              ))}
              {riskData.halted && (
                <div style={{ marginLeft: "auto", fontSize: 12, color: C.red, fontWeight: 700 }}>
                  ⛔ {riskData.halt_reason}
                </div>
              )}
            </div>
          )}

          {/* ── Analysis Tabs ── */}
          <div id="analysis-details" style={{ ...cardSt, marginBottom: 12 }}>
            <div className="analysis-tabs" style={{ display: "flex", gap: 4, marginBottom: 14, borderBottom: `1px solid ${C.border}`, paddingBottom: 0 }}>
              {[
                { id: "confluence", label: `Confluence (${data.reasons?.length || 0})` },
                { id: "execution",  label: `Exec Filter (${(data.scalp_filter?.passed_checks?.length || 0) + (data.scalp_filter?.failed_checks?.length || 0)})` },
                { id: "warnings",   label: `Warnings (${data.warnings?.length || 0})` },
                { id: "fvg",        label: `FVGs (${data.fvgs?.length || 0})` },
                { id: "hunts",      label: `Stop Hunts (${data.stop_hunts?.length || 0})` },
                { id: "obs",        label: `Order Blocks (${data.order_blocks?.length || 0})` },
              ].map((tab) => (
                <button key={tab.id} onClick={() => setTab(tab.id)} style={{
                  background: "none", border: "none", cursor: "pointer",
                  padding: "6px 12px", fontSize: 11, fontWeight: activeTab === tab.id ? 700 : 400,
                  color: activeTab === tab.id ? C.blue : C.muted2,
                  borderBottom: `2px solid ${activeTab === tab.id ? C.blue : "transparent"}`,
                  marginBottom: -1, transition: "all .15s",
                }}>{tab.label}</button>
              ))}
            </div>

            {/* Confluence */}
            {activeTab === "confluence" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {(data.reasons || []).map((r, i) => (
                  <div key={i} style={{ display: "flex", gap: 10, fontSize: 12, alignItems: "flex-start" }}>
                    <span style={{ color: C.green, fontWeight: 800, minWidth: 16, fontSize: 14, lineHeight: "18px" }}>✓</span>
                    <span style={{ color: C.text, lineHeight: 1.5 }}>{r}</span>
                  </div>
                ))}
                {!data.reasons?.length && <span style={{ color: C.muted, fontSize: 12 }}>No confluence met — signal is WAIT.</span>}
              </div>
            )}

            {/* NEW: Execution Filter tab */}
            {activeTab === "execution" && (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 24 }}>
                <div>
                  <div style={{ fontSize: 11, color: C.muted2, marginBottom: 8 }}>Filter Checks</div>
                  {allChecks.length === 0 && <span style={{ fontSize: 12, color: C.muted }}>No filter data — signal was WAIT.</span>}
                  {allChecks.map((c, i) => (
                    <CheckRow key={i} label={c.label} passed={c.passed} detail={c.detail} />
                  ))}
                  {(data.scalp_filter?.notes || []).map((n, i) => (
                    <div key={i} style={{ fontSize: 10, color: C.amber, marginTop: 6, fontFamily: "monospace" }}>ℹ {n}</div>
                  ))}
                </div>
                <div>
                  <div style={{ fontSize: 11, color: C.muted2, marginBottom: 8 }}>Microstructure Metrics</div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                    <div>
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: C.muted, marginBottom: 4 }}>
                        <span>ATR%</span>
                        <span style={{ fontFamily: "monospace" }}>{((data.scalp_filter?.atr_pct || 0) * 100).toFixed(4)}%</span>
                      </div>
                      <Bar
                        value={(data.scalp_filter?.atr_pct || 0) * 100 * 400}
                        color={data.scalp_filter?.atr_pct > 0.003 ? C.red : data.scalp_filter?.atr_pct > 0.00025 ? C.green : C.amber}
                      />
                    </div>
                    <div>
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: C.muted, marginBottom: 4 }}>
                        <span>Momentum</span>
                        <span style={{ fontFamily: "monospace" }}>{fmtPct((data.scalp_filter?.momentum_score || 0) * 100)}</span>
                      </div>
                      <Bar
                        value={(data.scalp_filter?.momentum_score || 0) * 100}
                        color={data.scalp_filter?.momentum_score >= 0.6 ? C.green : C.amber}
                      />
                    </div>
                    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginTop: 4 }}>
                      <Stat label="Session"  value={data.scalp_filter?.session || "—"} color={C.text} mono={false} />
                      <Stat label="Spread"   value={data.scalp_filter?.spread_pips != null ? `${data.scalp_filter.spread_pips.toFixed(1)}p` : "N/A"} color={C.muted2} />
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* Warnings */}
            {activeTab === "warnings" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {(data.warnings || []).map((w, i) => (
                  <div key={i} style={{ display: "flex", gap: 10, fontSize: 12, alignItems: "flex-start" }}>
                    <span style={{ color: C.amber, fontWeight: 800, minWidth: 16, fontSize: 14, lineHeight: "18px" }}>!</span>
                    <span style={{ color: C.text, lineHeight: 1.5 }}>{w}</span>
                  </div>
                ))}
                {!data.warnings?.length && <span style={{ color: C.muted, fontSize: 12 }}>No warnings.</span>}
              </div>
            )}

            {/* FVGs */}
            {activeTab === "fvg" && (
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11, fontFamily: "monospace" }}>
                  <thead>
                    <tr style={{ color: C.muted }}>
                      {["Direction","Top","Bottom","Size","Strength","Status"].map(h => (
                        <th key={h} style={{
                          padding: "4px 10px", textAlign: "left", fontWeight: 600,
                          borderBottom: `1px solid ${C.border}`, fontSize: 10,
                          textTransform: "uppercase", letterSpacing: 0.5,
                        }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {(data.fvgs || []).map((fvg, i) => {
                      const isJpy  = data.symbol?.toUpperCase().includes("JPY");
                      const size   = ((fvg.top - fvg.bottom) * (isJpy ? 100 : 10000)).toFixed(1);
                      const status = fvg.filled ? "Filled" : fvg.mitigated ? "Mitigated" : "Active";
                      const stCol  = fvg.filled ? C.muted : fvg.mitigated ? C.amber : C.green;
                      return (
                        <tr key={i} style={{ borderBottom: `1px solid ${C.border}`, opacity: fvg.filled ? 0.4 : 1 }}>
                          <td style={{ padding: "6px 10px", color: fvg.direction === "bullish" ? C.green : C.red, fontWeight: 700 }}>
                            {fvg.direction === "bullish" ? "▲ Bull" : "▼ Bear"}
                          </td>
                          <td style={{ padding: "6px 10px" }}>{fmt(fvg.top)}</td>
                          <td style={{ padding: "6px 10px" }}>{fmt(fvg.bottom)}</td>
                          <td style={{ padding: "6px 10px" }}>{size}p</td>
                          <td style={{ padding: "6px 10px" }}>
                            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                              <div style={{ width: 36, height: 3, background: C.border2, borderRadius: 99, overflow: "hidden" }}>
                                <div style={{ width: `${fvg.strength * 100}%`, height: "100%", background: fvg.strength > 0.6 ? C.green : C.amber, borderRadius: 99 }} />
                              </div>
                              <span style={{ color: fvg.strength > 0.6 ? C.green : C.muted2 }}>{(fvg.strength * 100).toFixed(0)}%</span>
                            </div>
                          </td>
                          <td style={{ padding: "6px 10px", color: stCol, fontWeight: 600 }}>{status}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}

            {/* Stop Hunts */}
            {activeTab === "hunts" && (
              <div>
                {!(data.stop_hunts?.length) && <span style={{ color: C.muted, fontSize: 12 }}>No stop hunts detected.</span>}
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  {(data.stop_hunts || []).map((h, i) => {
                    const isBull  = h.direction === "bullish_sweep";
                    const isFresh = i === data.stop_hunts.length - 1;
                    return (
                      <div key={i} style={{
                        background: C.surface2, border: `1px solid ${C.border}`,
                        borderRadius: 8, padding: "8px 14px", fontSize: 11, fontFamily: "monospace",
                      }}>
                        <div style={{ color: isBull ? C.green : C.red, fontWeight: 700, marginBottom: 2 }}>
                          {isBull ? "▲ Bullish Sweep" : "▼ Bearish Sweep"}{" "}
                          {isFresh && <Badge label="Recent" color={isBull ? "green" : "red"} small />}
                        </div>
                        <div style={{ color: C.muted2 }}>Swept @ <span style={{ color: C.text }}>{fmt(h.swept_level)}</span></div>
                        <div style={{ color: C.muted }}>Candle #{h.candle_index}</div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Order Blocks */}
            {activeTab === "obs" && (
              <div>
                {!(data.order_blocks?.length) && <span style={{ color: C.muted, fontSize: 12 }}>No order blocks detected.</span>}
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  {(data.order_blocks || []).map((ob, i) => (
                    <div key={i} style={{
                      background: C.surface2, border: `1px solid ${ob.direction === "bullish" ? C.tealDim : C.orangeDim}`,
                      borderRadius: 8, padding: "8px 14px", fontSize: 11, fontFamily: "monospace",
                    }}>
                      <div style={{ color: ob.direction === "bullish" ? C.teal : C.orange, fontWeight: 700, marginBottom: 4 }}>
                        {ob.direction === "bullish" ? "▲ Bullish OB" : "▼ Bearish OB"}
                        {ob.broken && <>{" "}<Badge label="Broken" color="gray" small /></>}
                      </div>
                      <div style={{ color: C.muted2 }}>Top: <span style={{ color: C.text }}>{fmt(ob.top)}</span></div>
                      <div style={{ color: C.muted2 }}>Bottom: <span style={{ color: C.text }}>{fmt(ob.bottom)}</span></div>
                      <div style={{ color: C.muted, marginTop: 2 }}>Width: {pipDiff(ob.top, ob.bottom, data.symbol)}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </>
      )}

      <p style={{ marginTop: 14, fontSize: 10, color: C.muted, textAlign: "center" }}>
        SMC Engine v4.0 · Not financial advice · Always manage risk · {data?.symbol} {data?.timeframe}
      </p>
      </main>
    </div>
  );
}

// ── Shared styles ──────────────────────────────────────────────────────────────
const selSt = {
  background: "#1a1f2d", color: "#dde3f0",
  border: "1px solid #252c3a", borderRadius: 7,
  padding: "6px 10px", fontSize: 12, cursor: "pointer",
};
const btnSt = {
  padding: "5px 10px", borderRadius: 7,
  fontSize: 11, fontWeight: 600, cursor: "pointer", transition: "all .15s",
};
const chkSt = {
  display: "flex", alignItems: "center", gap: 5,
  fontSize: 12, color: "#7a84a0", cursor: "pointer",
};
const ddLabelSt = {
  fontSize: 10, color: "#5a6480", textTransform: "uppercase", letterSpacing: 1, marginBottom: 8,
};
const cardSt = {
  background: "#111318", border: "1px solid #1d2231",
  borderRadius: 12, padding: "14px 16px",
};