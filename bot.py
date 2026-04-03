#!/usr/bin/env python3
"""
Trading Agent Bot — Hourly monitor + execution
Goal: $40 → $1000 in 30 days
"""

import jwt, time, secrets, requests, json, uuid, os, sys
from datetime import datetime, timezone
from cryptography.hazmat.primitives.serialization import load_pem_private_key

# ── Config ──────────────────────────────────────────────────────────────────
KEY_NAME = "organizations/554326fb-7744-4bae-a194-1b96ee7e9c58/apiKeys/c6238882-932b-4f8e-a966-31cb7686b56a"
PRIVATE_KEY_PEM = b"""-----BEGIN EC PRIVATE KEY-----
MHcCAQEEII26+gatABTgrcnIDmElCKh+1h32DvlHgB35nktG1VN2oAoGCCqGSM49
AwEHoUQDQgAEP/CbGkm6WWfACVqLR3A7u5SUu2gQKT3JDhR6f7EX/0mFKtRpl6+Y
PnRwZbrPVoZUJsGxlxNaTcz7mIF49PTn9Q==
-----END EC PRIVATE KEY-----"""

PORTFOLIO_UUID = "a0174166-1f88-51f5-aa9f-0970696063f1"
START_BALANCE = 40.00
START_DATE = "2026-03-29"
TARGET = 1000.00

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
STRATEGY_FILE = os.path.join(os.path.dirname(__file__), "strategy.json")

def load_strategy():
    if os.path.exists(STRATEGY_FILE):
        with open(STRATEGY_FILE) as f:
            return json.load(f)
    return {"take_profit_pct": 10.0, "stop_loss_pct": 5.0, "momentum_threshold_1h": 0.3,
            "max_position_pct": 60.0, "preferred_asset": "ETH-USD"}

# Position tracking (loaded from state file)
DEFAULT_STATE = {
    "positions": {},  # asset -> {qty, entry, stop_loss, target, size_usd, order_id}
    "trades": [],
    "balance_history": [{"time": START_DATE, "value": START_BALANCE}],
    "start_balance": START_BALANCE,
    "day": 1,
}

# ── Coinbase API ─────────────────────────────────────────────────────────────
def make_jwt(method, path):
    private_key = load_pem_private_key(PRIVATE_KEY_PEM, password=None)
    payload = {
        "sub": KEY_NAME, "iss": "cdp",
        "nbf": int(time.time()), "exp": int(time.time()) + 120,
        "uri": f"{method} api.coinbase.com{path}"
    }
    return jwt.encode(payload, private_key, algorithm="ES256",
                      headers={"kid": KEY_NAME, "nonce": secrets.token_hex(16)})

def cb_get(path, params=None):
    # Strip query string from path for JWT signing (JWT uri must be path only)
    path_for_jwt = path.split("?")[0]
    token = make_jwt("GET", path_for_jwt)
    r = requests.get(f"https://api.coinbase.com{path}",
                     headers={"Authorization": f"Bearer {token}"},
                     params=params, timeout=10)
    if not r.text:
        return {}
    return r.json()

def cb_post(path, body):
    token = make_jwt("POST", path)
    r = requests.post(f"https://api.coinbase.com{path}",
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      json=body, timeout=10)
    return r.status_code, r.json()

def get_portfolio():
    data = cb_get(f"/api/v3/brokerage/portfolios/{PORTFOLIO_UUID}")
    breakdown = data.get("breakdown", {})
    balances = breakdown.get("portfolio_balances", {})
    total = float(balances.get("total_balance", {}).get("value", 0))
    cash = float(balances.get("total_cash_equivalent_balance", {}).get("value", 0))
    positions = {}
    for pos in breakdown.get("spot_positions", []):
        val = float(pos.get("total_balance_fiat", 0))
        qty = float(pos.get("total_balance_crypto", 0))
        asset = pos.get("asset", "")
        if val > 0.01:
            positions[asset] = {"qty": qty, "fiat_value": val}
    return total, cash, positions

def get_price(product_id):
    """Get current price for a product"""
    data = cb_get(f"/api/v3/brokerage/products/{product_id}")
    return float(data.get("price", 0))

def market_buy(product_id, quote_size_usd):
    """Buy $X worth of an asset"""
    status, result = cb_post("/api/v3/brokerage/orders", {
        "client_order_id": str(uuid.uuid4()),
        "product_id": product_id,
        "side": "BUY",
        "order_configuration": {"market_market_ioc": {"quote_size": str(round(quote_size_usd, 2))}}
    })
    return status, result

def market_sell(product_id, base_size):
    """Sell X units of an asset"""
    status, result = cb_post("/api/v3/brokerage/orders", {
        "client_order_id": str(uuid.uuid4()),
        "product_id": product_id,
        "side": "SELL",
        "order_configuration": {"market_market_ioc": {"base_size": str(base_size)}}
    })
    return status, result

def get_order(order_id):
    data = cb_get(f"/api/v3/brokerage/orders/historical/{order_id}")
    return data.get("order", {})

# ── State management ──────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return dict(DEFAULT_STATE)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Market research ───────────────────────────────────────────────────────────
def get_crypto_prices():
    """Fetch prices with 1h/24h changes. Primary: CoinGecko markets. Fallback: Coinbase prices."""
    # Primary: CoinGecko markets endpoint (has 1h data)
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets"
            "?vs_currency=usd&ids=bitcoin,ethereum,solana,ripple"
            "&price_change_percentage=1h,24h",
            timeout=8
        )
        if r.status_code == 200:
            raw = r.json()
            mapping = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "ripple": "XRP"}
            result = {}
            for d in raw:
                cid = d.get("id", "")
                sym = mapping.get(cid)
                if sym:
                    result[sym] = {
                        "price": d.get("current_price", 0),
                        "change_1h": d.get("price_change_percentage_1h_in_currency", 0) or 0,
                        "change_24h": d.get("price_change_percentage_24h", 0) or 0,
                        "volume": d.get("total_volume", 0),
                    }
            if result:
                return result
    except Exception:
        pass

    # Fallback: CoinGecko simple price (no 1h, but better than nothing)
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin,ethereum,solana,ripple"
            "&vs_currencies=usd&include_24hr_change=true",
            timeout=8
        )
        if r.status_code == 200:
            raw = r.json()
            mapping = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "ripple": "XRP"}
            return {mapping[k]: {
                "price": v.get("usd", 0),
                "change_24h": v.get("usd_24h_change", 0),
                "change_1h": 0,
            } for k, v in raw.items() if k in mapping}
    except Exception:
        pass

    # Fallback 2: CryptoCompare (free, no key, has hourly OHLCV)
    try:
        syms = "ETH,BTC,SOL,XRP"
        r = requests.get(
            f"https://min-api.cryptocompare.com/data/pricemultifull?fsyms={syms}&tsyms=USD",
            timeout=8
        )
        if r.status_code == 200:
            raw = r.json().get("RAW", {})
            result = {}
            for sym in ["ETH", "BTC", "SOL", "XRP"]:
                d = raw.get(sym, {}).get("USD", {})
                if d:
                    price = d.get("PRICE", 0)
                    open_day = d.get("OPENDAY", price)
                    open_hour = d.get("OPENHOUR", price)
                    change_24h = ((price - open_day) / open_day * 100) if open_day else 0
                    change_1h = ((price - open_hour) / open_hour * 100) if open_hour else 0
                    result[sym] = {
                        "price": price,
                        "change_1h": change_1h,
                        "change_24h": change_24h,
                        "volume": d.get("TOTALVOLUME24HTO", 0),
                    }
            if result:
                return result
    except Exception:
        pass

    # Last resort: Coinbase prices only (no % change)
    result = {}
    for sym, product in [("ETH", "ETH-USD"), ("BTC", "BTC-USD"), ("SOL", "SOL-USD"), ("XRP", "XRP-USD")]:
        try:
            p = get_price(product)
            result[sym] = {"price": p, "change_1h": 0, "change_24h": 0, "volume": 0}
        except Exception:
            pass
    return result

