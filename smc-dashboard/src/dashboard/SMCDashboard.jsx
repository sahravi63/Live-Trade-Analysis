/**
 * SMC Trading Dashboard — v3.1
 *
 * Fixes vs v3:
 *   1. Scalp layer (scalp_filter, sizing, ready_to_trade, block_reasons,
 *      exec_entry/sl/tp, scalp_summary) now fully wired — new "Execution" tab
 *   2. Risk status panel (daily P&L, halt, cooldown) polling /risk/status every 10s
 *   3. PDZ gate indicator fixed — matches actual warning strings from backend
 *   4. spread_pips input added to controls — passed to /analyze
 *   5. SL/TP lines on chart guarded: only drawn when signal ≠ WAIT and value > 0
 *   6. candle count selector (100 / 200 / 300 / 500)
 *   7. Exec levels card shows spread-adjusted entry, SL, TP with pip labels
 *   8. Session badge coloured by liquidity score from scalp_filter
 *   9. Momentum score shown as mini progress bar in execution tab
 *  10. All 6 filter check results shown with pass/fail colour coding
 */

import { useEffect, useRef, useState, useCallback } from "react";
import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  CrosshairMode,
  LineSeries,
  LineStyle,
} from "lightweight-charts";

const API = "http://localhost:8000";

const TF_OPTIONS      = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];
const COUNT_OPTIONS   = [100, 200, 300, 500];
const SYMBOL_OPTIONS  = [
  "EURUSD","GBPUSD","USDJPY","USDCHF","AUDUSD","NZDUSD","USDCAD",
  "GBPJPY","EURJPY","EURGBP","XAUUSD",
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

// ── Helpers ────────────────────────────────────────────────────────────────────
const fmt    = (n, d = 5) => (n == null || n === 0 ? "—" : Number(n).toFixed(d));
const fmtPct = (n)        => (n == null ? "—" : Number(n).toFixed(1) + "%");
// JPY pairs use 2-decimal pips (0.01), all others 4-decimal (0.0001)
const pipDiff = (a, b, sym) => {
  if (a == null || b == null || a === 0 || b === 0) return "—";
  const isJpy = sym && sym.toUpperCase().includes("JPY");
  const mult  = isJpy ? 100 : 10000;
  return (Math.abs(a - b) * mult).toFixed(1) + "p";
};
const priceDecimals = (sym) => {
  const s = (sym || "").toUpperCase();
  if (s.includes("XAU") || s.includes("GOLD")) return 2;
  if (s.includes("JPY")) return 3;
  return 5;
};
const fmtPrice = (n, sym) => fmt(n, priceDecimals(sym));
const clk = () => new Date().toLocaleTimeString();

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
          value={amd.manipulation?.direction?.replace(/_/g, " ").toUpperCase() || "â€”"}
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
          value={amd.entry_zone_low && amd.entry_zone_high ? `${fmtPrice(amd.entry_zone_low, symbol)} - ${fmtPrice(amd.entry_zone_high, symbol)}` : "â€”"}
          color={C.blue}
        />
      </div>

      {amd.distribution_started && (
        <div style={{ marginTop: 10, fontSize: 11, color: C.blue, background: C.blueDim, borderRadius: 6, padding: "5px 8px" }}>
          Distribution leg started. Wait for pullback to the AMD entry zone.
        </div>
      )}
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
  const mkLine = (val, lbl) => {
    const s = chart.addSeries(LineSeries, {
      color, lineWidth: 1, lineStyle: lStyle,
      crosshairMarkerVisible: false,
      lastValueVisible: !!lbl,
      priceLineVisible: false,
      title: lbl || "",
    });
    s.setData([{ time: t0, value: val }, { time: t1, value: val }]);
    refs.current.push(s);
  };
  mkLine(top, title);
  mkLine(bot, "");
}

