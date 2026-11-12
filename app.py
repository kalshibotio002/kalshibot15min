import os, time, sqlite3, subprocess, json, hmac, smtplib, platform
from email.message import EmailMessage
from datetime import datetime, timezone, timedelta
import requests
import pandas as pd
import numpy as np
import streamlit as st
import plotly.graph_objects as go
from streamlit_autorefresh import st_autorefresh

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(APP_DIR, "bot_data.sqlite3"))
COINBASE = "https://api.exchange.coinbase.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXBTC15M"


st.set_page_config(page_title="BTC 15m Signal Desk", page_icon="₿", layout="wide")

# Optional shared-password gate. Set APP_PASSWORD in the hosting service's
# environment variables before making the dashboard public.
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()
if APP_PASSWORD:
    if not st.session_state.get("authenticated", False):
        st.title("₿ BTC 15-minute Signal Desk")
        st.caption("Private dashboard sign-in")
        with st.form("login_form"):
            entered = st.text_input("Dashboard password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            if hmac.compare_digest(entered, APP_PASSWORD):
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("That password did not match. Please try again.")
        st.stop()
else:
    st.warning("Dashboard password is not configured. Set APP_PASSWORD in your hosting environment before sharing the link.")


def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, direction TEXT, score REAL,
        entry_price REAL, expiry TEXT, status TEXT DEFAULT 'OPEN', exit_price REAL,
        result TEXT, source TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS candles (
        ts INTEGER PRIMARY KEY, open REAL, high REAL, low REAL, close REAL, volume REAL
    )""")
    con.commit()
    return con

def fetch_coinbase_candles(hours=72):
    # Coinbase Exchange limits candle responses to roughly 300 bars per request.
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    chunks, cursor = [], start
    while cursor < end:
        chunk_end = min(cursor + timedelta(minutes=270), end)
        params = {"granularity": 60, "start": cursor.isoformat(), "end": chunk_end.isoformat()}
        r = requests.get(f"{COINBASE}/products/BTC-USD/candles", params=params, timeout=12,
                         headers={"User-Agent":"BTC15mSignalDesk/1.0"})
        r.raise_for_status()
        rows = r.json()
        if rows:
            chunks.extend(rows)
        cursor = chunk_end
        time.sleep(0.12)
    # Coinbase rows: [time, low, high, open, close, volume]
    df = pd.DataFrame(chunks, columns=["ts","low","high","open","close","volume"])
    df = df.sort_values("ts").drop_duplicates("ts")
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_numeric(df["ts"], errors="coerce").astype("int64")
    return df.dropna().reset_index(drop=True)

def features(df):
    x = df.copy()
    x["ema_fast"] = x.close.ewm(span=9, adjust=False).mean()
    x["ema_slow"] = x.close.ewm(span=21, adjust=False).mean()
    x["ema_trend"] = x.close.ewm(span=50, adjust=False).mean()
    delta = x.close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    x["rsi"] = (100 - (100/(1+rs))).fillna(50)
    x["ret"] = x.close.pct_change()
    x["volatility"] = x.ret.rolling(20).std() * np.sqrt(60*24)
    x["momentum_5"] = x.close.pct_change(5)
    x["momentum_15"] = x.close.pct_change(15)
    x["atr"] = (x.high-x.low).rolling(14).mean()
    return x

def score_signal(row):
    """
    Weighted rule score, NOT a win probability.
    Every filter is mandatory for an entry; score >= 80 alone is insufficient.
    """
    direction = "WAIT"
    if (row.ema_fast > row.ema_slow > row.ema_trend) and (row.close > row.ema_trend):
        direction = "UP"
    elif (row.ema_fast < row.ema_slow < row.ema_trend) and (row.close < row.ema_trend):
        direction = "DOWN"

    if direction == "WAIT":
        return "WAIT", 0.0, "EMA 9/21/50 trend alignment failed"

    if direction == "UP":
        rsi_ok = 52 <= row.rsi <= 68
        mom5_ok = row.momentum_5 > 0
        mom15_ok = row.momentum_15 > 0
    else:
        rsi_ok = 32 <= row.rsi <= 48
        mom5_ok = row.momentum_5 < 0
        mom15_ok = row.momentum_15 < 0

    # Volatility is the rolling 1-minute realized standard deviation,
    # annualized to a daily scale. Bounds are configurable starter defaults.
    vol_ok = pd.notna(row.volatility) and 0.005 < row.volatility < 0.15

    score = (
        25 * 1.0 +                 # EMA trend alignment
        20 * float(rsi_ok) +
        20 * float(mom5_ok) +
        20 * float(mom15_ok) +
        15 * float(vol_ok)
    )

    failures = []
    if not rsi_ok: failures.append("RSI")
    if not mom5_ok: failures.append("5m momentum")
    if not mom15_ok: failures.append("15m momentum")
    if not vol_ok: failures.append("volatility")

    # Mandatory filters remain mandatory even if weighted score reaches 80.
    if score < 80 or failures:
        return "WAIT", score / 100, "Mandatory filter failed: " + ", ".join(failures)

    return direction, score / 100, "All mandatory filters passed"

def notify_user(message):
    """
    Hosted deployment: email an alert if SMTP settings are configured.
    Local Mac: otherwise show a macOS notification.
    """
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    email_from = os.getenv("ALERT_EMAIL_FROM", smtp_user or "")
    email_to = os.getenv("ALERT_EMAIL_TO")

    if all([smtp_host, smtp_user, smtp_password, email_from, email_to]):
        try:
            msg = EmailMessage()
            msg["Subject"] = "BTC 15m Signal Desk alert"
            msg["From"] = email_from
            msg["To"] = email_to
            msg.set_content(message)
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                server.starttls()
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
            return
        except Exception as exc:
            print(f"Email alert failed: {exc}")

    if platform.system() == "Darwin":
        try:
            safe = message.replace('"', '\\"')
            subprocess.run(
                ["osascript", "-e", f'display notification "{safe}" with title "BTC 15m Signal Desk"'],
                timeout=3, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception:
            pass


def fetch_kalshi_open():
    try:
        r = requests.get(f"{KALSHI}/markets", params={"series_ticker": SERIES, "status":"open", "limit":20},
                         timeout=10, headers={"User-Agent":"BTC15mSignalDesk/1.0"})
        r.raise_for_status()
        return r.json().get("markets", [])
    except Exception:
        return []

def get_market_info(markets):
    if not markets:
        return None
    now = time.time()
    live = [m for m in markets if m.get("close_time") and
            datetime.fromisoformat(m["close_time"].replace("Z","+00:00")).timestamp() > now]
    if not live:
        live = markets
    live.sort(key=lambda m: m.get("close_time",""))
    return live[0] if live else None

def save_candles(df):
    con = db()
    con.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?)",
                    df[["ts","open","high","low","close","volume"]].itertuples(index=False, name=None))
    con.commit()
    con.close()

def save_signal(direction, score, price, expiry, source):
    con = db()
    con.execute("INSERT INTO signals(ts,direction,score,entry_price,expiry,status,source) VALUES(?,?,?,?,?,'OPEN',?)",
                (datetime.now(timezone.utc).isoformat(), direction, float(score), float(price), expiry, source))
    con.commit()
    con.close()

def settle_due(latest_price):
    con = db()
    rows = con.execute("SELECT id,direction,entry_price,expiry FROM signals WHERE status='OPEN'").fetchall()
    now = datetime.now(timezone.utc)
    for sid, direction, entry, expiry in rows:
        try:
            exp = datetime.fromisoformat(expiry.replace("Z","+00:00"))
        except Exception:
            continue
        if now >= exp:
            # Directional paper proxy: compares latest spot with signal-time spot.
            # Not an official Kalshi settlement calculation.
            result = "WIN" if ((latest_price > entry and direction=="UP") or
                               (latest_price < entry and direction=="DOWN")) else "LOSS"
            con.execute("UPDATE signals SET status='SETTLED', exit_price=?, result=? WHERE id=?",
                        (float(latest_price), result, sid))
    con.commit()
    con.close()

def run_backtest(df, threshold=0.8):
    x = features(df).dropna().reset_index(drop=True)
    outcomes = []
    # Signals on each completed minute; outcome is close 15 minutes later.
    for i in range(60, len(x)-15):
        row = x.iloc[i]
        direction, score, _ = score_signal(row)
        if direction == "WAIT" or score < threshold:
            continue
        future = x.iloc[i+15].close
        won = (future > row.close and direction=="UP") or (future < row.close and direction=="DOWN")
        outcomes.append((x.iloc[i].ts, direction, row.close, future, bool(won)))
    return pd.DataFrame(outcomes, columns=["ts","direction","entry","exit","win"])

st.title("₿ BTC 15-minute Signal Desk")
st.caption("Private cloud dashboard · Use on phone, tablet, or computer · Paper trading only")

with st.sidebar:
    st.header("Controls")
    refresh = st.selectbox("Refresh interval", [5, 10, 15, 30], index=1, format_func=lambda v:f"{v} seconds")
    min_score = st.slider("Minimum rule score", 0.6, 1.0, 0.8, 0.05)
    enable_alerts = st.toggle("Signal alerts", value=True)
    auto_paper = st.toggle("Automatically paper-enter new signals", value=True)
    st.caption("Signals use Coinbase BTC-USD spot candles as a market-data proxy. Kalshi market metadata is displayed separately; the score is not a probability.")

st_autorefresh(interval=refresh*1000, key="refresh")
status_box = st.empty()
try:
    raw = fetch_coinbase_candles(6)
    save_candles(raw)
    data = features(raw)
    latest = data.iloc[-1]
    price = float(latest.close)
    direction, score, reason = score_signal(latest)
    markets = fetch_kalshi_open()
    market = get_market_info(markets)
    if market:
        expiry = market.get("close_time")
    else:
        # Stable next 15-minute UTC boundary, not a moving now+15m timestamp.
        now_utc = datetime.now(timezone.utc)
        next_minute = ((now_utc.minute // 15) + 1) * 15
        boundary = now_utc.replace(second=0, microsecond=0)
        if next_minute == 60:
            boundary = boundary.replace(minute=0) + timedelta(hours=1)
        else:
            boundary = boundary.replace(minute=next_minute)
        expiry = boundary.isoformat()

    # One paper entry total per market expiry, regardless of direction.
    con = db()
    already_entered = con.execute(
        "SELECT 1 FROM signals WHERE expiry = ? LIMIT 1", (expiry,)
    ).fetchone()
    con.close()
    if auto_paper and direction in ("UP","DOWN") and score >= min_score and not already_entered:
        save_signal(direction, score, price, expiry, "Coinbase BTC-USD spot proxy")
        if enable_alerts:
            notify_user(f"{direction} signal · rule score {score:.0%} · BTC ${price:,.2f}")
    settle_due(price)

    cols = st.columns(4)
    cols[0].metric("BTC-USD spot", f"${price:,.2f}", f"{latest.momentum_5*100:+.3f}% / 5m")
    cols[1].metric("Current signal", direction, f"Rule score {score:.0%}")
    cols[2].metric("RSI (14)", f"{latest.rsi:.1f}")
    cols[3].metric("Daily realized volatility", f"{latest.volatility:.1%}" if pd.notna(latest.volatility) else "—")
    if direction == "UP":
        st.success(f"UP signal candidate · {reason}")
    elif direction == "DOWN":
        st.error(f"DOWN signal candidate · {reason}")
    else:
        st.info(f"WAIT · {reason}")

    m1, m2, m3 = st.columns(3)
    if market:
        m1.metric("Kalshi market", market.get("ticker","—"))
        m2.metric("Kalshi YES bid", f"{market.get('yes_bid','—')}¢")
        m3.metric("Kalshi closes", market.get("close_time","—"))
        with st.expander("Kalshi market details"):
            st.write(market.get("title",""))
            st.write("Market rules:", market.get("rules_primary","Not returned by public market endpoint"))
    else:
        st.warning("Could not retrieve an open KXBTC15M market. Spot signals still work; verify the active contract and its rules on Kalshi.")

    chart = go.Figure()
    chart.add_trace(go.Candlestick(x=pd.to_datetime(data.ts, unit="s", utc=True),
                                   open=data.open, high=data.high, low=data.low, close=data.close,
                                   name="BTC-USD"))
    chart.add_trace(go.Scatter(x=pd.to_datetime(data.ts, unit="s", utc=True), y=data.ema_fast,
                               name="EMA 9", line=dict(width=1)))
    chart.add_trace(go.Scatter(x=pd.to_datetime(data.ts, unit="s", utc=True), y=data.ema_slow,
                               name="EMA 21", line=dict(width=1)))
    chart.update_layout(height=430, xaxis_rangeslider_visible=False, margin=dict(l=10,r=10,t=20,b=10))
    st.plotly_chart(chart, use_container_width=True)

    con = db()
    hist = pd.read_sql_query("SELECT * FROM signals ORDER BY id DESC LIMIT 100", con)
    con.close()
    settled = hist[hist.status=="SETTLED"] if not hist.empty else hist
    wins = int((settled.result=="WIN").sum()) if not settled.empty else 0
    losses = int((settled.result=="LOSS").sum()) if not settled.empty else 0
    rate = wins/(wins+losses) if wins+losses else 0
    k1,k2,k3,k4 = st.columns(4)
    k1.metric("Paper wins", wins)
    k2.metric("Paper losses", losses)
    k3.metric("Observed win rate", f"{rate:.1%}" if wins+losses else "—")
    k4.metric("Open paper positions", int((hist.status=="OPEN").sum()) if not hist.empty else 0)
    st.subheader("Paper-trade ledger")
    if not hist.empty:
        st.dataframe(hist, use_container_width=True, hide_index=True)
    else:
        st.info("No paper trades yet. A qualifying signal will create one automatically.")

    st.subheader("Historical backtest")
    bt_hours = st.selectbox("Backtest lookback", [24, 72], index=1, format_func=lambda h:f"Last {h} hours")
    if st.button("Run backtest", type="primary"):
        bt_raw = fetch_coinbase_candles(bt_hours)
        bt = run_backtest(bt_raw, min_score)
        if bt.empty:
            st.warning("No qualifying signals in this sample. Try a longer lookback or lower score threshold.")
        else:
            wr = bt.win.mean()
            a,b,c = st.columns(3)
            a.metric("Backtest signals", len(bt))
            b.metric("Backtest win rate", f"{wr:.1%}")
            c.metric("Wins / losses", f"{int(bt.win.sum())} / {int((~bt.win).sum())}")
            st.dataframe(bt.tail(100), use_container_width=True, hide_index=True)
            st.caption("Backtest uses the next 15-minute Coinbase close as a directional proxy; it excludes Kalshi spreads, fees, slippage, and official CF Benchmarks settlement.")
except Exception as e:
    st.error(f"Data connection error: {e}")
    st.info("Check your internet connection, then refresh. Coinbase public market data is used for the live spot feed.")

st.divider()
st.caption("Risk note: This is an experimental research tool, not financial advice. A signal score is not a calibrated probability or a promise of accuracy. Kalshi crypto markets can settle using CF Benchmarks index rules and averaging windows; Coinbase spot prices are only a proxy. Always inspect the exact contract rules before acting.")