# ── XRP Scalp logic ───────────────────────────────────────────────────────────
# Each run we compare current price to last-seen price to detect micro-moves.
# We enter on upswings > SCALP_ENTRY_MOVE_PCT and exit at SCALP_TARGET_PCT or SCALP_STOP_PCT.

SCALP_ASSET = "XRP-USD"
SCALP_SYMBOL = "XRP"
SCALP_ALLOC_USD = 5.0       # fixed $5 per scalp trade (small, frequent)
SCALP_TARGET_PCT = 4.5      # take profit at +4.5% (net ~3.3% after 1.2% round-trip fees)
SCALP_STOP_PCT = 1.5        # stop loss at -1.5% (net -2.7% after fees — 1.2:1 net ratio, BE ~45% WR)
SCALP_ENTRY_MOVE_PCT = 0.5  # enter if XRP moved up >=0.5% since last check (reduce noise)
SCALP_COOLDOWN_MINS = 5     # min minutes between scalp entries
SCALP_TIME_STOP_MINS = 60   # force-exit any scalp open longer than 60 minutes (scalp ≠ bag hold)
SCALP_MAX_AGE_MINS = 240    # ABSOLUTE max: kill any scalp older than 4 hours (stale position guard)

def check_scalp(state, prices, cash, total):
    """
    XRP micro-scalp layer.
    Runs every minute. Tracks price momentum and fires quick trades.
    Returns (action, message) where action is None | 'buy' | 'sell'
    """
    xrp_price = prices.get(SCALP_SYMBOL, {}).get("price", 0)
    if not xrp_price:
        try:
            xrp_price = get_price(SCALP_ASSET)
        except:
            return None, "XRP price unavailable"

    scalp = state.setdefault("scalp", {
        "position": None,
        "last_price": xrp_price,
        "last_entry_time": 0,
        "trades_today": 0,
        "wins": 0, "losses": 0
    })

    # Update last known price
    last_price = scalp.get("last_price", xrp_price)
    price_move_pct = (xrp_price - last_price) / last_price * 100 if last_price else 0
    scalp["last_price"] = xrp_price

    pos = scalp.get("position")

    # ── Check exit if in position ──
    if pos:
        entry = pos["entry"]
        pnl_pct = (xrp_price - entry) / entry * 100
        pos_age_mins = (time.time() - pos.get("entry_time", time.time())) / 60

        # Absolute max-age kill: stale scalp positions from crash/gap recovery
        if pos_age_mins >= SCALP_MAX_AGE_MINS:
            qty_str = f"{pos['qty']:.2f}"
            status, result = market_sell(SCALP_ASSET, qty_str)
            if result.get("success"):
                time.sleep(1)
                order = get_order(result["success_response"]["order_id"])
                exit_price = float(order.get("average_filled_price", xrp_price))
                realized_pnl = (exit_price - entry) * pos["qty"]
                scalp["position"] = None
                if realized_pnl >= 0:
                    scalp["wins"] += 1
                else:
                    scalp["losses"] += 1
                scalp["trades_today"] += 1
                state["trades"].append({
                    "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    "asset": "XRP", "side": "SELL",
                    "qty": pos["qty"], "entry": entry, "exit": exit_price,
                    "sizeUsd": pos["size_usd"], "pnl": realized_pnl,
                    "reason": f"SCALP STALE KILL — held {pos_age_mins:.0f}min (max: {SCALP_MAX_AGE_MINS}min) | P&L: {pnl_pct:+.2f}%",
                    "strategy_context": f"XRP scalp stale position killed | Entry ${entry:.4f} → Exit ${exit_price:.4f} | Age {pos_age_mins:.0f}min",
                    "signals": [f"scalp", f"stale-kill", f"XRP {pnl_pct:+.1f}%"]
                })
                return "sell", f"💀 XRP SCALP STALE KILL @ ${exit_price:.4f} | {pnl_pct:+.2f}% | {pos_age_mins:.0f}min open"

        # Time-stop: force-exit scalp that's been open too long (scalp ≠ bag hold)
        if pos_age_mins >= SCALP_TIME_STOP_MINS:
            qty_str = f"{pos['qty']:.2f}"
            status, result = market_sell(SCALP_ASSET, qty_str)
            if result.get("success"):
                time.sleep(1)
                order = get_order(result["success_response"]["order_id"])
                exit_price = float(order.get("average_filled_price", xrp_price))
                realized_pnl = (exit_price - entry) * pos["qty"]
                scalp["position"] = None
                if realized_pnl >= 0:
                    scalp["wins"] += 1
                else:
                    scalp["losses"] += 1
                scalp["trades_today"] += 1
                state["trades"].append({
                    "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    "asset": "XRP", "side": "SELL",
                    "qty": pos["qty"], "entry": entry, "exit": exit_price,
                    "sizeUsd": pos["size_usd"], "pnl": realized_pnl,
                    "reason": f"SCALP TIME-STOP — held {pos_age_mins:.0f}min (limit: {SCALP_TIME_STOP_MINS}min) | P&L: {pnl_pct:+.2f}%",
                    "strategy_context": f"XRP scalp time-stop | Entry ${entry:.4f} → Exit ${exit_price:.4f} | Age {pos_age_mins:.0f}min",
                    "signals": [f"scalp", f"time-stop", f"XRP {pnl_pct:+.1f}%"]
                })
                return "sell", f"⏱️ XRP SCALP TIME-STOP @ ${exit_price:.4f} | {pnl_pct:+.2f}% | {pos_age_mins:.0f}min open"

        if pnl_pct >= SCALP_TARGET_PCT:
            # SELL — take profit
            qty_str = f"{pos['qty']:.2f}"
            status, result = market_sell(SCALP_ASSET, qty_str)
            if result.get("success"):
                time.sleep(1)
                order = get_order(result["success_response"]["order_id"])
                exit_price = float(order.get("average_filled_price", xrp_price))
                realized_pnl = (exit_price - entry) * pos["qty"]
                scalp["position"] = None
                scalp["wins"] += 1
                scalp["trades_today"] += 1
                state["trades"].append({
                    "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    "asset": "XRP", "side": "SELL",
                    "qty": pos["qty"], "entry": entry, "exit": exit_price,
                    "sizeUsd": pos["size_usd"], "pnl": realized_pnl,
                    "reason": f"SCALP TAKE-PROFIT +{pnl_pct:.2f}% (target: +{SCALP_TARGET_PCT}%)",
                    "strategy_context": f"XRP scalp — quick +{pnl_pct:.1f}% | Entry ${entry:.4f} → Exit ${exit_price:.4f}",
                    "signals": [f"scalp", f"XRP +{pnl_pct:.1f}%"]
                })
                return "sell", f"🎯 XRP SCALP TP @ ${exit_price:.4f} | +${realized_pnl:.3f} (+{pnl_pct:.1f}%)"

        elif pnl_pct <= -SCALP_STOP_PCT:
            # SELL — stop loss
            qty_str = f"{pos['qty']:.2f}"
            status, result = market_sell(SCALP_ASSET, qty_str)
            if result.get("success"):
                time.sleep(1)
                order = get_order(result["success_response"]["order_id"])
                exit_price = float(order.get("average_filled_price", xrp_price))
                realized_pnl = (exit_price - entry) * pos["qty"]
                scalp["position"] = None
                scalp["losses"] += 1
                scalp["trades_today"] += 1
                state["trades"].append({
                    "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    "asset": "XRP", "side": "SELL",
                    "qty": pos["qty"], "entry": entry, "exit": exit_price,
                    "sizeUsd": pos["size_usd"], "pnl": realized_pnl,
                    "reason": f"SCALP STOP-LOSS {pnl_pct:.2f}% (stop: -{SCALP_STOP_PCT}%)",
                    "strategy_context": f"XRP scalp stopped out | Entry ${entry:.4f} → Exit ${exit_price:.4f}",
                    "signals": [f"scalp", f"XRP {pnl_pct:.1f}%"]
                })
                return "sell", f"🛑 XRP SCALP SL @ ${exit_price:.4f} | ${realized_pnl:.3f} ({pnl_pct:.1f}%)"

        return None, f"XRP scalp holding: {pnl_pct:+.2f}% | ${xrp_price:.4f}"

    # ── Check entry ──
    cooldown_elapsed = (time.time() - scalp.get("last_entry_time", 0)) / 60
    if cooldown_elapsed < SCALP_COOLDOWN_MINS:
        return None, f"XRP scalp cooldown ({SCALP_COOLDOWN_MINS - cooldown_elapsed:.0f}min)"

    # Need at least $6 cash (scalp $5 + $1 buffer)
    if cash < 6:
        return None, f"XRP scalp skipped — not enough cash (${cash:.2f})"

    # Max 12 scalp trades/day
    if scalp.get("trades_today", 0) >= 12:
        return None, "XRP scalp daily limit reached (12 trades)"

    # Only enter on upward momentum
    if price_move_pct >= SCALP_ENTRY_MOVE_PCT:
        trade_usd = min(SCALP_ALLOC_USD, cash - 1.0)
        status, result = market_buy(SCALP_ASSET, trade_usd)
        if result.get("success"):
            time.sleep(1)
            order = get_order(result["success_response"]["order_id"])
            entry_price = float(order.get("average_filled_price", xrp_price))
            qty = float(order.get("filled_size", 0))
            scalp["position"] = {
                "entry": entry_price, "qty": qty,
                "size_usd": trade_usd, "entry_time": time.time()
            }
            scalp["last_entry_time"] = time.time()
            state["trades"].append({
                "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "asset": "XRP", "side": "BUY",
                "qty": qty, "entry": entry_price, "exit": None,
                "sizeUsd": trade_usd, "pnl": None,
                "reason": f"SCALP ENTRY — XRP moved {price_move_pct:+.2f}% in last check (threshold: +{SCALP_ENTRY_MOVE_PCT}%)",
                "strategy_context": f"Micro-scalp: target +{SCALP_TARGET_PCT}% (${entry_price*1.015:.4f}) | stop -{SCALP_STOP_PCT}% (${entry_price*0.994:.4f})",
                "signals": [f"scalp", f"move: {price_move_pct:+.2f}%", f"XRP ${entry_price:.4f}"]
            })
            return "buy", f"⚡ XRP SCALP ENTRY @ ${entry_price:.4f} | ${trade_usd:.2f} | TP: ${entry_price*1.015:.4f} | SL: ${entry_price*0.994:.4f}"

    return None, f"XRP no signal (move: {price_move_pct:+.2f}%, need: +{SCALP_ENTRY_MOVE_PCT}%)"

