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
    return {"take_profit_pct": 8.0, "stop_loss_pct": 10.0, "momentum_threshold_1h": 0.3,
            "max_position_pct": 80.0, "preferred_asset": "ETH-USD"}

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

def cb_get(path):
    token = make_jwt("GET", path)
    r = requests.get(f"https://api.coinbase.com{path}",
                     headers={"Authorization": f"Bearer {token}"}, timeout=10)
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
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin,ethereum,solana,ripple"
            "&vs_currencies=usd&include_24hr_change=true",
            timeout=5
        )
        raw = r.json()
        mapping = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "ripple": "XRP"}
        return {mapping[k]: {
            "price": v.get("usd", 0),
            "change_24h": v.get("usd_24h_change", 0),
            "change_1h": 0,
        } for k, v in raw.items() if k in mapping}
    except Exception:
        return {}

# ── XRP Scalp logic ───────────────────────────────────────────────────────────
# Each run we compare current price to last-seen price to detect micro-moves.
# We enter on upswings > SCALP_ENTRY_MOVE_PCT and exit at SCALP_TARGET_PCT or SCALP_STOP_PCT.

SCALP_ASSET = "XRP-USD"
SCALP_SYMBOL = "XRP"
SCALP_ALLOC_USD = 5.0       # fixed $5 per scalp trade (small, frequent)
SCALP_TARGET_PCT = 1.5      # take profit at +1.5%
SCALP_STOP_PCT = 0.6        # stop loss at -0.6% (tight, fees ~1.2% round trip)
SCALP_ENTRY_MOVE_PCT = 0.3  # enter if XRP moved up ≥0.3% since last check
SCALP_COOLDOWN_MINS = 5     # min minutes between scalp entries

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

