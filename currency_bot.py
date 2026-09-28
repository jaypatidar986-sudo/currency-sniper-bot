#!/usr/bin/env python3
"""
Currency Sniper Bot v2.1 — FINAL
Pairs: EURUSD, GBPUSD, USDJPY | TF: 5m | Source: Twelve Data
Behavior: NEVER STOPS. NEVER SILENT. Messages forever.
8-min internal loop with 1-min proximity alerts.
"""
import os, json, time, traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
import pandas as pd

# ============================================================
# CONFIG
# ============================================================
TD_KEY        = os.getenv("TD_KEY", "").strip()
TG_TOKEN      = os.getenv("TG_TOKEN", "").strip()
TG_CHAT_ID    = os.getenv("TG_CHAT_ID", "").strip()
ACCOUNT_BAL   = float(os.getenv("ACCOUNT_BALANCE", "10000") or 10000)
RISK_PER_TRADE= float(os.getenv("RISK_PER_TRADE", "0.01") or 0.01)

PAIRS         = ["EURUSD", "GBPUSD", "USDJPY"]
TD_SYMBOL     = {"EURUSD": "EUR/USD", "GBPUSD": "GBP/USD", "USDJPY": "USD/JPY"}
PIP           = {"EURUSD": 0.0001, "GBPUSD": 0.0001, "USDJPY": 0.01}
PIP_VALUE_LOT = {"EURUSD": 10.0, "GBPUSD": 10.0, "USDJPY": 6.7}

SETUPS_FILE   = "currency_setups.csv"
STATE_FILE    = "bot_state.json"
NEWS_CACHE    = "news_cache.json"

TIER_SLTP = {"S+": (20, 40), "S": (50, 50), "A": (50, 25), "B": (45, 22)}

MAX_OPEN_TRADES       = 20
MAX_TRADES_PER_PAIR   = 10
MAX_DAILY_LOSS_PCT    = 0.03
MAX_CONSEC_LOSSES     = 3
SPREAD_LIMIT_PIP      = 2.0
SIGNAL_EXPIRY_MIN     = 30
TRADE_MAX_HOURS       = 25
LIMIT_OFFSET_PIPS     = 4
HEARTBEAT_MIN         = 15
PROXIMITY_ALERT_PIPS  = 5
LOOP_MIN              = 8
TICK_SEC              = 60
ETA_CAP_MIN           = 180
NEWS_URL              = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# ============================================================
# LOG / TELEGRAM
# ============================================================
def log(*a):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}]", *a, flush=True)

def tg_send(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        log("TG skip:", text[:80])
        return False
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=15)
            if r.status_code == 200:
                return True
            log(f"TG {r.status_code} attempt {attempt+1}")
        except Exception as e:
            log(f"TG err attempt {attempt+1}:", e)
        if attempt < 2:
            time.sleep(5)
    return False

# ============================================================
# HELPERS
# ============================================================
def fmt_price(pair, p):
    return f"{p:.5f}" if pair != "USDJPY" else f"{p:.3f}"

def _parse_iso(s):
    try:
        t = datetime.fromisoformat(s.replace("Z","+00:00"))
        if t.tzinfo is None: t = t.replace(tzinfo=timezone.utc)
        return t
    except Exception:
        return None

# ============================================================
# STATE
# ============================================================
def load_state():
    if Path(STATE_FILE).exists():
        try: return json.loads(Path(STATE_FILE).read_text())
        except Exception: pass
    return {}

def save_state(s):
    try: Path(STATE_FILE).write_text(json.dumps(s, indent=2, default=str))
    except Exception as e: log("save err", e)

def today_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def fresh_daily():
    return {"date": today_utc_str(), "pnl_pip": 0.0, "pnl_usd": 0.0,
            "trades": 0, "wins": 0, "losses": 0,
            "pair_trades": {}, "loss_warned": False}

def ensure_state():
    s = load_state()
    s.setdefault("open_trades", [])
    s.setdefault("consec_losses", 0)
    s.setdefault("recent_signals", {})
    s.setdefault("last_heartbeat", "")
    s.setdefault("last_scan_msg", "")
    s.setdefault("setup_perf", {})
    s.setdefault("daily", fresh_daily())
    if s["daily"].get("date") != today_utc_str():
        s["daily"] = fresh_daily()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    s["recent_signals"] = {
        k: v for k, v in s["recent_signals"].items()
        if (_parse_iso(v) and _parse_iso(v) > cutoff)
    }
    return s

# ============================================================
# DST
# ============================================================
def _nth_sunday(y, m, n):
    d = datetime(y, m, 1, tzinfo=timezone.utc)
    first = d + timedelta(days=(6 - d.weekday()) % 7)
    return first + timedelta(days=7 * (n - 1))

def _last_sunday(y, m):
    if m == 12:
        d = datetime(y+1, 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    else:
        d = datetime(y, m+1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - 6) % 7)