def get_trending_news():
    signals = []
    try:
        # Trending coins on CoinGecko
        r = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=5)
        coins = r.json().get("coins", [])[:3]
        for c in coins:
            signals.append({"source": "CoinGecko Trending", "text": f"{c['item']['name']} trending (rank #{c['item']['market_cap_rank']})", "sentiment": "bullish"})
    except:
        pass

    try:
        # Fear & Greed index
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        d = r.json()["data"][0]
        val = int(d["value"])
        label = d["value_classification"]
        sentiment = "bullish" if val > 50 else "bearish" if val < 30 else "neutral"
        signals.append({"source": "Fear & Greed", "text": f"Index: {val}/100 — {label}", "sentiment": sentiment})
    except:
        pass

    try:
        # CoinGecko global market data
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=5)
        g = r.json().get("data", {})
        btc_dom = g.get("market_cap_percentage", {}).get("btc", 0)
        total_cap = g.get("total_market_cap", {}).get("usd", 0)
        change = g.get("market_cap_change_percentage_24h_usd", 0)
        sentiment = "bullish" if change > 1 else "bearish" if change < -1 else "neutral"
        signals.append({"source": "Global Market", "text": f"Total cap: ${total_cap/1e9:.0f}B | 24h: {change:+.1f}% | BTC dom: {btc_dom:.1f}%", "sentiment": sentiment})
    except:
        pass

    return signals

# ── Trading logic ─────────────────────────────────────────────────────────────
def should_sell_eth(state, current_price):
    """Check if we should exit ETH position — uses live strategy params"""
    pos = state["positions"].get("ETH")
    if not pos:
        return False, None

    strategy = load_strategy()
    entry = pos["entry"]
    pnl_pct = (current_price - entry) / entry * 100

    # Recalculate stop/target from live strategy (may have changed since entry)
    dynamic_stop = entry * (1 - strategy["stop_loss_pct"] / 100)
    dynamic_target = entry * (1 + strategy["take_profit_pct"] / 100)

    # Update position's stop/target to reflect current strategy
    pos["stop_loss"] = dynamic_stop
    pos["target"] = dynamic_target

    if current_price <= dynamic_stop:
        return True, f"STOP-LOSS HIT — price ${current_price:.2f} <= stop ${dynamic_stop:.2f} ({pnl_pct:.1f}%) [strategy: -{strategy['stop_loss_pct']}%]"
    if current_price >= dynamic_target:
        return True, f"TAKE-PROFIT HIT — price ${current_price:.2f} >= target ${dynamic_target:.2f} (+{pnl_pct:.1f}%) [strategy: +{strategy['take_profit_pct']}%]"
    # Trailing: if up > half of take-profit and starts reversing >2% from peak, sell
    half_tp = strategy["take_profit_pct"] / 2
    if pnl_pct >= half_tp and pos.get("peak_price", 0) > 0:
        peak = pos.get("peak_price", current_price)
        drawdown_from_peak = (current_price - peak) / peak * 100
        if drawdown_from_peak < -2:
            return True, f"TRAILING EXIT — {pnl_pct:.1f}% up but reversing {drawdown_from_peak:.1f}% from peak"

    return False, None

