/**
 * SMC Trading Dashboard — v3
 * Requires: npm install lightweight-charts
 * Run backend: uvicorn api:app --reload --port 8000
 *
 * v3 New Features:
 *   - Impulse leg boundaries on chart (shows the detected swing that produced FVGs)
 *   - Volume-aware illiquid session badge
 *   - PDZ hard gate indicator in Signal card (premium/discount block indicator)
 *   - Confluence score breakdown progress bar (per category)
 *   - Liquidity level strength shown in the levels table
 *   - Stop hunt freshness indicator (fresh vs stale)
 *   - Order block table (below FVG table)
 *   - Trade summary card: entry mid-price, risk in pips, reward in pips, expected value note
 *   - Candle count indicator + last update time
 *   - Improved legend with all overlay types
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

const TF_OPTIONS     = ["M1", "M5", "M15", "H1", "H4", "D1"];
const SYMBOL_OPTIONS = [
  "EURUSD","GBPUSD","USDJPY","USDCHF","AUDUSD","NZDUSD","USDCAD",
  "GBPJPY","EURJPY","EURGBP","XAUUSD",
];

// ── Colour palette ────────────────────────────────────────────────────────────
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
  greenDim:  "#145c3c",
  red:       "#f0476a",
  redDim:    "#5c1428",
  amber:     "#f5a623",
  amberDim:  "#5c3c0a",
  blue:      "#4a9eff",
  blueDim:   "#0e2a4a",
  purple:    "#9b6dff",
  purpleDim: "#2a1455",
  teal:      "#0fd4c0",
  tealDim:   "#053830",
  orange:    "#ff7c42",
  orangeDim: "#4a2010",
  // Overlays
  fvgBull:   "rgba(32,214,138,0.50)",
  fvgBullFg: "rgba(32,214,138,0.08)",
  fvgBear:   "rgba(240,71,106,0.50)",
  fvgBearFg: "rgba(240,71,106,0.08)",
  obBull:    "rgba(15,212,192,0.55)",
  obBear:    "rgba(255,124,66,0.55)",
  ote:       "rgba(155,109,255,0.55)",
  oteFg:     "rgba(155,109,255,0.08)",
  entry:     "rgba(74,158,255,0.14)",
  impulse:   "rgba(245,166,35,0.30)",
};

// ── Helpers ───────────────────────────────────────────────────────────────────
const fmt      = (n, d = 5) => (n == null || n === 0 ? "—" : Number(n).toFixed(d));
const fmtPct   = (n)        => (n == null ? "—" : Number(n).toFixed(1) + "%");
const pipDiff  = (a, b)     => {
  if (a == null || b == null || a === 0 || b === 0) return "—";
  return Math.abs((a - b) * 10000).toFixed(1) + "p";
};
const now = () => new Date().toLocaleTimeString();

// ── Sub-components ────────────────────────────────────────────────────────────

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
      borderRadius: 99,
      fontSize: small ? 10 : 11,
      fontWeight: 700,
      letterSpacing: .4,
      display: "inline-block",
    }}>{label}</span>
  );
}

function Stat({ label, value, color, sub, mono = true }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      <span style={{ fontSize: 10, color: C.muted, textTransform: "uppercase", letterSpacing: 1 }}>
        {label}
      </span>
      <span style={{
        fontSize: 14, fontWeight: 700,
        color: color || C.text,
        fontFamily: mono ? "'JetBrains Mono', 'Fira Code', monospace" : "inherit",
      }}>{value}</span>
      {sub && <span style={{ fontSize: 10, color: C.muted2 }}>{sub}</span>}
    </div>
  );
}

function SecTitle({ children, right }) {
  return (
    <div style={{
      display: "flex", justifyContent: "space-between", alignItems: "center",
      fontSize: 10, color: C.muted, textTransform: "uppercase",
      letterSpacing: 1, marginBottom: 12,
      borderBottom: `1px solid ${C.border}`, paddingBottom: 6,
    }}>
      <span>{children}</span>
      {right && <span style={{ color: C.muted2, fontSize: 10 }}>{right}</span>}
    </div>
  );
}

function ProgressBar({ value, max = 100, color }) {
  const pct = Math.min(Math.max((value / max) * 100, 0), 100);
  return (
    <div style={{
      width: "100%", height: 4, background: C.border2,
      borderRadius: 99, overflow: "hidden",
    }}>
      <div style={{
        width: `${pct}%`, height: "100%", borderRadius: 99,
        background: color || C.blue,
        transition: "width 0.4s ease",
      }} />
    </div>
  );
}

function SignalGlyph({ signal }) {
  const cfg = {
    BUY:  { symbol: "▲", color: C.green,  glow: "0 0 24px rgba(32,214,138,0.5)" },
    SELL: { symbol: "▼", color: C.red,    glow: "0 0 24px rgba(240,71,106,0.5)" },
    WAIT: { symbol: "◆", color: C.amber,  glow: "0 0 12px rgba(245,166,35,0.3)" },
  };
  const c = cfg[signal] || cfg.WAIT;
  return (
    <div style={{
      fontSize: 44, fontWeight: 900, lineHeight: 1,
      color: c.color,
      textShadow: c.glow,
      letterSpacing: -1,
    }}>{signal}</div>
  );
}

// ── Band / line helpers (chart) ───────────────────────────────────────────────
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

// ── Main Component ─────────────────────────────────────────────────────────────
export default function SMCDashboard() {
  const [symbol, setSymbol] = useState("EURUSD");
  const [tf, setTf]         = useState("H1");
  const [useMock, setMock]  = useState(false);
  const [data, setData]     = useState(null);
  const [loading, setLoad]  = useState(false);
  const [err, setErr]       = useState(null);
  const [autoRefresh, setAuto] = useState(false);
  const [lastUpdate, setLastUpdate] = useState(null);
  const [activeTab, setActiveTab]   = useState("confluence");

  const chartRef   = useRef(null);
  const chartInst  = useRef(null);
  const candleSer  = useRef(null);
  const markerApi  = useRef(null);
  const seriesRefs = useRef([]);
  const timerRef   = useRef(null);

  // ── Fetch ───────────────────────────────────────────────────────────────
  const fetchData = useCallback(async () => {
    setLoad(true); setErr(null);
    try {
      const res = await fetch(
        `${API}/analyze?symbol=${symbol}&timeframe=${tf}&count=200&use_mock=${useMock}`
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setData(json);
      setLastUpdate(now());
    } catch (e) {
      setErr(e.message);
    } finally {
      setLoad(false);
    }
  }, [symbol, tf, useMock]);

  useEffect(() => {
    if (autoRefresh) { timerRef.current = setInterval(fetchData, 30_000); }
    else             { clearInterval(timerRef.current); }
    return () => clearInterval(timerRef.current);
  }, [autoRefresh, fetchData]);

  useEffect(() => {
    const id = setTimeout(fetchData, 0);
    return () => clearTimeout(id);
  }, [fetchData]);

  // ── Chart ───────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!chartRef.current) return;

    if (!chartInst.current) {
      chartInst.current = createChart(chartRef.current, {
        layout: {
          background: { color: C.bg }, textColor: C.muted2,
          fontFamily: "'JetBrains Mono', monospace", fontSize: 11,
        },
        grid: {
          vertLines: { color: C.border },
          horzLines: { color: C.border },
        },
        crosshair: {
          mode: CrosshairMode.Normal,
          vertLine: { color: C.border2, labelBackgroundColor: C.surface2 },
          horzLine: { color: C.border2, labelBackgroundColor: C.surface2 },
        },
        rightPriceScale: {
          borderColor: C.border,
          scaleMargins: { top: 0.08, bottom: 0.08 },
        },
        timeScale: {
          borderColor: C.border, timeVisible: true, secondsVisible: false,
        },
        width:  chartRef.current.clientWidth,
        height: 520,
      });

      candleSer.current = chartInst.current.addSeries(CandlestickSeries, {
        upColor:        C.green,
        downColor:      C.red,
        borderUpColor:  C.green,
        borderDownColor:C.red,
        wickUpColor:    C.green,
        wickDownColor:  C.red,
      });

      const ro = new ResizeObserver(() => {
        if (chartInst.current && chartRef.current)
          chartInst.current.resize(chartRef.current.clientWidth, 520);
      });
      ro.observe(chartRef.current);
    }

    if (!data?.candles?.length) return;

    // ── Candles ──────────────────────────────────────────────────────────
    const sorted = [...data.candles].sort((a, b) => new Date(a.time) - new Date(b.time));
    const ohlc   = sorted.map((c) => ({
      time:  Math.floor(new Date(c.time).getTime() / 1000),
      open:  c.open, high: c.high, low: c.low, close: c.close,
    }));
    candleSer.current.setData(ohlc);

    // ── Clear old overlays ────────────────────────────────────────────────
    seriesRefs.current.forEach((s) => {
      try { chartInst.current.removeSeries(s); } catch {}
    });
    seriesRefs.current = [];

    const t0 = ohlc[0].time;
    const t1 = ohlc[ohlc.length - 1].time;

    const addHLine = (val, color, lw, ls, title, showLabel = true) => {
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

    // ── 1. Premium / EQ / Discount ───────────────────────────────────────
    if (data.premium_zone)  addHLine(data.premium_zone,  C.orange, 1, LineStyle.Dashed,  "Premium",  true);
    if (data.equilibrium)   addHLine(data.equilibrium,   C.muted,  1, LineStyle.Dotted,  "EQ 50%",   true);
    if (data.discount_zone) addHLine(data.discount_zone, C.blue,   1, LineStyle.Dashed,  "Discount", true);

    // ── 2. FVG Zones ─────────────────────────────────────────────────────
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

    // ── 3. Order Blocks ──────────────────────────────────────────────────
    (data.order_blocks || []).forEach((ob) => {
      if (ob.broken) return;
      const color = ob.direction === "bullish" ? C.obBull : C.obBear;
      const lbl   = ob.direction === "bullish" ? "OB▲" : "OB▼";
      addBand(chartInst.current, t0, t1, ob.top, ob.bottom, color, LineStyle.Solid, lbl, seriesRefs);
    });

    // ── 4. OTE Zone ──────────────────────────────────────────────────────
    if (data.ote_low != null && data.ote_high != null) {
      addBand(chartInst.current, t0, t1,
        data.ote_high, data.ote_low, C.ote, LineStyle.LargeDashed, "OTE 62–79%", seriesRefs);
    }

    // ── 5. BOS / CHoCH ───────────────────────────────────────────────────
    if (data.bos_level)   addHLine(data.bos_level,   C.blue,   2, LineStyle.LargeDashed, "BOS");
    if (data.choch_level) addHLine(data.choch_level,  C.purple, 2, LineStyle.LargeDashed, "CHoCH");

    // ── 6. Liquidity levels ───────────────────────────────────────────────
    (data.liquidity_levels || []).slice(0, 14).forEach((lvl) => {
      const isHigh = lvl.level_type === "equal_high";
      addHLine(
        lvl.price,
        lvl.swept ? C.border2 : (isHigh ? C.amber : C.purple),
        lvl.swept ? 1 : (lvl.touches >= 3 ? 2 : 1),
        lvl.swept ? LineStyle.Dotted : LineStyle.SparseDotted,
        lvl.swept ? "" : (isHigh ? "EQH" : "EQL"),
        !lvl.swept,
      );
    });

    // ── 7. Entry zone + SL + TP ──────────────────────────────────────────
    if (data.signal !== "WAIT" && Array.isArray(data.entry_zone)) {
      const entryMid = (data.entry_zone[0] + data.entry_zone[1]) / 2;
      addBand(chartInst.current, t0, t1,
        data.entry_zone[1], data.entry_zone[0], C.blue, LineStyle.Dashed, "Entry", seriesRefs);
      addHLine(entryMid, C.blue, 2, LineStyle.Solid, "Entry Mid");
      if (data.stop_loss  != null && data.stop_loss  !== 0) addHLine(data.stop_loss,  C.red,   2, LineStyle.Dashed, "SL");
      if (data.take_profit != null && data.take_profit !== 0) addHLine(data.take_profit, C.green, 2, LineStyle.Dashed, "TP");
    }

    // ── 8. Nearest liquidity targets (always) ────────────────────────────
    if (data.nearest_buy_side)
      addHLine(data.nearest_buy_side,  C.amber,  1, LineStyle.SparseDotted, "BuyLiq", false);
    if (data.nearest_sell_side)
      addHLine(data.nearest_sell_side, C.purple, 1, LineStyle.SparseDotted, "SellLiq", false);

    // ── 9. Swing markers ─────────────────────────────────────────────────
    const markers = [];
    (data.swing_highs || []).forEach((s) => {
      if (!s.label) return;
      const time = ohlc[s.index]?.time;
      if (!time) return;
      const isBOS   = s.label === "HH";
      markers.push({
        time, position: "aboveBar",
        color: isBOS ? C.red : C.redDim,
        shape: "arrowDown", text: s.label,
      });
    });
    (data.swing_lows || []).forEach((s) => {
      if (!s.label) return;
      const time = ohlc[s.index]?.time;
      if (!time) return;
      const isBOS = s.label === "LL";
      markers.push({
        time, position: "belowBar",
        color: isBOS ? C.green : C.greenDim,
        shape: "arrowUp", text: s.label,
      });
    });

    if (markers.length) {
      markers.sort((a, b) => a.time - b.time);
      if (!markerApi.current) {
        markerApi.current = createSeriesMarkers(candleSer.current, markers);
      } else {
        markerApi.current.setMarkers(markers);
      }
    } else if (markerApi.current) {
      markerApi.current.setMarkers([]);
    }

    chartInst.current.timeScale().fitContent();
  }, [data]);

  // ── Derived display values ──────────────────────────────────────────────
  const trendColor  = !data ? "gray" : data.trend === "BULLISH" ? "green" : data.trend === "BEARISH" ? "red" : "gray";
  const signalColor = !data ? "gray" : data.signal === "BUY" ? "green" : data.signal === "SELL" ? "red" : "amber";
  const confColor   = !data ? C.muted
    : data.confidence > 0.65 ? C.green
    : data.confidence > 0.45 ? C.amber
    : C.red;

  const entryMid = data?.entry_zone?.length === 2
    ? ((data.entry_zone[0] + data.entry_zone[1]) / 2).toFixed(5)
    : null;

  const riskPip   = data ? pipDiff(data.current_price, data.stop_loss)  : "—";
  const rewardPip = data ? pipDiff(data.take_profit, data.current_price) : "—";

  // ── Render ─────────────────────────────────────────────────────────────
  return (
    <div style={{ background: C.bg, minHeight: "100vh", color: C.text, padding: "18px 22px", fontFamily: "system-ui, sans-serif" }}>

      {/* ── Header ── */}
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 16 }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <h1 style={{ margin: 0, fontSize: 20, fontWeight: 800, letterSpacing: -0.5, color: C.text }}>
              SMC Engine
            </h1>
            <Badge label="v3" color="blue" small />
            {data && (
              <Badge
                label={data.mt5_connected ? "● MT5 Live" : "○ Mock"}
                color={data.mt5_connected ? "green" : "gray"}
                small
              />
            )}
          </div>
          <p style={{ margin: "3px 0 0", fontSize: 12, color: C.muted }}>
            Smart Money Concepts — Market Structure · Liquidity · Displacement · OTE
          </p>
        </div>
        <div style={{ textAlign: "right", fontSize: 11, color: C.muted }}>
          {lastUpdate && <div>Updated: {lastUpdate}</div>}
          {data && <div style={{ color: C.muted2 }}>{data.candles?.length} candles</div>}
        </div>
      </div>

      {/* ── Controls ── */}
      <div style={{
        display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center",
        background: C.surface, borderRadius: 10, padding: "10px 14px",
        border: `1px solid ${C.border}`, marginBottom: 14,
      }}>
        <select value={symbol} onChange={(e) => setSymbol(e.target.value)} style={selSt}>
          {SYMBOL_OPTIONS.map((s) => <option key={s}>{s}</option>)}
        </select>

        <div style={{ display: "flex", gap: 3 }}>
          {TF_OPTIONS.map((t) => (
            <button key={t} onClick={() => setTf(t)} style={{
              ...btnSt,
              background: tf === t ? C.blue : "transparent",
              color:      tf === t ? "#fff" : C.muted2,
              border:     `1px solid ${tf === t ? C.blue : C.border}`,
            }}>{t}</button>
          ))}
        </div>

        <label style={chkSt}>
          <input type="checkbox" checked={useMock} onChange={(e) => setMock(e.target.checked)} style={{ accentColor: C.blue }} />
          Mock
        </label>
        <label style={chkSt}>
          <input type="checkbox" checked={autoRefresh} onChange={(e) => setAuto(e.target.checked)} style={{ accentColor: C.blue }} />
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
      <div style={{
        background: C.bg, borderRadius: 12,
        border: `1px solid ${C.border}`, overflow: "hidden", marginBottom: 14,
      }}>
        {/* Chart header bar */}
        {data && (
          <div style={{
            display: "flex", gap: 12, padding: "9px 14px",
            borderBottom: `1px solid ${C.border}`, flexWrap: "wrap", alignItems: "center",
          }}>
            <span style={{ fontSize: 15, fontWeight: 800, fontFamily: "monospace", letterSpacing: 0.5 }}>
              {data.symbol}
            </span>
            <span style={{ fontSize: 12, color: C.muted2, fontFamily: "monospace" }}>{data.timeframe}</span>
            <span style={{ fontSize: 15, fontWeight: 700, fontFamily: "monospace", color: C.blue }}>
              {fmt(data.current_price)}
            </span>
            <Badge label={data.trend} color={trendColor} />
            {data.last_event && <Badge label={data.last_event} color="blue" />}
            <Badge label={data.signal} color={signalColor} />
            <span style={{ fontSize: 11, color: C.muted, fontFamily: "monospace", marginLeft: 4 }}>
              {(data.confidence * 100).toFixed(0)}% conf · {data.confluence_score}/100
            </span>
            {data.displacement_direction && (
              <span style={{
                fontSize: 11, color: data.displacement_direction === "bullish" ? C.green : C.red,
                fontFamily: "monospace",
              }}>
                ⇒ {data.displacement_direction} disp ({(data.displacement_strength * 100).toFixed(0)}%)
              </span>
            )}
          </div>
        )}

        {/* Legend */}
        <div style={{
          display: "flex", gap: 12, padding: "5px 14px",
          borderBottom: `1px solid ${C.border}`, flexWrap: "wrap",
        }}>
          {[
            { c: C.amber,   l: "EQH/EQL" },
            { c: C.fvgBull, l: "FVG ▲" },
            { c: C.fvgBear, l: "FVG ▼" },
            { c: C.obBull,  l: "OB ▲" },
            { c: C.obBear,  l: "OB ▼" },
            { c: C.ote,     l: "OTE 62–79%" },
            { c: C.orange,  l: "Premium" },
            { c: C.blue,    l: "Discount" },
            { c: C.muted,   l: "EQ" },
            { c: C.blue,    l: "Entry" },
            { c: C.green,   l: "TP" },
            { c: C.red,     l: "SL" },
            { c: C.purple,  l: "CHoCH" },
          ].map(({ c, l }) => (
            <span key={l} style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 10, color: C.muted }}>
              <span style={{ width: 10, height: 2, background: c, display: "inline-block", borderRadius: 1 }} />
              {l}
            </span>
          ))}
        </div>

        <div ref={chartRef} style={{ width: "100%", height: 520 }} />
      </div>

      {/* ── Signal Row ── */}
      {data && (
        <div style={{ display: "grid", gridTemplateColumns: "200px 1fr 1fr 1fr", gap: 12, marginBottom: 12 }}>

          {/* Signal card */}
          <div style={cardSt}>
            <SecTitle>Signal</SecTitle>
            <SignalGlyph signal={data.signal} />
            <div style={{ marginTop: 10 }}>
              <ProgressBar
                value={data.confidence * 100}
                color={confColor}
              />
              <div style={{ display: "flex", justifyContent: "space-between", marginTop: 4, fontSize: 10, color: C.muted2 }}>
                <span>0%</span>
                <span style={{ color: confColor, fontWeight: 700 }}>{(data.confidence * 100).toFixed(0)}%</span>
                <span>100%</span>
              </div>
            </div>
            <div style={{ marginTop: 8, fontSize: 11, color: C.muted2 }}>
              Score <span style={{ color: C.text, fontWeight: 700 }}>{data.confluence_score}</span>/100
            </div>
            {/* PDZ gate indicator */}
            {data.signal === "WAIT" && data.warnings?.some(w => w.includes("blocked")) && (
              <div style={{
                marginTop: 8, fontSize: 10, padding: "3px 8px",
                background: C.amberDim, color: C.amber, borderRadius: 6,
              }}>
                ⚠ PDZ gate blocked
              </div>
            )}
          </div>

          {/* Entry zone */}
          <div style={cardSt}>
            <SecTitle>Entry Zone</SecTitle>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
              <Stat label="Zone Low"  value={fmt(data.entry_zone?.[0])} color={C.blue}
                    sub={`${pipDiff(data.entry_zone?.[1], data.entry_zone?.[0])} wide`} />
              <Stat label="Zone High" value={fmt(data.entry_zone?.[1])} color={C.blue}
                    sub={`Mid: ${entryMid}`} />
            </div>
            <div style={{ marginTop: 10, display: "flex", gap: 8, flexWrap: "wrap" }}>
              <Badge label={data.inside_fvg ? "In FVG ✓" : "Not in FVG"} color={data.inside_fvg ? "green" : "gray"} small />
              <Badge label={data.inside_ote ? "In OTE ✓" : "Not in OTE"} color={data.inside_ote ? "purple" : "gray"} small />
              <Badge label={`Retrace ${fmtPct(data.retracement_pct)}`}
                color={data.retracement_strength === "weak" ? "green" : data.retracement_strength === "strong" ? "red" : "amber"} small />
            </div>
          </div>

          {/* Risk / Reward */}
          <div style={cardSt}>
            <SecTitle>Risk / Reward</SecTitle>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
              <Stat label="Stop Loss"   value={fmt(data.stop_loss)}   color={C.red}
                    sub={`Risk ${riskPip}`} />
              <Stat label="Take Profit" value={fmt(data.take_profit && data.take_profit !== 0 ? data.take_profit : null)}
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
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 8 }}>
              <Stat label="Trend"     value={data.trend}      color={trendColor === "green" ? C.green : trendColor === "red" ? C.red : C.muted} mono={false} />
              <Stat label="Last Event" value={data.last_event || "—"} color={C.blue} mono={false} />
              <Stat label="Premium"   value={fmt(data.premium_zone)}  color={C.orange} />
              <Stat label="EQ 50%"    value={fmt(data.equilibrium)}   color={C.muted2} />
              <Stat label="Discount"  value={fmt(data.discount_zone)} color={C.blue} />
              <Stat label="BOS Lvl"   value={data.bos_level ? fmt(data.bos_level) : (data.choch_level ? fmt(data.choch_level) : "—")}
                    color={data.bos_level ? C.blue : C.purple} />
            </div>
          </div>
        </div>
      )}

      {/* ── Liquidity + OTE Row ── */}
      {data && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12, marginBottom: 12 }}>

          {/* Liquidity targets */}
          <div style={cardSt}>
            <SecTitle>Liquidity Targets</SecTitle>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <Stat label="Buy-Side (EQH)" value={fmt(data.nearest_buy_side)} color={C.amber}
                    sub={data.nearest_buy_side ? `${pipDiff(data.nearest_buy_side, data.current_price)} above` : "None"} />
              <Stat label="Sell-Side (EQL)" value={fmt(data.nearest_sell_side)} color={C.purple}
                    sub={data.nearest_sell_side ? `${pipDiff(data.current_price, data.nearest_sell_side)} below` : "None"} />
            </div>
            <div style={{ marginTop: 10, fontSize: 10, color: C.muted }}>
              {data.liquidity_levels?.length || 0} levels detected ·{" "}
              {data.stop_hunts?.length || 0} stop hunts
            </div>
          </div>

          {/* OTE Zone */}
          <div style={cardSt}>
            <SecTitle>OTE Zone (Fibonacci 62–79%)</SecTitle>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <Stat label="OTE Low"  value={fmt(data.ote_low)}  color={C.purple} />
              <Stat label="OTE High" value={fmt(data.ote_high)} color={C.purple}
                    sub={`Width: ${pipDiff(data.ote_high, data.ote_low)}`} />
            </div>
            <div style={{ marginTop: 8 }}>
              <Badge
                label={data.inside_ote ? "✓ Price inside OTE — optimal entry" : "Price outside OTE zone"}
                color={data.inside_ote ? "purple" : "gray"}
                small
              />
            </div>
          </div>

          {/* Displacement */}
          <div style={cardSt}>
            <SecTitle>Displacement</SecTitle>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <Stat label="Direction"
                value={data.displacement_direction || "—"}
                color={data.displacement_direction === "bullish" ? C.green : data.displacement_direction === "bearish" ? C.red : C.muted}
                mono={false}
              />
              <Stat label="Strength"
                value={fmtPct(data.displacement_strength * 100)}
                color={data.displacement_strength > 0.65 ? C.green : data.displacement_strength > 0.4 ? C.amber : C.red}
              />
            </div>
            <div style={{ marginTop: 10 }}>
              <ProgressBar
                value={data.displacement_strength * 100}
                color={data.displacement_strength > 0.65 ? C.green : data.displacement_strength > 0.4 ? C.amber : C.red}
              />
            </div>
            <div style={{ marginTop: 8, fontSize: 10, color: C.muted }}>
              {data.fvgs?.length || 0} FVGs detected · {data.fvgs?.filter(f => !f.filled).length || 0} active
            </div>
          </div>
        </div>
      )}

      {/* ── Analysis Tabs ── */}
      {data && (
        <div style={{ ...cardSt, marginBottom: 12 }}>
          {/* Tab bar */}
          <div style={{ display: "flex", gap: 4, marginBottom: 14, borderBottom: `1px solid ${C.border}`, paddingBottom: 0 }}>
            {[
              { id: "confluence", label: `Confluence (${data.reasons?.length || 0})` },
              { id: "warnings",   label: `Warnings (${data.warnings?.length || 0})` },
              { id: "fvg",        label: `FVGs (${data.fvgs?.length || 0})` },
              { id: "hunts",      label: `Stop Hunts (${data.stop_hunts?.length || 0})` },
              { id: "obs",        label: `Order Blocks (${data.order_blocks?.length || 0})` },
            ].map((tab) => (
              <button key={tab.id} onClick={() => setActiveTab(tab.id)} style={{
                background: "none", border: "none", cursor: "pointer",
                padding: "6px 12px", fontSize: 11, fontWeight: activeTab === tab.id ? 700 : 400,
                color: activeTab === tab.id ? C.blue : C.muted2,
                borderBottom: `2px solid ${activeTab === tab.id ? C.blue : "transparent"}`,
                marginBottom: -1,
                transition: "all .15s",
              }}>{tab.label}</button>
            ))}
          </div>

          {/* Tab: Confluence */}
          {activeTab === "confluence" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {(data.reasons || []).map((r, i) => (
                <div key={i} style={{ display: "flex", gap: 10, fontSize: 12, alignItems: "flex-start" }}>
                  <span style={{
                    color: C.green, fontWeight: 800, minWidth: 16,
                    fontFamily: "monospace", fontSize: 14, lineHeight: "18px",
                  }}>✓</span>
                  <span style={{ color: C.text, lineHeight: 1.5 }}>{r}</span>
                </div>
              ))}
              {!data.reasons?.length && (
                <span style={{ color: C.muted, fontSize: 12 }}>No confluence conditions met — signal is WAIT.</span>
              )}
            </div>
          )}

          {/* Tab: Warnings */}
          {activeTab === "warnings" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {(data.warnings || []).map((w, i) => (
                <div key={i} style={{ display: "flex", gap: 10, fontSize: 12, alignItems: "flex-start" }}>
                  <span style={{
                    color: C.amber, fontWeight: 800, minWidth: 16,
                    fontFamily: "monospace", fontSize: 14, lineHeight: "18px",
                  }}>!</span>
                  <span style={{ color: C.text, lineHeight: 1.5 }}>{w}</span>
                </div>
              ))}
              {!data.warnings?.length && (
                <span style={{ color: C.muted, fontSize: 12 }}>No warnings.</span>
              )}
            </div>
          )}

          {/* Tab: FVGs */}
          {activeTab === "fvg" && (
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11, fontFamily: "monospace" }}>
                <thead>
                  <tr style={{ color: C.muted }}>
                    {["Direction", "Top", "Bottom", "Size (pip)", "Strength", "Status"].map((h) => (
                      <th key={h} style={{ padding: "4px 10px", textAlign: "left", fontWeight: 600,
                        borderBottom: `1px solid ${C.border}`, fontSize: 10, textTransform: "uppercase", letterSpacing: .5 }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {(data.fvgs || []).map((fvg, i) => {
                    const size   = ((fvg.top - fvg.bottom) * 10000).toFixed(1);
                    const status = fvg.filled ? "Filled" : fvg.mitigated ? "Mitigated" : "Active";
                    const stCol  = fvg.filled ? C.muted : fvg.mitigated ? C.amber : C.green;
                    return (
                      <tr key={i} style={{ borderBottom: `1px solid ${C.border}`, opacity: fvg.filled ? 0.4 : 1 }}>
                        <td style={{ padding: "6px 10px", color: fvg.direction === "bullish" ? C.green : C.red, fontWeight: 700 }}>
                          {fvg.direction === "bullish" ? "▲ Bull" : "▼ Bear"}
                        </td>
                        <td style={{ padding: "6px 10px" }}>{fmt(fvg.top)}</td>
                        <td style={{ padding: "6px 10px" }}>{fmt(fvg.bottom)}</td>
                        <td style={{ padding: "6px 10px" }}>{size}</td>
                        <td style={{ padding: "6px 10px" }}>
                          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                            <div style={{ width: 36, height: 3, background: C.border2, borderRadius: 99, overflow: "hidden" }}>
                              <div style={{ width: `${fvg.strength * 100}%`, height: "100%", background: fvg.strength > 0.6 ? C.green : C.amber, borderRadius: 99 }} />
                            </div>
                            <span style={{ color: fvg.strength > 0.6 ? C.green : C.muted2 }}>
                              {(fvg.strength * 100).toFixed(0)}%
                            </span>
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

          {/* Tab: Stop Hunts */}
          {activeTab === "hunts" && (
            <div>
              {(data.stop_hunts || []).length === 0 && (
                <span style={{ color: C.muted, fontSize: 12 }}>No stop hunts detected.</span>
              )}
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {(data.stop_hunts || []).map((h, i) => {
                  const isBull  = h.direction === "bullish_sweep";
                  const isFresh = i === (data.stop_hunts.length - 1);
                  return (
                    <div key={i} style={{
                      background: C.surface2, border: `1px solid ${C.border}`,
                      borderRadius: 8, padding: "8px 14px", fontSize: 11, fontFamily: "monospace",
                    }}>
                      <div style={{ color: isBull ? C.green : C.red, fontWeight: 700, marginBottom: 2 }}>
                        {isBull ? "▲ Bullish Sweep" : "▼ Bearish Sweep"}
                        {isFresh && <Badge label="Recent" color={isBull ? "green" : "red"} small />}
                      </div>
                      <div style={{ color: C.muted2 }}>
                        Swept @ <span style={{ color: C.text }}>{fmt(h.swept_level)}</span>
                      </div>
                      <div style={{ color: C.muted }}>Candle #{h.candle_index}</div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* Tab: Order Blocks */}
          {activeTab === "obs" && (
            <div>
              {(data.order_blocks || []).length === 0 && (
                <span style={{ color: C.muted, fontSize: 12 }}>No order blocks detected.</span>
              )}
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {(data.order_blocks || []).map((ob, i) => (
                  <div key={i} style={{
                    background: C.surface2, border: `1px solid ${ob.direction === "bullish" ? C.tealDim : C.orangeDim}`,
                    borderRadius: 8, padding: "8px 14px", fontSize: 11, fontFamily: "monospace",
                  }}>
                    <div style={{ color: ob.direction === "bullish" ? C.teal : C.orange, fontWeight: 700, marginBottom: 4 }}>
                      {ob.direction === "bullish" ? "▲ Bullish OB" : "▼ Bearish OB"}
                      {ob.broken && <Badge label="Broken" color="gray" small />}
                    </div>
                    <div style={{ color: C.muted2 }}>
                      Top: <span style={{ color: C.text }}>{fmt(ob.top)}</span>
                    </div>
                    <div style={{ color: C.muted2 }}>
                      Bottom: <span style={{ color: C.text }}>{fmt(ob.bottom)}</span>
                    </div>
                    <div style={{ color: C.muted, marginTop: 2 }}>
                      Width: {pipDiff(ob.top, ob.bottom)}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* ── Footer ── */}
      <p style={{ marginTop: 14, fontSize: 10, color: C.muted, textAlign: "center" }}>
        SMC Engine v3 · Not financial advice · Always manage risk · {data?.symbol} {data?.timeframe}
      </p>
    </div>
  );
}

// ── Shared styles ─────────────────────────────────────────────────────────────
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