def is_us_dst(dt=None):
    dt = dt or datetime.now(timezone.utc); y = dt.year
    return _nth_sunday(y, 3, 2) <= dt < _nth_sunday(y, 11, 1)

def is_eu_dst(dt=None):
    dt = dt or datetime.now(timezone.utc); y = dt.year
    return _last_sunday(y, 3) <= dt < _last_sunday(y, 10)

# ============================================================
# SESSION / DAY
# ============================================================
def in_session(sess, dt=None):
    dt = dt or datetime.now(timezone.utc)
    h = dt.hour + dt.minute / 60.0
    us_dst = is_us_dst(dt)
    if sess == "sess_tokyo":
        return (1.0 <= h < 9.0) if us_dst else (0.0 <= h < 8.0)
    if sess == "sess_london":
        return (7.0 <= h < 16.0) if is_eu_dst(dt) else (8.0 <= h < 17.0)
    if sess == "overlap":
        return (12.0 <= h < 16.0) if (is_eu_dst(dt) and us_dst) else (13.0 <= h < 17.0)
    if sess == "ny_am":
        return (12.0 <= h < 16.0) if us_dst else (13.0 <= h < 17.0)
    if sess == "ny_pm":
        return (16.0 <= h < 20.0) if us_dst else (17.0 <= h < 21.0)
    return True

def day_matches(day_rule, dt=None):
    dt = dt or datetime.now(timezone.utc)
    wd = dt.weekday()
    if day_rule in ("all", "day_all"): return True
    if day_rule == "day_wed":     return wd == 2
    if day_rule == "day_tue_thu": return wd in (1, 2, 3)
    if day_rule == "mon":         return wd == 0
    if day_rule == "fri":         return wd == 4
    if day_rule in ("wom2", "wom3"):
        wom = (dt.day - 1) // 7 + 1
        return wom == (2 if day_rule == "wom2" else 3)
    return True

# ============================================================
# TWELVE DATA
# ============================================================
def fetch_td(symbol, interval, outputsize=100):
    if not TD_KEY: return None
    url = (f"https://api.twelvedata.com/time_series?"
           f"symbol={symbol}&interval={interval}"
           f"&outputsize={outputsize}&apikey={TD_KEY}&format=JSON")
    try:
        r = requests.get(url, timeout=20)
        j = r.json()
        if j.get("status") == "error" or "values" not in j:
            log("TD err", symbol, interval, j.get("message"))
            return None
        df = pd.DataFrame(j["values"])
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
        for c in ("open", "high", "low", "close"):
            df[c] = df[c].astype(float)
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0) if "volume" in df.columns else 0.0
        df = df.sort_values("datetime").reset_index(drop=True)
        return df
    except Exception as e:
        log("fetch_td exc", symbol, e)
        return None

def fetch_live_price(pair):
    if not TD_KEY: return None
    try:
        r = requests.get(
            f"https://api.twelvedata.com/quote?symbol={TD_SYMBOL[pair]}&apikey={TD_KEY}",
            timeout=10)
        j = r.json()
        bid = float(j.get("bid") or 0)
        ask = float(j.get("ask") or 0)
        if bid and ask:
            return (bid + ask) / 2.0, (ask - bid)
    except Exception as e:
        log("live price err", pair, e)
    return None, None

# ============================================================
# INDICATORS
# ============================================================
def atr(df, n=14):
    if len(df) < n + 1:
        return float((df["high"] - df["low"]).mean())
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])

def vwap_intraday(df):
    if len(df) == 0: return None
    df = df.copy()
    df["date"] = df["datetime"].dt.date
    today = df["datetime"].iloc[-1].date()
    d = df[df["date"] == today]
    if len(d) == 0: return None
    tp = (d["high"] + d["low"] + d["close"]) / 3
    if d["volume"].sum() == 0:
        return float(tp.mean())
    return float((tp * d["volume"]).sum() / d["volume"].sum())

def day_high_low(df_daily):
    if df_daily is None or len(df_daily) < 2:
        return None, None, None
    prev = df_daily.iloc[-2]
    return float(prev["high"]), float(prev["low"]), float(prev["close"])

def cpr_levels(hi, lo, cl):
    pivot = (hi + lo + cl) / 3.0
    bc    = (hi + lo) / 2.0
    tc    = 2 * pivot - bc
    return tc, pivot, bc

# ============================================================
# PATTERNS
# ============================================================
def body(o, c): return abs(c - o)
def rng(h, l):  return max(h - l, 1e-12)

def pat_at_day_lo(df, day_lo, pip):
    if day_lo is None or len(df) < 2: return False
    return abs(df["low"].iloc[-1] - day_lo) <= 5 * pip

def pat_at_day_hi(df, day_hi, pip):
    if day_hi is None or len(df) < 2: return False
    return abs(df["high"].iloc[-1] - day_hi) <= 5 * pip