def decide_next_trade(state, cash, prices, total_balance):
    """Decide whether to enter a new position — uses live strategy params"""
    strategy = load_strategy()

    if total_balance < 5:
        return None, "Insufficient funds"

    invested_pct = (total_balance - cash) / total_balance * 100 if total_balance > 0 else 0
    max_invested = strategy["max_position_pct"]
    if invested_pct > max_invested:
        return None, f"Already >{max_invested:.0f}% invested ({invested_pct:.0f}%)"

    if cash < 3:
        return None, "Not enough cash buffer (<$3)"

    preferred = strategy.get("preferred_asset", "ETH-USD")
    symbol = preferred.replace("-USD", "")
    asset_data = prices.get(symbol, {})
    eth_data = prices.get("ETH", {})

    change_1h = (asset_data.get("price_change_percentage_1h_in_currency") or
                 asset_data.get("change_1h") or 0)
    change_24h = (asset_data.get("price_change_percentage_24h") or
                  asset_data.get("change_24h") or 0)
    threshold = strategy["momentum_threshold_1h"]

    if change_1h > threshold and change_24h > -8:
        trade_usd = min(cash * 0.9, total_balance * (max_invested / 100) - (total_balance - cash))
        trade_usd = max(2.0, round(trade_usd, 2))
        if trade_usd > cash:
            trade_usd = round(cash * 0.9, 2)
        return (preferred, trade_usd), (
            f"{symbol} momentum: {change_1h:+.2f}% 1h, {change_24h:+.2f}% 24h "
            f"(threshold: {threshold}%) | regime: {strategy.get('market_regime','?')}"
        )

    return None, f"No signal — {symbol} 1h={change_1h:+.2f}% below threshold {threshold}%"

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

    # Update peak prices for trailing stops
    if "ETH" in state["positions"] and eth_price:
        pos = state["positions"]["ETH"]
        pos["peak_price"] = max(pos.get("peak_price", eth_price), eth_price)

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

    # ── Check exit conditions ──
    if "ETH" in state["positions"] and eth_price:
        should_exit, reason = should_sell_eth(state, eth_price)
        pos = state["positions"]["ETH"]
        unrealized_pnl = (eth_price - pos["entry"]) * pos["qty"]
        unrealized_pct = (eth_price - pos["entry"]) / pos["entry"] * 100

        report_lines.append(f"**Open Position:** ETH {pos['qty']:.6f} @ ${pos['entry']:.2f} entry")
        report_lines.append(f"  Current: ${eth_price:.2f} | P&L: {'+' if unrealized_pnl >= 0 else ''}${unrealized_pnl:.2f} ({'+' if unrealized_pct >= 0 else ''}{unrealized_pct:.1f}%)")
        report_lines.append(f"  Stop: ${pos['stop_loss']:.2f} | Target: ${pos['target']:.2f}")

        if should_exit:
            # SELL
            report_lines.append(f"  🚨 **EXIT TRIGGERED:** {reason}")
            eth_qty = live_positions.get("ETH", {}).get("qty", pos["qty"])
            status, result = market_sell("ETH-USD", f"{eth_qty:.6f}")
            if result.get("success"):
                order_id = result["success_response"]["order_id"]
                time.sleep(1)
                order = get_order(order_id)
                exit_price = float(order.get("average_filled_price", eth_price))
                realized_pnl = (exit_price - pos["entry"]) * pos["qty"]
                state["trades"].append({
                    "time": ts, "asset": "ETH", "side": "SELL",
                    "qty": pos["qty"], "entry": pos["entry"], "exit": exit_price,
                    "sizeUsd": pos.get("size_usd", 0),
                    "pnl": realized_pnl, "reason": reason,
                    "strategy_context": f"ETH at ${eth_price:.2f}. Entry was ${pos['entry']:.2f}. " +
                        f"Stop-loss was ${pos['stop_loss']:.2f}, target was ${pos['target']:.2f}. " +
                        f"Held for {round((time.time() - pos.get('entry_time', time.time())) / 3600, 1)}h.",
                    "signals": [f"ETH {eth.get('change_1h',0):+.2f}% 1h", f"Fear&Greed: {fear_greed_val}", f"BTC ${btc_price:,.0f}"]
                })
                del state["positions"]["ETH"]
                actions_taken.append(f"✅ Sold ETH @ ${exit_price:.2f} | P&L: ${realized_pnl:+.2f}")
                report_lines.append(f"  ✅ **SOLD** @ ${exit_price:.2f} | Realized P&L: ${realized_pnl:+.2f}")

    # ── XRP Scalp Layer ──
    xrp_price = prices.get("XRP", {}).get("price", 0)
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

    # ── Check entry conditions ──
    if "ETH" not in state["positions"]:
        trade_signal, signal_reason = decide_next_trade(state, cash, prices, total)
        if trade_signal:
            product_id, trade_usd = trade_signal
            report_lines.append(f"")
            report_lines.append(f"📈 **ENTRY SIGNAL:** {signal_reason}")
            status, result = market_buy(product_id, trade_usd)
            if result.get("success"):
                order_id = result["success_response"]["order_id"]
                time.sleep(1)
                order = get_order(order_id)
                entry_price = float(order.get("average_filled_price", eth_price))
                qty = float(order.get("filled_size", 0))
                stop = entry_price * 0.90
                target = entry_price * 1.08
                eth_1h = prices.get("ETH", {}).get("change_1h", 0) or 0
                state["positions"]["ETH"] = {
                    "qty": qty, "entry": entry_price,
                    "stop_loss": stop, "target": target,
                    "size_usd": trade_usd, "peak_price": entry_price,
                    "order_id": order_id, "entry_time": time.time()
                }
                # Log as a trade entry with full context
                state["trades"].append({
                    "time": ts, "asset": "ETH", "side": "BUY",
                    "qty": qty, "entry": entry_price, "exit": None,
                    "sizeUsd": trade_usd, "pnl": None,
                    "reason": signal_reason,
                    "strategy_context": f"ETH at ${entry_price:.2f} | 1h momentum: {eth_1h:+.2f}% | "
                        f"Stop: ${stop:.2f} (-10%) | Target: ${target:.2f} (+8%) | "
                        f"Capital deployed: ${trade_usd:.2f} of ${total:.2f} portfolio ({trade_usd/total*100:.0f}%)",
                    "signals": [f"1h: {eth_1h:+.2f}%", f"24h: {prices.get('ETH',{}).get('change_24h',0):+.2f}%",
                                f"BTC ${btc_price:,.0f}", f"Vol: ${prices.get('ETH',{}).get('volume',0)/1e9:.1f}B"]
                })
                actions_taken.append(f"✅ Bought {qty:.6f} ETH @ ${entry_price:.2f}")
                report_lines.append(f"✅ **BOUGHT** {qty:.6f} ETH @ ${entry_price:.2f} | Stop: ${stop:.2f} | Target: ${target:.2f}")
        else:
            report_lines.append(f"")
            report_lines.append(f"⏸️ **No new entry:** {signal_reason}")

    # ── Update live prices on positions for dashboard ──
    if "ETH" in state["positions"] and eth_price:
        state["positions"]["ETH"]["current_price"] = eth_price

    # ── Store cash balance ──
    state["cash"] = round(cash, 2)

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