def fetch_candles(sym, granularity="ONE_HOUR", limit=24):
    """
    Fetch OHLCV candles from Coinbase for a given symbol.
    Returns list of dicts with open/high/low/close/volume, newest first.
    """
    product_id = f"{sym}-USD"
    try:
        data = cb_get(f"/api/v3/brokerage/products/{product_id}/candles",
                      params={"granularity": granularity, "limit": str(limit)})
        candles = data.get("candles", [])
        return [{"open": float(c["open"]), "high": float(c["high"]),
                 "low": float(c["low"]), "close": float(c["close"]),
                 "volume": float(c["volume"]), "start": int(c["start"])}
                for c in candles]
    except Exception:
        return []

def calc_rsi(closes, period=14):
    """Calculate RSI from a list of closes (oldest first)."""
    if len(closes) < period + 1:
        return 50  # neutral if not enough data
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i-1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)

def get_ta_signals(sym):
    """
    Fetch candles and compute RSI, volume trend, support/resistance proximity.
    Returns dict with ta_score bonus (+/-), rsi, vol_trend, notes.
    """
    candles = fetch_candles(sym, granularity="ONE_HOUR", limit=48)
    if len(candles) < 15:
        return {"ta_score": 0, "rsi": None, "notes": "no candle data"}

    # Candles are newest-first from Coinbase — reverse for calculations
    candles_asc = list(reversed(candles))
    closes = [c["close"] for c in candles_asc]
    volumes = [c["volume"] for c in candles_asc]
    highs = [c["high"] for c in candles_asc]
    lows = [c["low"] for c in candles_asc]

    rsi = calc_rsi(closes)
    current_price = closes[-1]

    # Volume trend: is current volume above 20h average?
    avg_vol = sum(volumes[-20:]) / min(20, len(volumes))
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
    vol_trending = vol_ratio > 1.2  # 20% above average = confirmation

    # Resistance proximity: is price near a recent 24h high? (potential ceiling)
    recent_high = max(highs[-24:]) if len(highs) >= 24 else max(highs)
    recent_low = min(lows[-24:]) if len(lows) >= 24 else min(lows)
    pct_from_high = (recent_high - current_price) / recent_high * 100
    pct_from_low = (current_price - recent_low) / recent_low * 100

    # Score adjustments
    ta_score = 0.0
    notes = []

    # RSI signals
    if rsi < 35:
        ta_score += 0.3  # oversold — bounce potential
        notes.append(f"RSI {rsi} oversold ↑")
    elif rsi > 70:
        ta_score -= 0.3  # overbought — caution
        notes.append(f"RSI {rsi} overbought ↓")
    elif 40 <= rsi <= 60:
        ta_score += 0.1  # healthy mid-range momentum
        notes.append(f"RSI {rsi} healthy")
    else:
        notes.append(f"RSI {rsi}")

    # Volume confirmation
    if vol_trending:
        ta_score += 0.2
        notes.append(f"vol {vol_ratio:.1f}x avg ↑")
    else:
        notes.append(f"vol {vol_ratio:.1f}x avg")

    # Near resistance ceiling — penalise (little upside room)
    if pct_from_high < 0.5:
        ta_score -= 0.2
        notes.append(f"near 24h high (-{pct_from_high:.1f}%) ↓")
    elif pct_from_high > 3:
        ta_score += 0.1
        notes.append(f"room to {pct_from_high:.1f}% below high ↑")

    # Momentum: last 3 candles trending up?
    if len(closes) >= 4 and closes[-1] > closes[-2] > closes[-3]:
        ta_score += 0.15
        notes.append("3-candle uptrend ↑")
    elif len(closes) >= 3 and closes[-1] < closes[-2] < closes[-3]:
        ta_score -= 0.15
        notes.append("3-candle downtrend ↓")

    return {
        "ta_score": round(ta_score, 3),
        "rsi": rsi,
        "vol_ratio": round(vol_ratio, 2),
        "pct_from_high": round(pct_from_high, 2),
        "notes": " | ".join(notes)
    }

def score_signal(change_1h, change_24h, volume=0):
    """
    Weighted signal score combining 1h momentum, 24h trend alignment, and volume.
    Returns a float score — higher is stronger.
    - 1h momentum is primary (weight 0.6)
    - 24h trend adds confirmation if aligned (weight 0.3)
    - Volume adds 0-0.1 bonus for high-volume moves
    """
    score = change_1h * 0.6
    # 24h alignment bonus: if 24h is also positive, it confirms the trend
    if change_24h > 0:
        score += change_24h * 0.3
    elif change_24h < -5:
        score -= abs(change_24h) * 0.1  # mild penalty for recovering from dump
    # Volume bonus (normalised — volume in USD billions)
    if volume > 0:
        vol_b = volume / 1e9
        score += min(vol_b * 0.01, 0.1)  # cap at 0.1 bonus
    return score

def scan_sniper_targets(prices):
    """
    SNIPER MODE v2: Scan all tracked assets and return ranked list of scored signals.
    Returns list of (product_id, symbol, score, change_1h, change_24h, price) sorted by score desc.
    """
    candidates = []
    asset_map = {
        "ETH": "ETH-USD",
        "SOL": "SOL-USD",
        "XRP": "XRP-USD",
        "BTC": "BTC-USD",
    }
    for sym, product_id in asset_map.items():
        data = prices.get(sym, {})
        price = data.get("price", 0)
        change_1h = data.get("change_1h") or 0
        change_24h = data.get("change_24h") or 0
        volume = data.get("volume") or 0
        if price > 0:
            sc = score_signal(change_1h, change_24h, volume)
            # Augment score with technical analysis from Coinbase candles
            ta = get_ta_signals(sym)
            sc_final = sc + ta["ta_score"]
            candidates.append((product_id, sym, sc_final, change_1h, change_24h, price, ta))
    # Sort by composite score descending
    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates

def should_rotate_position(state, prices, strategy):
    """
    ROTATION CHECK: If we're in a position that's underperforming, and another asset
    has a significantly stronger signal score, return the better asset to rotate into.
    Rotation criteria:
    - Current position is negative OR near-flat (< +0.5%)
    - Best alternative has score > current held asset's score + 0.3
    - We haven't rotated in the last 30 minutes
    Returns (True, product_id, sym, reason) or (False, None, None, reason)
    """
    if not state["positions"]:
        return False, None, None, "No positions to rotate", None

    candidates = scan_sniper_targets(prices)
    if not candidates:
        return False, None, None, "No price data", None

    # Find best alternative (not currently held)
    held = set(state["positions"].keys())
    held_scores = {}
    best_alt = None

    for item in candidates:
        product_id, sym, sc, c1h, c24h, price = item[0], item[1], item[2], item[3], item[4], item[5]
        if sym in held:
            held_scores[sym] = (sc, c1h, c24h)
        elif best_alt is None and sc > 0:
            best_alt = (product_id, sym, sc, c1h, c24h, price)

    if not best_alt or not held_scores:
        return False, None, None, "No rotation candidate found", None

    # Check if current position is underperforming
    for sym, pos in state["positions"].items():
        current_price = prices.get(sym, {}).get("price") or pos.get("current_price") or pos["entry"]
        pnl_pct = (current_price - pos["entry"]) / pos["entry"] * 100
        held_score = held_scores.get(sym, (0,))[0]
        alt_product_id, alt_sym, alt_score, alt_1h, alt_24h, alt_price = best_alt

        # Rotation condition — strict to avoid fee-burning churn:
        # - Position clearly losing (>3% down) — never rotate a winner or breakeven
        # - Strong score edge (>1.2) — high conviction only, not noise
        # - Held at least 6 hours — give position real time to work
        # - 8-hour cooldown — each rotation burns ~1.2% round-trip in fees
        rotation_edge = alt_score - held_score
        held_hours = (time.time() - pos.get("entry_time", time.time())) / 3600

        # NEVER rotate a profitable or breakeven position — only cut losers
        if pnl_pct >= 0:
            continue

        # Each rotation burns ~0.8-1.2% in round-trip fees — need strong conviction:
        # - Position must be down >5% (significant loss, not noise)
        # - Alternative must have score edge > 2.0 (high conviction, not statistical noise)
        # - Held at least 10 hours (give position real time to work)
        if pnl_pct < -5.0 and rotation_edge > 2.0 and held_hours >= 10.0:
            # Check rotation cooldown
            last_rotate = state.get("last_rotation_time", 0)
            if time.time() - last_rotate < 28800:  # 8 hour cooldown
                return False, None, None, f"Rotation cooldown ({int((28800 - (time.time()-last_rotate))/60)}min left)", None

            reason = (f"ROTATE: {sym} ({pnl_pct:+.2f}%, score={held_score:.3f}) → "
                      f"{alt_sym} (score={alt_score:.3f}, edge={rotation_edge:+.3f}) | "
                      f"{alt_sym} 1h={alt_1h:+.2f}% 24h={alt_24h:+.2f}%")
            # Return which specific position to rotate out of
            return True, alt_product_id, alt_sym, reason, sym

    return False, None, None, "No rotation needed — positions performing adequately", None

def decide_next_trade(state, cash, prices, total_balance):
    """
    SNIPER MODE v2: Score-based signal selection. Deploy 90% on best scored signal.
    Also deploys idle cash into second position if main position exists and cash > $5.
    """
    strategy = load_strategy()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if total_balance < 5:
        return None, "Insufficient funds"

    if cash < 3:
        return None, "Not enough cash buffer (<$3)"

    threshold = strategy["momentum_threshold_1h"]
    candidates = scan_sniper_targets(prices)
    held = set(state["positions"].keys())

    best_signal = None
    best_reason = None
    all_considered = []

    for item in candidates:
        product_id, sym, sc, change_1h, change_24h, price = item[0], item[1], item[2], item[3], item[4], item[5]
        ta = item[6] if len(item) > 6 else {}
        all_considered.append(f"{sym}: {change_1h:+.2f}%1h score={sc:.2f} RSI={ta.get('rsi','?')}")

        # Skip assets we're already holding
        if sym in held:
            continue

        # Skip if 24h is extremely bearish (>10% down)
        if change_24h < -10:
            state.setdefault("missed_opportunities", []).append({
                "time": ts, "asset": sym,
                "reason": f"24h too bearish ({change_24h:.1f}%)",
                "signal": f"{change_1h:+.2f}% 1h | {change_24h:+.2f}% 24h | score={sc:.2f}",
                "price": price,
                "est_gain_pct": strategy["take_profit_pct"],
                "est_gain_usd": round(cash * 0.9 * strategy["take_profit_pct"] / 100, 2)
            })
            continue

        if change_1h >= threshold:
            if best_signal is None:
                best_signal = (product_id, sym, sc, change_1h, change_24h, price, ta)
                best_reason = (f"🎯 SNIPER v2: {sym} score={sc:.3f} | 1h={change_1h:+.2f}% 24h={change_24h:+.2f}% "
                               f"| RSI={ta.get('rsi','?')} vol={ta.get('vol_ratio','?')}x | {ta.get('notes','')}")
        else:
            if change_1h > 0:
                state.setdefault("missed_opportunities", []).append({
                    "time": ts, "asset": sym,
                    "reason": f"Below threshold ({change_1h:+.2f}% < {threshold}%)",
                    "signal": f"{change_1h:+.2f}% 1h | score={sc:.2f} | RSI={ta.get('rsi','?')}",
                    "price": price,
                    "est_gain_pct": strategy["take_profit_pct"],
                    "est_gain_usd": round(cash * 0.9 * strategy["take_profit_pct"] / 100, 2)
                })

    if best_signal:
        product_id, sym, sc, change_1h, change_24h, price = best_signal[0], best_signal[1], best_signal[2], best_signal[3], best_signal[4], best_signal[5]
        ta = best_signal[6] if len(best_signal) > 6 else {}
        # Position sizing: each slot gets ~30% of total portfolio, leaving ~10% cash reserve
        # Slot 1 (no existing positions): up to 60% of total
        # Slot 2+: up to 30% of total, but also capped by available cash minus $2 reserve
        CASH_RESERVE = max(2.0, total_balance * 0.10)  # keep 10% as reserve, min $2
        if not held:
            # First position: up to 60% of portfolio
            max_alloc = round(total_balance * 0.60, 2)
            trade_usd = round(min(cash - CASH_RESERVE, max_alloc), 2)
        else:
            # 2nd/3rd position: up to 30% of portfolio each
            max_alloc = round(total_balance * 0.30, 2)
            trade_usd = round(min(cash - CASH_RESERVE, max_alloc), 2)
        trade_usd = max(2.0, trade_usd)
        return (product_id, trade_usd), best_reason

    best_str = f"{candidates[0][1]} score={candidates[0][2]:.2f} {candidates[0][3]:+.2f}%1h" if candidates else "no data"
    return None, f"No signal — best: {best_str} (need 1h>{threshold}%) | All: {', '.join(all_considered) or 'none'}"