def pat_rej_day_hi(df, day_hi, pip):
    if day_hi is None or len(df) < 2: return False
    c = df.iloc[-1]
    return (c["high"] >= day_hi - 2*pip and c["close"] < c["open"]
            and (c["high"] - c["close"]) >= 0.5 * rng(c["high"], c["low"]))

def pat_rej_day_lo(df, day_lo, pip):
    if day_lo is None or len(df) < 2: return False
    c = df.iloc[-1]
    return (c["low"] <= day_lo + 2*pip and c["close"] > c["open"]
            and (c["close"] - c["low"]) >= 0.5 * rng(c["high"], c["low"]))

def pat_dn3(df):
    if len(df) < 3: return False
    c = df.iloc[-3:]
    return all(c["close"].iloc[i] < c["open"].iloc[i] for i in range(3))

def pat_up3(df):
    if len(df) < 3: return False
    c = df.iloc[-3:]
    return all(c["close"].iloc[i] > c["open"].iloc[i] for i in range(3))

def pat_2bar_bear(df):
    if len(df) < 2: return False
    c = df.iloc[-2:]
    return all(c["close"].iloc[i] < c["open"].iloc[i] for i in range(2))

def pat_2bar_bull(df):
    if len(df) < 2: return False
    c = df.iloc[-2:]
    return all(c["close"].iloc[i] > c["open"].iloc[i] for i in range(2))

def pat_evening_star(df):
    if len(df) < 3: return False
    a, b, c = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    return (a["close"] > a["open"] and body(b["open"], b["close"]) < 0.4 * body(a["open"], a["close"])
            and c["close"] < c["open"] and c["close"] < a["close"])

def pat_morning_star(df):
    if len(df) < 3: return False
    a, b, c = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    return (a["close"] < a["open"] and body(b["open"], b["close"]) < 0.4 * body(a["open"], a["close"])
            and c["close"] > c["open"] and c["close"] > a["close"])

def pat_pullback_bear(df):
    if len(df) < 5: return False
    if df["close"].iloc[-4] <= df["close"].iloc[-5]: return False
    c = df.iloc[-1]
    return c["close"] < c["open"] and body(c["open"], c["close"]) > 0.6 * rng(c["high"], c["low"])

def pat_pullback_bull(df):
    if len(df) < 5: return False
    if df["close"].iloc[-4] >= df["close"].iloc[-5]: return False
    c = df.iloc[-1]
    return c["close"] > c["open"] and body(c["open"], c["close"]) > 0.6 * rng(c["high"], c["low"])

def pat_fvg_bear(df):
    if len(df) < 3: return False
    a, _, c = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    return a["low"] > c["high"]

def pat_fvg_bull(df):
    if len(df) < 3: return False
    a, _, c = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    return a["high"] < c["low"]

def pat_tweezer_top(df, pip=0.0001):
    if len(df) < 2: return False
    a, b = df.iloc[-2], df.iloc[-1]
    return abs(a["high"] - b["high"]) <= 2 * pip and b["close"] < b["open"]

def pat_tweezer_bottom(df, pip=0.0001):
    if len(df) < 2: return False
    a, b = df.iloc[-2], df.iloc[-1]
    return abs(a["low"] - b["low"]) <= 2 * pip and b["close"] > b["open"]

PATTERN_FN = {
    "dn3":           pat_dn3,
    "up3":           pat_up3,
    "2bar_bear":     pat_2bar_bear,
    "2bar_bull":     pat_2bar_bull,
    "evening_star":  pat_evening_star,
    "morning_star":  pat_morning_star,
    "pullback_bear": pat_pullback_bear,
    "pullback_bull": pat_pullback_bull,
    "fvg_bear":      pat_fvg_bear,
    "fvg_bull":      pat_fvg_bull,
}

def detect_pattern(name, df, day_hi, day_lo, pip):
    if name == "at_day_lo":      return pat_at_day_lo(df, day_lo, pip)
    if name == "at_day_hi":      return pat_at_day_hi(df, day_hi, pip)
    if name == "rej_day_hi":     return pat_rej_day_hi(df, day_hi, pip)
    if name == "rej_day_lo":     return pat_rej_day_lo(df, day_lo, pip)
    if name == "tweezer_top":    return pat_tweezer_top(df, pip)
    if name == "tweezer_bottom": return pat_tweezer_bottom(df, pip)
    fn = PATTERN_FN.get(name)
    return bool(fn(df)) if fn else False

# ============================================================
# FILTERS
# ============================================================
def filter_ok(name, df, day_hi, day_lo, day_cl, pip):
    if name in ("none", "", "all"): return True
    last = df.iloc[-1]
    close = float(last["close"])
    vwap  = vwap_intraday(df)
    if name == "below_vwap":
        return vwap is not None and close < vwap
    if name == "above_vwap":
        return vwap is not None and close > vwap
    if day_hi is not None and day_lo is not None and day_cl is not None:
        tc, _, bc = cpr_levels(day_hi, day_lo, day_cl)
        if name == "above_cpr": return close > tc
        if name == "below_cpr": return close < bc
    return True