// ── Session colour helper ─────────────────────────────────────────────────────
function sessionColor(session) {
  if (!session) return "gray";
  if (session.includes("Overlap")) return "green";
  if (session.includes("London") || session.includes("New York")) return "blue";
  if (session.includes("Tokyo") || session.includes("Sydney")) return "amber";
  return "red"; // Off-Hours
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
  const [loading,  setLoad]    = useState(false);
  const [err,      setErr]     = useState(null);
  const [autoRefresh, setAuto] = useState(false);
  const [lastUpdate, setLast]  = useState(null);
  const [activeTab, setTab]    = useState("confluence");

  const chartRef   = useRef(null);
  const chartInst  = useRef(null);
  const candleSer  = useRef(null);
  const markerApi  = useRef(null);
  const seriesRefs = useRef([]);
  const timerRef   = useRef(null);
  const riskTimer  = useRef(null);

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
        height: 520,
      });
      candleSer.current = chartInst.current.addSeries(CandlestickSeries, {
        upColor: C.green, downColor: C.red,
        borderUpColor: C.green, borderDownColor: C.red,
        wickUpColor: C.green, wickDownColor: C.red,
      });
      const ro = new ResizeObserver(() => {
        if (chartInst.current && chartRef.current)
          chartInst.current.resize(chartRef.current.clientWidth, 520);
      });
      ro.observe(chartRef.current);
    }

    if (!data?.candles?.length) return;

    const sorted = [...data.candles].sort((a, b) => new Date(a.time) - new Date(b.time));
    const ohlc   = sorted.map((c) => ({
      time:  Math.floor(new Date(c.time).getTime() / 1000),
      open: c.open, high: c.high, low: c.low, close: c.close,
    }));
    candleSer.current.setData(ohlc);

    // Clear overlays
    seriesRefs.current.forEach((s) => {
      try { chartInst.current.removeSeries(s); } catch {
        // Some chart series can already be detached after rapid refresh/resizes.
      }
    });
    seriesRefs.current = [];

    const t0 = ohlc[0].time;
    const t1 = ohlc[ohlc.length - 1].time;

    const addH = (val, color, lw, ls, title, showLabel = true) => {
      if (!val || val === 0) return;
      const s = chartInst.current.addSeries(LineSeries, {
        color, lineWidth: lw, lineStyle: ls,
        crosshairMarkerVisible: false,
        lastValueVisible: showLabel,
        priceLineVisible: false,
        title,
      });
      s.setData([{ time: t0, value: val }, { time: t1, value: val }]);
      seriesRefs.current.push(s);
    };

    // 1. PDZ
    addH(data.premium_zone,  C.orange, 1, LineStyle.Dashed,  "Premium");
    addH(data.equilibrium,   C.muted,  1, LineStyle.Dotted,  "EQ 50%");
    addH(data.discount_zone, C.blue,   1, LineStyle.Dashed,  "Discount");

    // 2. FVGs
    (data.fvgs || []).forEach((fvg) => {
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
    (data.order_blocks || []).forEach((ob) => {
      if (ob.broken) return;
      const color = ob.direction === "bullish" ? C.obBull : C.obBear;
      addBand(chartInst.current, t0, t1, ob.top, ob.bottom, color, LineStyle.Solid,
        ob.direction === "bullish" ? "OB▲" : "OB▼", seriesRefs);
    });

    // 4. OTE
    if (data.ote_low != null && data.ote_high != null)
      addBand(chartInst.current, t0, t1, data.ote_high, data.ote_low, C.ote,
        LineStyle.LargeDashed, "OTE 62–79%", seriesRefs);

    // 5. BOS / CHoCH
    addH(data.bos_level,   C.blue,   2, LineStyle.LargeDashed, "BOS");
    addH(data.choch_level, C.purple, 2, LineStyle.LargeDashed, "CHoCH");

    // 6. Liquidity levels
    (data.liquidity_levels || []).slice(0, 14).forEach((lvl) => {
      const isHigh = lvl.level_type === "equal_high";
      addH(
        lvl.price,
        lvl.swept ? C.border2 : (isHigh ? C.amber : C.purple),
        lvl.swept ? 1 : (lvl.touches >= 3 ? 2 : 1),
        lvl.swept ? LineStyle.Dotted : LineStyle.SparseDotted,
        lvl.swept ? "" : (isHigh ? "EQH" : "EQL"),
        !lvl.swept,
      );
    });

    // 7. Entry / SL / TP — only when signal is active and values are valid
    const hasSignal = data.signal !== "WAIT";
    if (hasSignal && Array.isArray(data.entry_zone)) {
      addBand(chartInst.current, t0, t1,
        data.entry_zone[1], data.entry_zone[0], C.blue, LineStyle.Dashed, "Entry", seriesRefs);
      const entryMid = (data.entry_zone[0] + data.entry_zone[1]) / 2;
      addH(entryMid, C.blue, 2, LineStyle.Solid, "Entry Mid");
    }
    // Guard: stop_loss and take_profit can be 0 on WAIT signal
    if (hasSignal && data.stop_loss  && data.stop_loss  !== 0) addH(data.stop_loss,   C.red,   2, LineStyle.Dashed, "SL");
    if (hasSignal && data.take_profit && data.take_profit !== 0) addH(data.take_profit, C.green, 2, LineStyle.Dashed, "TP");

    // 8. Exec levels from scalp layer (spread-adjusted, dashed)
    if (hasSignal && data.exec_entry) addH(data.exec_entry, C.blue,  1, LineStyle.SparseDotted, "Exec Entry", false);
    if (hasSignal && data.exec_sl)    addH(data.exec_sl,    C.red,   1, LineStyle.SparseDotted, "Exec SL",    false);
    if (hasSignal && data.exec_tp)    addH(data.exec_tp,    C.green, 1, LineStyle.SparseDotted, "Exec TP",    false);

    // 9. AMD phase levels
    if (data.amd?.manipulation?.extreme) {
      addH(data.amd.manipulation.extreme, C.amber, 2, LineStyle.Dashed, "Manip");
    }
    if (data.amd?.manipulation_sl) {
      addH(data.amd.manipulation_sl, C.red, 1, LineStyle.SparseDotted, "AMD SL", false);
    }
    if (data.amd?.accumulation_range) {
      addH(data.amd.accumulation_range.high, C.green, 1, LineStyle.Dotted, "Acc H");
      addH(data.amd.accumulation_range.low,  C.green, 1, LineStyle.Dotted, "Acc L");
    }
    if (data.amd?.entry_zone_low && data.amd?.entry_zone_high) {
      addBand(chartInst.current, t0, t1,
        data.amd.entry_zone_high, data.amd.entry_zone_low, C.blue, LineStyle.SparseDotted, "AMD Entry", seriesRefs);
    }

    // 10. Nearest liquidity targets
    addH(data.nearest_buy_side,  C.amber,  1, LineStyle.SparseDotted, "BuyLiq",  false);
    addH(data.nearest_sell_side, C.purple, 1, LineStyle.SparseDotted, "SellLiq", false);

    // 11. Swing markers
    const markers = [];
    (data.swing_highs || []).forEach((s) => {
      const time = ohlc[s.index]?.time;
      if (!time || !s.label) return;
      markers.push({ time, position: "aboveBar", color: s.label === "HH" ? C.red : "#5c1428", shape: "arrowDown", text: s.label });
    });
    (data.swing_lows || []).forEach((s) => {
      const time = ohlc[s.index]?.time;
      if (!time || !s.label) return;
      markers.push({ time, position: "belowBar", color: s.label === "LL" ? C.green : "#145c3c", shape: "arrowUp", text: s.label });
    });
    if (markers.length) {
      markers.sort((a, b) => a.time - b.time);
      if (!markerApi.current) { markerApi.current = createSeriesMarkers(candleSer.current, markers); }
      else { markerApi.current.setMarkers(markers); }
    } else if (markerApi.current) { markerApi.current.setMarkers([]); }

    chartInst.current.timeScale().fitContent();
  }, [data]);

  // ── Derived ─────────────────────────────────────────────────────────────────
  const trendColor  = !data ? "gray" : data.trend === "BULLISH" ? "green" : data.trend === "BEARISH" ? "red" : "gray";
  const signalColor = !data ? "gray" : data.signal === "BUY" ? "green" : data.signal === "SELL" ? "red" : "amber";
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

  // PDZ gate detection — backend warning contains "premium" or "discount"
  const pdZgateBlocked = data?.warnings?.some(
    w => w.toLowerCase().includes("premium zone") || w.toLowerCase().includes("discount zone")
  );

  // ── Render ──────────────────────────────────────────────────────────────────
  return (
    <div style={{ background: C.bg, minHeight: "100vh", color: C.text, padding: "18px 22px", fontFamily: "system-ui, sans-serif" }}>

      {/* ── Header ── */}
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 16 }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <h1 style={{ margin: 0, fontSize: 20, fontWeight: 800, letterSpacing: -0.5 }}>SMC Engine</h1>
            <Badge label="v3.1" color="blue" small />
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
      <div style={{
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

        <label style={chkSt}>
          <input type="checkbox" checked={useMock} onChange={e => setMock(e.target.checked)} style={{ accentColor: C.blue }} />
          Mock
        </label>
        <label style={chkSt}>
          <input type="checkbox" checked={autoRefresh} onChange={e => setAuto(e.target.checked)} style={{ accentColor: C.blue }} />
          Auto 30s
        </label>

        <button onClick={fetchData} disabled={loading} style={{
          ...btnSt, marginLeft: "auto",
          background: loading ? C.surface2 : C.blue, color: "#fff",
          border: "none", padding: "7px 16px", fontSize: 12, fontWeight: 700,
          opacity: loading ? 0.6 : 1,
        }}>
          {loading ? "Analyzing…" : "⟳ Analyze"}
        </button>
      </div>

      {err && (
        <div style={{
          background: C.redDim, color: "#ffb3c0", padding: "10px 14px",
          borderRadius: 8, marginBottom: 14, fontSize: 12, border: `1px solid ${C.red}22`,
        }}>
          ✗ {err} — ensure <code style={{ background: C.surface, padding: "1px 5px", borderRadius: 4 }}>uvicorn api:app --reload --port 8000</code> is running.
        </div>
      )}

      {/* ── Chart ── */}
      <div style={{ background: C.bg, borderRadius: 12, border: `1px solid ${C.border}`, overflow: "hidden", marginBottom: 14 }}>
        {data && (
          <div style={{
            display: "flex", gap: 12, padding: "9px 14px",
            borderBottom: `1px solid ${C.border}`, flexWrap: "wrap", alignItems: "center",
          }}>
            <span style={{ fontSize: 15, fontWeight: 800, fontFamily: "monospace" }}>{data.symbol}</span>
            <span style={{ fontSize: 15, fontWeight: 700, fontFamily: "monospace", color: C.blue }}>{fmt(data.current_price)}</span>
            <Badge label={data.trend} color={trendColor} />
            {data.last_event && <Badge label={data.last_event} color="blue" />}
            <Badge label={data.signal} color={signalColor} />
            {data.amd?.phase && <Badge label={`AMD ${data.amd.phase}`} color={amdPhaseColor} />}
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
          </div>
        )}

        {/* Legend */}
        <div style={{
          display: "flex", gap: 12, padding: "5px 14px",
          borderBottom: `1px solid ${C.border}`, flexWrap: "wrap",
        }}>
          {[
            { c: C.amber,   l: "EQH/EQL" }, { c: C.fvgBull, l: "FVG▲" }, { c: C.fvgBear, l: "FVG▼" },
            { c: C.obBull,  l: "OB▲" },     { c: C.obBear,  l: "OB▼" },  { c: C.ote,     l: "OTE" },
            { c: C.orange,  l: "Premium" },  { c: C.blue,    l: "Discount" }, { c: C.muted, l: "EQ" },
            { c: C.blue,    l: "Entry" },    { c: C.green,   l: "TP" },    { c: C.red,     l: "SL" },
            { c: C.purple,  l: "CHoCH" },    { c: C.amber,   l: "AMD Manip" },
          ].map(({ c, l }) => (
            <span key={l} style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 10, color: C.muted }}>
              <span style={{ width: 10, height: 2, background: c, display: "inline-block", borderRadius: 1 }} />
              {l}
            </span>
          ))}
        </div>
        <div ref={chartRef} style={{ width: "100%", height: 520 }} />
      </div>

      {data && (
        <>
          {data.amd && (
            <div style={{ display: "grid", gridTemplateColumns: "minmax(320px, 1.15fr) minmax(280px, 0.85fr)", gap: 12, marginBottom: 12 }}>
              <AMDPhaseCard amd={data.amd} symbol={data.symbol || symbol} />
              <AMDSetupCard data={data} />
            </div>
          )}
          {/* ── Row 1: Signal · Entry · R/R · Structure ── */}
          <div style={{ display: "grid", gridTemplateColumns: "200px 1fr 1fr 1fr", gap: 12, marginBottom: 12 }}>

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
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12, marginBottom: 12 }}>
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
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12, marginBottom: 12 }}>

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
          <div style={{ ...cardSt, marginBottom: 12 }}>
            <div style={{ display: "flex", gap: 4, marginBottom: 14, borderBottom: `1px solid ${C.border}`, paddingBottom: 0 }}>
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
        SMC Engine v3.1 · Not financial advice · Always manage risk · {data?.symbol} {data?.timeframe}
      </p>
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
const cardSt = {
  background: "#111318", border: "1px solid #1d2231",
  borderRadius: 12, padding: "14px 16px",
};