# ── Main hourly run ───────────────────────────────────────────────────────────
def run_hourly():
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%d %H:%M UTC")
    state = load_state()

    # Day counter
    from datetime import date
    start = date.fromisoformat(START_DATE)
    day_num = (date.today() - start).days + 1
    # Reset daily scalp counter if new day
    if state.get("last_day") != day_num:
        state.setdefault("scalp", {})["trades_today"] = 0
        state["last_day"] = day_num
    state["day"] = day_num

    # Get current portfolio
    total, cash, live_positions = get_portfolio()

    # Get prices
    prices = get_crypto_prices()
    eth_price = prices.get("ETH", {}).get("price") or get_price("ETH-USD")
    btc_price = prices.get("BTC", {}).get("price", 0)
    eth = prices.get("ETH", {})

    # Get Fear & Greed for context
    fear_greed_val = "N/A"
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        fear_greed_val = r.json()["data"][0]["value"] + "/100"
    except:
        pass

    # Sync all positions from live Coinbase portfolio (source of truth)
    for sym in list(state["positions"].keys()):
        live_qty = live_positions.get(sym, {}).get("qty", 0)
        live_price = prices.get(sym, {}).get("price") or 0
        if live_qty > 0:
            state["positions"][sym]["qty"] = live_qty
            if live_price:
                state["positions"][sym]["size_usd"] = round(live_qty * live_price, 2)
                state["positions"][sym]["peak_price"] = max(
                    state["positions"][sym].get("peak_price", live_price), live_price
                )

    report_lines = [
        f"📊 **HOURLY REPORT — Day {day_num}/30 — {ts}**",
        f"",
        f"**Portfolio:** ${total:.2f} | Start: ${START_BALANCE:.2f} | Change: {'+' if total >= START_BALANCE else ''}{((total - START_BALANCE)/START_BALANCE*100):.1f}%",
        f"**Goal progress:** ${total:.2f} / ${TARGET:.2f} ({total/TARGET*100:.1f}%)",
        f"**Cash:** ${cash:.2f} | **Crypto:** ${total - cash:.2f}",
        f"",
        f"**Prices:** ETH ${eth_price:,.2f} | BTC ${btc_price:,.0f}",
    ]

    actions_taken = []

    # ── Safety: Enforce TP:SL ratio ≥ 2.0:1 ──
    # Only writes to strategy.json ONCE per run when correction needed.
    # No adjustment_log spam — external research script will see the new values.
    import json as _json
    strategy_changed = False
    strategy = load_strategy()
    tp = strategy.get("take_profit_pct", 5.0)
    MIN_RATIO = 2.0
    sl = strategy.get("stop_loss_pct", 7.0)
    ratio = tp / sl if sl > 0 else 0
    if ratio < MIN_RATIO:
        corrected_sl = round(tp / MIN_RATIO, 2)
        strategy["stop_loss_pct"] = corrected_sl
        strategy_changed = True

    # ── Fee drag sanity check ──
    if strategy.get("take_profit_pct", 0) < 3.0:
        strategy["take_profit_pct"] = 5.0
        strategy_changed = True

    if strategy_changed:
        with open(STRATEGY_FILE, "w") as f:
            _json.dump(strategy, f, indent=2)

    # ── Check exit conditions (all open positions) ──
    positions_to_close = []
    for sym, pos in list(state["positions"].items()):
        product_id = f"{sym}-USD"
        current_price = prices.get(sym, {}).get("price") or get_price(product_id)
        if not current_price:
            continue

        # Update peak for trailing stop
        pos["peak_price"] = max(pos.get("peak_price", current_price), current_price)
        pos["current_price"] = current_price

        entry = pos["entry"]
        pnl_pct = (current_price - entry) / entry * 100
        unrealized_pnl = (current_price - entry) * pos["qty"]
        strategy = load_strategy()
        # Enforce TP:SL ≥ 2.0:1
        tp_pct = strategy["take_profit_pct"]
        sl_pct = min(strategy["stop_loss_pct"], tp_pct / 2.0)
        stop = entry * (1 - sl_pct / 100)
        target = entry * (1 + tp_pct / 100)
        pos["stop_loss"] = stop
        pos["target"] = target

        report_lines.append(f"**Open Position:** {sym} {pos['qty']:.6f} @ ${entry:.4f} entry")
        report_lines.append(f"  Current: ${current_price:.4f} | P&L: {'+' if unrealized_pnl >= 0 else ''}${unrealized_pnl:.3f} ({pnl_pct:+.2f}%)")
        report_lines.append(f"  Stop: ${stop:.4f} (-{sl_pct:.1f}%) | Target: ${target:.4f} (+{tp_pct:.1f}%) | Ratio: {tp_pct/sl_pct:.1f}:1")

        should_exit = False
        exit_reason = ""
        if current_price <= stop:
            should_exit = True
            exit_reason = f"STOP-LOSS HIT — ${current_price:.4f} <= ${stop:.4f} ({pnl_pct:.1f}%)"
        elif current_price >= target:
            should_exit = True
            exit_reason = f"TAKE-PROFIT HIT — ${current_price:.4f} >= ${target:.4f} (+{pnl_pct:.1f}%)"
        elif pnl_pct >= tp_pct / 2 and pos.get("peak_price", 0) > 0:
            drawdown = (current_price - pos["peak_price"]) / pos["peak_price"] * 100
            # Progressive trailing: wider at moderate gains, tighter near target
            # At 50% of TP: trail -2.5% | At 75% of TP: trail -1.5% | Above TP: trail -1.0%
            if pnl_pct >= tp_pct:
                trail_threshold = -1.0
            elif pnl_pct >= tp_pct * 0.75:
                trail_threshold = -1.5
            else:
                trail_threshold = -2.5
            if drawdown < trail_threshold:
                should_exit = True
                exit_reason = f"TRAILING EXIT — {pnl_pct:.1f}% up, reversing {drawdown:.1f}% from peak (trail threshold: {trail_threshold}%)"

        if should_exit:
            positions_to_close.append((sym, product_id, pos, current_price, exit_reason))

    for sym, product_id, pos, current_price, exit_reason in positions_to_close:
        report_lines.append(f"  🚨 **EXIT:** {exit_reason}")
        live_qty = live_positions.get(sym, {}).get("qty", pos["qty"])
        status, result = market_sell(product_id, f"{live_qty:.8f}")
        if result.get("success"):
            order_id = result["success_response"]["order_id"]
            time.sleep(1)
            order = get_order(order_id)
            exit_price = float(order.get("average_filled_price", current_price))
            realized_pnl = (exit_price - pos["entry"]) * pos["qty"]
            held_h = round((time.time() - pos.get("entry_time", time.time())) / 3600, 1)
            state["trades"].append({
                "time": ts, "asset": sym, "side": "SELL",
                "qty": pos["qty"], "entry": pos["entry"], "exit": exit_price,
                "sizeUsd": pos.get("size_usd", 0),
                "pnl": realized_pnl, "reason": exit_reason,
                "strategy_context": f"SNIPER exit: {sym} @ ${exit_price:.4f} | Entry ${pos['entry']:.4f} | Held {held_h}h | P&L ${realized_pnl:+.3f}",
                "signals": [f"F&G: {fear_greed_val}", f"BTC ${btc_price:,.0f}"]
            })
            del state["positions"][sym]
            actions_taken.append(f"✅ Sold {sym} @ ${exit_price:.4f} | P&L: ${realized_pnl:+.3f} | Back to cash — scanning next sniper target")
            report_lines.append(f"  ✅ **SOLD** @ ${exit_price:.4f} | Realized P&L: ${realized_pnl:+.3f}")

    # ── Rotation check — swap underperforming position for stronger signal ──
    if state["positions"] and not positions_to_close:
        strategy = load_strategy()
        should_rotate, rot_product_id, rot_sym, rot_reason, rot_from_sym = should_rotate_position(state, prices, strategy)
        if should_rotate:
            # Only sell the specific underperforming position, not all positions
            for sym, pos in list(state["positions"].items()):
                if sym != rot_from_sym:
                    continue  # leave winning positions alone
                product_id = f"{sym}-USD"
                current_price = prices.get(sym, {}).get("price") or pos.get("current_price") or pos["entry"]
                live_qty = live_positions.get(sym, {}).get("qty", pos["qty"])
                report_lines.append(f"")
                report_lines.append(f"🔄 **ROTATION:** {rot_reason}")
                status, result = market_sell(product_id, f"{live_qty:.8f}")
                if result.get("success"):
                    time.sleep(1)
                    order_id = result["success_response"]["order_id"]
                    order = get_order(order_id)
                    exit_price = float(order.get("average_filled_price", current_price))
                    realized_pnl = (exit_price - pos["entry"]) * pos["qty"]
                    held_h = round((time.time() - pos.get("entry_time", time.time())) / 3600, 1)
                    state["trades"].append({
                        "time": ts, "asset": sym, "side": "SELL",
                        "qty": pos["qty"], "entry": pos["entry"], "exit": exit_price,
                        "sizeUsd": pos.get("size_usd", 0), "pnl": realized_pnl,
                        "reason": f"ROTATION SELL: {rot_reason}",
                        "strategy_context": f"Rotated out of {sym} @ ${exit_price:.4f} into {rot_sym} | Held {held_h}h | P&L ${realized_pnl:+.3f}",
                        "signals": [f"F&G: {fear_greed_val}"]
                    })
                    del state["positions"][sym]
                    actions_taken.append(f"🔄 Rotated {sym} → {rot_sym}")
                    report_lines.append(f"  Sold {sym} @ ${exit_price:.4f} | P&L: ${realized_pnl:+.3f}")
            state["last_rotation_time"] = time.time()
            time.sleep(1)
            total, cash, live_positions = get_portfolio()

            # Now buy the rotation target
            trade_usd = round(cash * 0.90, 2)
            rot_price = prices.get(rot_sym, {}).get("price") or get_price(rot_product_id)
            status, result = market_buy(rot_product_id, trade_usd)
            if result.get("success"):
                time.sleep(1)
                order_id = result["success_response"]["order_id"]
                order = get_order(order_id)
                entry_price = float(order.get("average_filled_price", rot_price))
                qty = float(order.get("filled_size", 0))
                strategy = load_strategy()
                tp_pct = strategy["take_profit_pct"]
                sl_pct = min(strategy["stop_loss_pct"], tp_pct / 1.5)
                stop = entry_price * (1 - sl_pct / 100)
                target = entry_price * (1 + tp_pct / 100)
                state["positions"][rot_sym] = {
                    "qty": qty, "entry": entry_price, "stop_loss": stop, "target": target,
                    "size_usd": trade_usd, "peak_price": entry_price,
                    "order_id": order_id, "entry_time": time.time()
                }
                state["trades"].append({
                    "time": ts, "asset": rot_sym, "side": "BUY",
                    "qty": qty, "entry": entry_price, "exit": None,
                    "sizeUsd": trade_usd, "pnl": None,
                    "reason": f"ROTATION BUY: {rot_reason}",
                    "strategy_context": f"Rotated into {rot_sym} @ ${entry_price:.4f} | Stop ${stop:.4f} (-{sl_pct:.1f}%) | Target ${target:.4f} (+{tp_pct:.1f}%)",
                    "signals": []
                })
                actions_taken.append(f"🔄 Bought {rot_sym} @ ${entry_price:.4f}")
                report_lines.append(f"  Bought {rot_sym} @ ${entry_price:.4f} | Stop: ${stop:.4f} | Target: ${target:.4f}")

    # ── Track missed opportunities ──
    # Did we have a signal but couldn't act due to being invested?
    if state["positions"]:
        held = list(state["positions"].keys())
        for sym, data in prices.items():
            if sym in held:
                continue
            move_1h = data.get("change_1h", 0) or 0
            move_24h = data.get("change_24h", 0) or 0
            strategy = load_strategy()
            if move_1h > strategy["momentum_threshold_1h"]:
                state.setdefault("missed_opportunities", []).append({
                    "time": ts,
                    "asset": sym,
                    "reason": f"Fully invested in {', '.join(held)}",
                    "signal": f"{move_1h:+.2f}% 1h | {move_24h:+.2f}% 24h",
                    "price": data.get("price", 0),
                    "est_gain_pct": strategy["take_profit_pct"],
                    # Estimate based on a 30% slot allocation (realistic per-position sizing)
                    "est_gain_usd": round(total * 0.30 * strategy["take_profit_pct"] / 100, 2)
                })

    # ── XRP Scalp Layer — crash recovery kill stale positions ──
    xrp_price = prices.get("XRP", {}).get("price", 0)

    # Crash recovery: if scalp position was open at last save but daemon
    # went down, kill it unconditionally (we can't trust entry price/fills
    # after a gap — and holding a scalp for hours violates the strategy).
    scalp_data = state.get("scalp", {})
    scalp_pos = scalp_data.get("position")
    if scalp_pos:
        pos_age_mins = (time.time() - scalp_pos.get("entry_time", time.time())) / 60
        if pos_age_mins > SCALP_MAX_AGE_MINS + 30:
            # Stale scalp from crash — liquidate immediately
            qty_str = f"{scalp_pos['qty']:.2f}"
            status, result = market_sell(SCALP_ASSET, qty_str)
            if result.get("success"):
                time.sleep(1)
                order = get_order(result["success_response"]["order_id"])
                exit_price = float(order.get("average_filled_price", xrp_price))
                entry = scalp_pos["entry"]
                realized_pnl = (exit_price - entry) * scalp_pos["qty"]
                pnl_pct = (exit_price - entry) / entry * 100
                scalp_data["position"] = None
                if realized_pnl >= 0:
                    scalp_data["wins"] = scalp_data.get("wins", 0) + 1
                else:
                    scalp_data["losses"] = scalp_data.get("losses", 0) + 1
                scalp_data.setdefault("trades_today", 0)
                scalp_data["trades_today"] += 1
                state["scalp"] = scalp_data
                state["trades"].append({
                    "time": ts, "asset": "XRP", "side": "SELL",
                    "qty": scalp_pos["qty"], "entry": entry, "exit": exit_price,
                    "sizeUsd": scalp_pos["size_usd"], "pnl": realized_pnl,
                    "reason": f"CRASH RECOVERY KILL — scalp open {pos_age_mins:.0f}min (max: {SCALP_MAX_AGE_MINS}min) | P&L: {pnl_pct:+.2f}%",
                    "strategy_context": f"Stale scalp killed after daemon gap | Entry ${entry:.4f} → Exit ${exit_price:.4f} | Age {pos_age_mins:.0f}min",
                    "signals": ["crash-recovery", "stale-kill"]
                })
                # Save state immediately so we don't double-kill on next run
                save_state(state)
                # Refresh cash after liquidation
                total, cash, live_positions = get_portfolio()
            else:
                # If sell failed, drop the position anyway to prevent infinite re-kill
                scalp_data["position"] = None
                state["scalp"] = scalp_data

    scalp_action, scalp_msg = check_scalp(state, prices, cash, total)
    if scalp_action:
        actions_taken.append(scalp_msg)
        report_lines.append(f"")
        report_lines.append(f"{scalp_msg}")
        # Refresh cash after scalp trade
        time.sleep(1)
        total, cash, live_positions = get_portfolio()
    else:
        # Only show scalp status occasionally
        run_count = len(state.get("balance_history", []))
        if run_count % 5 == 0:
            report_lines.append(f"  📊 XRP scalp: {scalp_msg}")

    # ── Refresh cash after potential sell ──
    if actions_taken:
        time.sleep(1)
        total, cash, live_positions = get_portfolio()

    # ── Check entry conditions (new position OR deploy idle cash into 2nd/3rd position) ──
    # Drawdown circuit breaker: cap positions when portfolio is bleeding
    drawdown_pct = (total - START_BALANCE) / START_BALANCE * 100 if START_BALANCE else 0
    if drawdown_pct < -15:
        max_positions = 1
        report_lines.append(f"\n⚠️ **DRAWDOWN CIRCUIT BREAKER:** Portfolio {drawdown_pct:.0f}% below start, max positions = 1")
    elif drawdown_pct < -10:
        max_positions = 2
        report_lines.append(f"\n⚠️ **DRAWDOWN WARNING:** Portfolio {drawdown_pct:.0f}% below start, max positions = 2")
    else:
        max_positions = 3  # normal: up to 3 simultaneous positions
    if len(state["positions"]) < max_positions:
        trade_signal, signal_reason = decide_next_trade(state, cash, prices, total)
        if trade_signal:
            product_id, trade_usd = trade_signal
            sym = product_id.replace("-USD", "")
            current_asset_price = prices.get(sym, {}).get("price") or get_price(product_id)
            report_lines.append(f"")
            report_lines.append(f"🎯 **SNIPER ENTRY:** {signal_reason}")
            status, result = market_buy(product_id, trade_usd)
            if result.get("success"):
                order_id = result["success_response"]["order_id"]
                time.sleep(1)
                order = get_order(order_id)
                entry_price = float(order.get("average_filled_price", current_asset_price))
                qty = float(order.get("filled_size", 0))
                strategy = load_strategy()
                stop = entry_price * (1 - strategy["stop_loss_pct"] / 100)
                target = entry_price * (1 + strategy["take_profit_pct"] / 100)
                asset_1h = prices.get(sym, {}).get("change_1h", 0) or 0
                asset_24h = prices.get(sym, {}).get("change_24h", 0) or 0
                state["positions"][sym] = {
                    "qty": qty, "entry": entry_price,
                    "stop_loss": stop, "target": target,
                    "size_usd": trade_usd, "peak_price": entry_price,
                    "order_id": order_id, "entry_time": time.time()
                }
                state["trades"].append({
                    "time": ts, "asset": sym, "side": "BUY",
                    "qty": qty, "entry": entry_price, "exit": None,
                    "sizeUsd": trade_usd, "pnl": None,
                    "reason": signal_reason,
                    "strategy_context": f"SNIPER: {sym} @ ${entry_price:.4f} | 1h: {asset_1h:+.2f}% | "
                        f"Stop: ${stop:.4f} (-{strategy['stop_loss_pct']}%) | Target: ${target:.4f} (+{strategy['take_profit_pct']}%) | "
                        f"Deployed: ${trade_usd:.2f} ({trade_usd/total*100:.0f}% of portfolio)",
                    "signals": [f"1h: {asset_1h:+.2f}%", f"24h: {asset_24h:+.2f}%",
                                f"BTC ${btc_price:,.0f}"]
                })
                actions_taken.append(f"🎯 Sniper buy: {qty:.6f} {sym} @ ${entry_price:.4f}")
                report_lines.append(f"✅ **BOUGHT** {qty:.6f} {sym} @ ${entry_price:.4f} | Stop: ${stop:.4f} | Target: ${target:.4f}")
            else:
                report_lines.append(f"⚠️ Order failed: {result}")
        else:
            report_lines.append(f"")
            report_lines.append(f"⏸️ **No sniper signal:** {signal_reason}")

    # ── Update live prices on positions for dashboard ──
    if "ETH" in state["positions"] and eth_price:
        state["positions"]["ETH"]["current_price"] = eth_price

    # ── Store cash balance ──
    state["cash"] = round(cash, 2)

    # ── Cap missed opportunities log (trim aggressively — just recent context) ──
    if "missed_opportunities" in state:
        state["missed_opportunities"] = state["missed_opportunities"][-50:]

    # ── Activity log entry (shown in dashboard cycle log) ──
    # Summarise all open positions for the log
    pos_summary_pnl = 0
    pos_names = []
    for sym, pos in state["positions"].items():
        cp = prices.get(sym, {}).get("price") or pos.get("current_price") or pos["entry"]
        pnl_pct = (cp - pos["entry"]) / pos["entry"] * 100 if pos["entry"] else 0
        pos_summary_pnl += pnl_pct
        pos_names.append(f"{sym} {pnl_pct:+.1f}%")
    avg_pnl_pct = pos_summary_pnl / len(state["positions"]) if state["positions"] else 0

    log_entry = {
        "time": ts,
        "cycle": len(state.get("balance_history", [])),
        "portfolio": round(total, 2),
        "eth_price": round(eth_price, 2) if eth_price else None,
        "eth_pnl_pct": round(avg_pnl_pct, 2),
        "cash": round(cash, 2),
        "actions": actions_taken if actions_taken else [f"holding {', '.join(pos_names) or 'cash'}"],
        "xrp": f"Positions: {', '.join(pos_names) or 'none'}",
        "regime": load_strategy().get("market_regime", "unknown"),
    }
    state.setdefault("activity_log", []).append(log_entry)
    # Keep last 120 entries (2 hours at 1/min)
    state["activity_log"] = state["activity_log"][-120:]

    # ── Update balance history ──
    state["balance_history"].append({"time": ts, "value": round(total, 4)})
    if len(state["balance_history"]) > 720:  # keep 30 days of hourly data
        state["balance_history"] = state["balance_history"][-720:]

    # News signals only on every 10th run (every ~10 min) to save time
    run_count = len(state.get("balance_history", []))
    if run_count % 10 == 0:
        signals = get_trending_news()
        if signals:
            report_lines.append(f"")
            report_lines.append(f"📰 **Market Signals:**")
            for s in signals:
                emoji = "🟢" if s["sentiment"] == "bullish" else "🔴" if s["sentiment"] == "bearish" else "🟡"
                report_lines.append(f"  {emoji} [{s['source']}] {s['text']}")

    report_lines.append(f"")
    report_lines.append(f"_Next check in ~1 hour_")

    save_state(state)
    return "\n".join(report_lines)

if __name__ == "__main__":
    print(run_hourly())