def multi_tf_confirm(df_15m, direction):
    if df_15m is None or len(df_15m) < 20: return True
    ema_f = df_15m["close"].ewm(span=9).mean().iloc[-1]
    ema_s = df_15m["close"].ewm(span=21).mean().iloc[-1]
    if direction == "BUY":  return ema_f >= ema_s
    if direction == "SELL": return ema_f <= ema_s
    return True

# ============================================================
# SPREAD / NEWS
# ============================================================
def spread_ok(pair):
    if not TD_KEY: return True, 0.0
    try:
        r = requests.get(
            f"https://api.twelvedata.com/quote?symbol={TD_SYMBOL[pair]}&apikey={TD_KEY}",
            timeout=10)
        j = r.json()
        bid = float(j.get("bid") or 0)
        ask = float(j.get("ask") or 0)
        if bid and ask:
            sp = (ask - bid) / PIP[pair]
            return sp <= SPREAD_LIMIT_PIP, sp
    except Exception as e:
        log("spread err", pair, e)
    return True, 0.0

def load_news_cache():
    if Path(NEWS_CACHE).exists():
        try: return json.loads(Path(NEWS_CACHE).read_text())
        except Exception: pass
    return {"fetched_at": "", "events": []}

def fetch_news():
    try:
        r = requests.get(NEWS_URL, timeout=15)
        arr = r.json()
        events = []
        for e in arr:
            impact = (e.get("impact") or "").lower()
            if impact not in ("high", "medium"): continue
            country = (e.get("country") or "").upper()
            if country not in ("USD", "EUR", "GBP", "JPY"): continue
            events.append({
                "title": e.get("title",""),
                "country": country,
                "date": e.get("date",""),
                "impact": impact,
            })
        return {"fetched_at": datetime.now(timezone.utc).isoformat(), "events": events}
    except Exception as e:
        log("news fetch err", e)
        return None

def refresh_news_if_needed():
    c = load_news_cache()
    stale = True
    try:
        if c.get("fetched_at"):
            t = datetime.fromisoformat(c["fetched_at"].replace("Z","+00:00"))
            stale = (datetime.now(timezone.utc) - t) > timedelta(hours=6)
    except Exception: pass
    if stale:
        n = fetch_news()
        if n:
            Path(NEWS_CACHE).write_text(json.dumps(n, indent=2))
            c = n
    return c

def news_window_active(cache):
    try:
        now = datetime.now(timezone.utc)
        for e in cache.get("events", []):
            try:
                t = datetime.fromisoformat(e["date"].replace("Z","+00:00"))
                if t.tzinfo is None: t = t.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if abs((now - t).total_seconds()) <= 15 * 60:
                return True, e
    except Exception: pass
    return False, None

# ============================================================
# LOT / ETA
# ============================================================
def compute_lot(pair, sl_pip):
    risk_usd = ACCOUNT_BAL * RISK_PER_TRADE
    per_pip  = PIP_VALUE_LOT[pair]
    if sl_pip <= 0: sl_pip = 20
    lot = risk_usd / (sl_pip * per_pip)
    return max(round(lot, 2), 0.01)

def estimate_eta_min(atr_val, pip, tp_pip):
    if not atr_val or atr_val <= 0: return 45
    atr_pip = atr_val / pip
    candles = tp_pip / max(atr_pip * 0.5, 1.0)
    eta = max(int(candles * 5), 10)
    return min(eta, ETA_CAP_MIN)

# ============================================================
# SIGNAL SCANNING
# ============================================================
def load_setups():
    df = pd.read_csv(SETUPS_FILE)
    df.columns = [c.strip() for c in df.columns]
    return df

def signal_key(pair, pattern, direction):
    return f"{pair}_{pattern}_{direction}"

def already_signalled(state, key):
    last = state["recent_signals"].get(key)
    if not last: return False
    try:
        t = datetime.fromisoformat(last.replace("Z","+00:00"))
        return (datetime.now(timezone.utc) - t) < timedelta(minutes=SIGNAL_EXPIRY_MIN)
    except Exception:
        return False

def has_open_same_setup(state, pair, pattern, direction):
    for t in state["open_trades"]:
        if t["pair"] == pair and t["pattern"] == pattern and t["direction"] == direction:
            return True
    return False

def has_open_opposite(state, pair, direction):
    for t in state["open_trades"]:
        if t["pair"] == pair and t["direction"] != direction:
            return t
    return None

def correlation_warn(pair, direction, open_trades):
    if pair in ("EURUSD", "GBPUSD"):
        other = "GBPUSD" if pair == "EURUSD" else "EURUSD"
        for t in open_trades:
            if t["pair"] == other and t["direction"] == direction:
                return True
    return False

def scan_signals(state):
    setups = load_setups()
    now = datetime.now(timezone.utc)
    news = refresh_news_if_needed()
    news_active, news_evt = news_window_active(news)

    found = []
    for pair in PAIRS:
        df5 = fetch_td(TD_SYMBOL[pair], "5min", 100)
        if df5 is None or len(df5) < 30:
            log("no data", pair); continue
        age_min = (now - df5["datetime"].iloc[-1].to_pydatetime()).total_seconds()/60
        if age_min > 15:
            log("stale data", pair, round(age_min,1)); continue

        df15 = fetch_td(TD_SYMBOL[pair], "15min", 100)
        dfd  = fetch_td(TD_SYMBOL[pair], "1day", 10)
        day_hi, day_lo, day_cl = day_high_low(dfd)

        pair_cnt = state["daily"].get("pair_trades", {}).get(pair, 0)
        if pair_cnt >= MAX_TRADES_PER_PAIR:
            continue

        for _, row in setups[setups["instrument"] == pair].iterrows():
            if row["tf"] != "5m": continue
            if not in_session(row["session"], now): continue
            if not day_matches(row["day"], now): continue

            pip = PIP[pair]
            if not detect_pattern(row["pattern"], df5, day_hi, day_lo, pip):
                continue
            if not filter_ok(row["filter"], df5, day_hi, day_lo, day_cl, pip):
                continue

            weak = not multi_tf_confirm(df15, row["direction"])

            key = signal_key(pair, row["pattern"], row["direction"])
            if already_signalled(state, key):
                continue

            if has_open_same_setup(state, pair, row["pattern"], row["direction"]):
                log(f"dup setup skip {key}"); continue

            warn = None
            if news_active and news_evt:
                warn = f"⚠️ News: {news_evt.get('country')} {news_evt.get('title','')[:30]}"

            corr_warn = correlation_warn(pair, row["direction"], state["open_trades"])

            sp_ok, sp_val = spread_ok(pair)
            if not sp_ok:
                log(f"spread skip {pair} {sp_val:.1f}"); continue

            sl_pip, tp_pip = TIER_SLTP[row["tier"]]
            last_close = float(df5["close"].iloc[-1])

            if row["direction"] == "BUY":
                limit = last_close - LIMIT_OFFSET_PIPS * pip
                sl = limit - sl_pip * pip
                tp = limit + tp_pip * pip
            else:
                limit = last_close + LIMIT_OFFSET_PIPS * pip
                sl = limit + sl_pip * pip
                tp = limit - tp_pip * pip

            lot = compute_lot(pair, sl_pip)
            atr_val = atr(df5)
            eta = estimate_eta_min(atr_val, pip, tp_pip)

            sig = {
                "pair": pair, "tier": row["tier"], "pattern": row["pattern"],
                "direction": row["direction"], "entry": limit, "sl": sl, "tp": tp,
                "sl_pip": sl_pip, "tp_pip": tp_pip, "lot": lot, "eta": eta,
                "created_at": now.isoformat(), "news_warn": warn, "spread": sp_val,
                "weak": weak, "corr_warn": corr_warn,
            }
            found.append(sig)
            state["recent_signals"][key] = now.isoformat()

    return found, news

# ============================================================
# TELEGRAM MESSAGES
# ============================================================
def msg_signal(sig):
    arrow = "🟢" if sig["direction"] == "BUY" else "🔴"
    lines = [
        f"🎯 <b>TIER {sig['tier']} SIGNAL</b> @ {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
        "━━━━━━━━━━━━━━━━━━━",
        f"Pair: <b>{sig['pair']}</b>",
        f"Pattern: <code>{sig['pattern']}</code> {arrow} {sig['direction']}",
        f"Limit Entry: <b>{fmt_price(sig['pair'], sig['entry'])}</b>",
        f"SL: {fmt_price(sig['pair'], sig['sl'])} ({sig['sl_pip']} pip)",
        f"TP: {fmt_price(sig['pair'], sig['tp'])} ({sig['tp_pip']} pip)",
        f"Lot: {sig['lot']}",
        f"ETA: ~{sig['eta']} min",
        "━━━━━━━━━━━━━━━━━━━",
    ]
    if sig.get("weak"):
        lines.append("⚠️ <b>Weak signal</b> — 15m trend disagrees")
    if sig.get("corr_warn"):
        lines.append("⚠️ Correlated pair open same direction")
    if sig.get("news_warn"):
        lines.append(sig["news_warn"])
    else:
        lines.append("News: ✅ Safe")
    sp = sig.get("spread") or 0.0
    lines.append(f"Spread: {sp:.1f} pip")
    return "\n".join(lines)

def msg_scan(state, sigs, reason=""):
    now = datetime.now(timezone.utc).strftime('%H:%M UTC')
    next_t = (datetime.now(timezone.utc) + timedelta(minutes=HEARTBEAT_MIN)).strftime('%H:%M UTC')
    sess_now = []
    for s, label in [("sess_tokyo", "Tokyo"), ("sess_london", "London"),
                     ("ny_am", "NY-AM"), ("ny_pm", "NY-PM")]:
        if in_session(s): sess_now.append(label)
    sess_str = ", ".join(sess_now) if sess_now else "None"
    txt = (f"⏰ <b>Scan</b> @ {now}\n"
           f"Pairs: EURUSD, GBPUSD, USDJPY\n"
           f"Signals: {len(sigs)} new | Open: {len(state['open_trades'])}\n"
           f"Session: {sess_str}\n"
           f"Next: {next_t}\n"
           f"Bot: 🟢 Running")
    if reason:
        txt += f"\nNote: {reason}"
    return txt

def msg_heartbeat(state):
    d = state["daily"]
    top = sorted(state.get("setup_perf", {}).items(),
                 key=lambda kv: kv[1]["pnl_pip"], reverse=True)[:3]
    top_str = ""
    if top:
        top_str = "\n<b>Top setups:</b>\n" + "\n".join(
            f"  • {k}: {v['wins']}W/{v['losses']}L ({v['pnl_pip']:+.0f}p)"
            for k, v in top)
    return (f"💓 <b>Heartbeat</b> @ {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"
            f"Open: {len(state['open_trades'])} | Today: {d['trades']} trades\n"
            f"Day P/L: {d['pnl_pip']:+.1f} pip (${d['pnl_usd']:+.2f})\n"
            f"W/L: {d['wins']}/{d['losses']} | Consec L: {state['consec_losses']}"
            f"{top_str}")

def msg_tp(pair, pips, usd, day_total):
    return (f"🎯 <b>TP HIT</b> {pair} +{pips:.0f} pip\n"
            f"Profit: +${usd:.2f}\n"
            f"Day total: ${day_total:+.2f}")

def msg_sl(pair, pips, usd, consec, emoji):
    return (f"❌ <b>SL HIT</b> {pair} -{abs(pips):.0f} pip\n"
            f"Loss: -${abs(usd):.2f}\n"
            f"⚠️ Warning: Loss #{consec}. {emoji}\n"
            f"Bot: 🟢 Running")

def msg_warning(text):
    return f"⚠️ <b>WARNING</b>\n{text}\nBot: 🟢 Running"

def msg_proximity(tr, cur_price, distance_pip):
    return (f"🔔 <b>SIGNAL APPROACHING</b>\n"
            f"Pair: <b>{tr['pair']}</b> {tr['direction']}\n"
            f"Entry: {fmt_price(tr['pair'], tr['entry'])}\n"
            f"Current: {fmt_price(tr['pair'], cur_price)}\n"
            f"Distance: {distance_pip:.1f} pip\n"
            f"Fill hone wala hai — ready raho.")

# ============================================================
# TRADE MONITOR
# ============================================================
def monitor_trades(state):
    if not state["open_trades"]:
        return
    remaining = []
    now = datetime.now(timezone.utc)

    for tr in state["open_trades"]:
        pair = tr["pair"]
        df5  = fetch_td(TD_SYMBOL[pair], "5min", 50)
        if df5 is None or len(df5) == 0:
            remaining.append(tr); continue

        pip = PIP[pair]

        if tr["status"] == "limit_pending":
            try:
                created = datetime.fromisoformat(tr["created_at"].replace("Z","+00:00"))
                if (now - created) > timedelta(minutes=SIGNAL_EXPIRY_MIN):
                    tg_send(f"⏱️ <b>Signal expired</b> — {pair} {tr['pattern']} no fill")
                    continue
            except Exception: pass

            try:
                since = df5[df5["datetime"] >= pd.Timestamp(tr["created_at"])]
            except Exception:
                since = df5.tail(3)
            if len(since) == 0:
                since = df5.tail(3)

            filled = False
            for _, row in since.iterrows():
                if tr["direction"] == "BUY" and float(row["low"]) <= tr["entry"]:
                    filled = True; break
                if tr["direction"] == "SELL" and float(row["high"]) >= tr["entry"]:
                    filled = True; break
            if filled:
                tr["status"] = "open"
                tr["filled_at"] = now.isoformat()
                tr["last_check_ts"] = tr["created_at"]
                tg_send(f"✅ <b>FILLED</b> {pair} {tr['direction']} @ {fmt_price(pair, tr['entry'])}")
            remaining.append(tr); continue

        last_check = tr.get("last_check_ts") or tr.get("filled_at") or tr["created_at"]
        try:
            since = df5[df5["datetime"] > pd.Timestamp(last_check)]
        except Exception:
            since = df5.tail(5)
        if len(since) == 0:
            since = df5.tail(1)

        tp_price = tr["tp"]; sl_price = tr["sl"]
        hit_tp = hit_sl = False

        for _, row in since.iterrows():
            h, l = float(row["high"]), float(row["low"])
            if tr["direction"] == "BUY":
                if l <= sl_price: hit_sl = True; break
                if h >= tp_price: hit_tp = True; break
            else:
                if h >= sl_price: hit_sl = True; break
                if l <= tp_price: hit_tp = True; break

        tr["last_check_ts"] = now.isoformat()

        if not tr.get("partial_done"):
            one_r = (tr["entry"] + tr["sl_pip"]*pip) if tr["direction"]=="BUY" \
                    else (tr["entry"] - tr["sl_pip"]*pip)
            for _, row in since.iterrows():
                h, l = float(row["high"]), float(row["low"])
                if (tr["direction"]=="BUY" and h >= one_r) or \
                   (tr["direction"]=="SELL" and l <= one_r):
                    tr["partial_done"] = True
                    pnl_pip = tr["sl_pip"]
                    pnl_usd = pnl_pip * PIP_VALUE_LOT[pair] * (tr["lot"] * 0.5)
                    state["daily"]["pnl_pip"] += pnl_pip
                    state["daily"]["pnl_usd"] += pnl_usd
                    tg_send(f"💰 Partial 50% {pair} +{pnl_pip} pip (+${pnl_usd:.2f})")
                    break

        if not tr.get("breakeven_moved"):
            be_at = (tr["entry"] + 20*pip) if tr["direction"]=="BUY" \
                    else (tr["entry"] - 20*pip)
            for _, row in since.iterrows():
                h, l = float(row["high"]), float(row["low"])
                if (tr["direction"]=="BUY" and h >= be_at) or \
                   (tr["direction"]=="SELL" and l <= be_at):
                    tr["sl"] = tr["entry"]
                    tr["breakeven_moved"] = True
                    tg_send(f"🛡️ SL moved to BE {pair}")
                    break

        perf_key = f"{pair}_{tr['pattern']}_{tr['direction']}"
        sp = state["setup_perf"].setdefault(perf_key,
                {"wins":0,"losses":0,"pnl_pip":0.0})

        if hit_tp:
            pnl_pip = tr["tp_pip"] if not tr.get("partial_done") else tr["tp_pip"]*0.5
            pnl_usd = pnl_pip * PIP_VALUE_LOT[pair] * tr["lot"]
            state["daily"]["pnl_pip"] += pnl_pip
            state["daily"]["pnl_usd"] += pnl_usd
            state["daily"]["wins"]   += 1
            state["daily"].setdefault("pair_trades", {})
            state["daily"]["pair_trades"][pair] = state["daily"]["pair_trades"].get(pair,0)+1
            state["consec_losses"] = 0
            sp["wins"] += 1; sp["pnl_pip"] += pnl_pip
            tg_send(msg_tp(pair, pnl_pip, pnl_usd, state["daily"]["pnl_usd"]))
            continue

        if hit_sl:
            if tr.get("breakeven_moved"):
                real_pip = 0
            elif tr.get("partial_done"):
                real_pip = -tr["sl_pip"] * 0.5
            else:
                real_pip = -tr["sl_pip"]
            pnl_usd = real_pip * PIP_VALUE_LOT[pair] * tr["lot"]
            state["daily"]["pnl_pip"] += real_pip
            state["daily"]["pnl_usd"] += pnl_usd
            state["daily"]["losses"] += 1
            state["daily"].setdefault("pair_trades", {})
            state["daily"]["pair_trades"][pair] = state["daily"]["pair_trades"].get(pair,0)+1
            if not tr.get("breakeven_moved"):
                state["consec_losses"] += 1
            sp["losses"] += 1; sp["pnl_pip"] += real_pip
            emoji = "Market choppy." if state["consec_losses"] < MAX_CONSEC_LOSSES else "Careful!"
            tg_send(msg_sl(pair, abs(real_pip), pnl_usd, state["consec_losses"], emoji))
            if state["consec_losses"] >= MAX_CONSEC_LOSSES:
                tg_send(msg_warning(f"{MAX_CONSEC_LOSSES} consecutive losses. Stay alert."))
            if state["daily"]["pnl_usd"] <= -ACCOUNT_BAL * MAX_DAILY_LOSS_PCT and \
               not state["daily"].get("loss_warned"):
                tg_send(msg_warning(f"Daily loss > {MAX_DAILY_LOSS_PCT*100:.0f}%. Manage size."))
                state["daily"]["loss_warned"] = True
            continue

        try:
            opened = datetime.fromisoformat((tr.get("filled_at") or tr["created_at"]).replace("Z","+00:00"))
            if (now - opened) > timedelta(hours=TRADE_MAX_HOURS):
                tg_send(f"⏱️ Time exit {pair} after {TRADE_MAX_HOURS}h")
                continue
        except Exception: pass

        remaining.append(tr)

    state["open_trades"] = remaining

# ============================================================
# FLIP
# ============================================================
def try_flip(state, sig):
    flip = None
    for t in state["open_trades"]:
        if t["pair"] == sig["pair"] and t["direction"] != sig["direction"]:
            flip = t; break
    if not flip:
        return False

    tg_send(f"🔄 <b>FLIP</b> {sig['pair']}: closing {flip['direction']} → opening {sig['direction']}")

    try:
        cur, _ = fetch_live_price(sig["pair"])
        if cur is None:
            df_now = fetch_td(TD_SYMBOL[sig["pair"]], "5min", 5)
            cur = float(df_now["close"].iloc[-1]) if df_now is not None else flip["entry"]
        pip = PIP[sig["pair"]]
        raw = ((cur - flip["entry"]) / pip) if flip["direction"]=="BUY" \
              else ((flip["entry"] - cur) / pip)
        pnl_usd = raw * PIP_VALUE_LOT[sig["pair"]] * flip["lot"]
        state["daily"]["pnl_pip"] += raw
        state["daily"]["pnl_usd"] += pnl_usd
        state["daily"]["trades"] += 1
    except Exception as e:
        log("flip close err", e)

    state["open_trades"].remove(flip)
    return True

# ============================================================
# PROXIMITY LOOP (1-min)
# ============================================================
def proximity_check(state):
    for tr in state["open_trades"]:
        if tr["status"] != "limit_pending": continue
        if tr.get("proximity_alerted"): continue
        pair = tr["pair"]
        cur, _ = fetch_live_price(pair)
        if cur is None: continue
        pip = PIP[pair]
        dist = abs(cur - tr["entry"]) / pip
        if dist <= PROXIMITY_ALERT_PIPS:
            tg_send(msg_proximity(tr, cur, dist))
            tr["proximity_alerted"] = True

def internal_loop(state):
    end_time = datetime.now(timezone.utc) + timedelta(minutes=LOOP_MIN)
    while datetime.now(timezone.utc) < end_time:
        try:
            monitor_trades(state)
        except Exception as e:
            log("loop monitor err", e)
        try:
            proximity_check(state)
        except Exception as e:
            log("proximity err", e)
        save_state(state)
        time.sleep(TICK_SEC)

# ============================================================
# MAIN
# ============================================================
def main():
    log("=== Currency Sniper Bot v2.1 ===")
    state = ensure_state()

    try:
        monitor_trades(state)
    except Exception as e:
        log("monitor err", e); traceback.print_exc()

    sigs = []
    try:
        sigs, news = scan_signals(state)
    except Exception as e:
        log("scan err", e); traceback.print_exc()

    for s in sigs:
        if len(state["open_trades"]) >= MAX_OPEN_TRADES:
            if not try_flip(state, s):
                log("max trades reached, skip"); break
        else:
            try_flip(state, s)

        if len(state["open_trades"]) >= MAX_OPEN_TRADES:
            break

        tg_send(msg_signal(s))
        state["open_trades"].append({
            "pair": s["pair"], "direction": s["direction"],
            "entry": s["entry"], "sl": s["sl"], "tp": s["tp"],
            "sl_pip": s["sl_pip"], "tp_pip": s["tp_pip"],
            "lot": s["lot"], "tier": s["tier"], "pattern": s["pattern"],
            "status": "limit_pending",
            "created_at": s["created_at"],
            "breakeven_moved": False, "partial_done": False,
            "proximity_alerted": False,
            "last_check_ts": s["created_at"],
        })
        state["daily"]["trades"] += 1

    now = datetime.now(timezone.utc)
    last_scan = state.get("last_scan_msg","")
    send_scan = True
    try:
        if last_scan:
            t = datetime.fromisoformat(last_scan.replace("Z","+00:00"))
            send_scan = (now - t) >= timedelta(minutes=HEARTBEAT_MIN)
    except Exception: pass
    if send_scan:
        tg_send(msg_scan(state, sigs))
        state["last_scan_msg"] = now.isoformat()

    last_hb = state.get("last_heartbeat","")
    send_hb = True
    try:
        if last_hb:
            t = datetime.fromisoformat(last_hb.replace("Z","+00:00"))
            send_hb = (now - t) >= timedelta(minutes=HEARTBEAT_MIN)
    except Exception: pass
    if send_hb:
        tg_send(msg_heartbeat(state))
        state["last_heartbeat"] = now.isoformat()

    save_state(state)

    try:
        internal_loop(state)
    except Exception as e:
        log("loop err", e)

    save_state(state)
    log("done. signals:", len(sigs), "open:", len(state["open_trades"]))

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("FATAL", e)
        traceback.print_exc()
        try: tg_send(f"🚨 Bot error: {str(e)[:200]}\nBot will retry next run.")
        except Exception: pass
