#!/usr/bin/env python3
"""
Hourly research engine — scrapes market data, analyzes performance,
and rewrites bot strategy parameters to optimize for current conditions.
"""

import requests, json, os, time
from datetime import datetime, timezone

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
STRATEGY_FILE = os.path.join(os.path.dirname(__file__), "strategy.json")

DEFAULT_STRATEGY = {
    "take_profit_pct": 8.0,
    "stop_loss_pct": 10.0,
    "momentum_threshold_1h": 0.3,
    "max_position_pct": 80.0,
    "preferred_asset": "ETH-USD",
    "last_updated": None,
    "reasoning": "Default parameters — no data yet",
    "market_regime": "unknown",
    "fear_greed": None,
    "adjustments_log": []
}

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def load_strategy():
    if os.path.exists(STRATEGY_FILE):
        with open(STRATEGY_FILE) as f:
            return json.load(f)
    return dict(DEFAULT_STRATEGY)

def save_strategy(s):
    with open(STRATEGY_FILE, "w") as f:
        json.dump(s, f, indent=2)

def fetch_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=7", timeout=10)
        data = r.json()["data"]
        current = int(data[0]["value"])
        avg_7d = sum(int(d["value"]) for d in data) / len(data)
        return current, avg_7d, data[0]["value_classification"]
    except:
        return None, None, None

def fetch_market_data():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets"
            "?vs_currency=usd&ids=bitcoin,ethereum,solana,ripple"
            "&order=market_cap_desc&per_page=4&sparkline=false"
            "&price_change_percentage=1h,24h,7d",
            timeout=10
        )
        return {c["symbol"].upper(): c for c in r.json()}
    except:
        return {}

def fetch_global():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=10)
        return r.json().get("data", {})
    except:
        return {}

def fetch_trending():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=10)
        return [c["item"] for c in r.json().get("coins", [])[:5]]
    except:
        return []

def fetch_top_gainers():
    """Get top movers from CoinGecko"""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets"
            "?vs_currency=usd&order=percent_change_24h_desc&per_page=20&page=1"
            "&sparkline=false&price_change_percentage=24h"
            "&ids=bitcoin,ethereum,solana,ripple,binancecoin,cardano,avalanche-2,chainlink,polygon,uniswap",
            timeout=10
        )
        coins = r.json()
        gainers = sorted(coins, key=lambda x: x.get("price_change_percentage_24h", 0), reverse=True)
        return gainers[:3]
    except:
        return []

def analyze_performance(state):
    """Analyze recent trade history to assess strategy performance"""
    trades = state.get("trades", [])
    if not trades:
        return {"win_rate": None, "avg_pnl": None, "recent_losses": 0, "note": "No trades yet"}
    
    completed = [t for t in trades if t.get("pnl") is not None]
    if not completed:
        return {"win_rate": None, "avg_pnl": None, "recent_losses": 0, "note": "No completed trades"}
    
    wins = [t for t in completed if t["pnl"] > 0]
    losses = [t for t in completed if t["pnl"] < 0]
    recent = completed[-5:] if len(completed) >= 5 else completed
    recent_losses = len([t for t in recent if t["pnl"] < 0])
    
    return {
        "win_rate": len(wins) / len(completed),
        "avg_pnl": sum(t["pnl"] for t in completed) / len(completed),
        "total_trades": len(completed),
        "recent_losses": recent_losses,
        "note": f"{len(wins)}W/{len(losses)}L"
    }

def determine_strategy(fear_greed, avg_7d_fg, market_data, global_data, perf, state):
    """
    Core research logic — determines optimal parameters based on current conditions.
    Returns updated strategy dict with reasoning.
    """
    strategy = load_strategy()
    changes = []
    reasoning_parts = []

    eth = market_data.get("ETH", {})
    btc = market_data.get("BTC", {})
    
    eth_1h = eth.get("price_change_percentage_1h_in_currency", 0) or 0
    eth_24h = eth.get("price_change_percentage_24h", 0) or 0
    eth_7d = eth.get("price_change_percentage_7d_in_currency", 0) or 0
    btc_24h = btc.get("price_change_percentage_24h", 0) or 0
    
    market_cap_change = global_data.get("market_cap_change_percentage_24h_usd", 0)
    btc_dom = global_data.get("market_cap_percentage", {}).get("btc", 0)

    # ── Determine market regime ──────────────────────────────────────────
    if fear_greed is not None:
        if fear_greed <= 15:
            regime = "extreme_fear"
        elif fear_greed <= 30:
            regime = "fear"
        elif fear_greed <= 55:
            regime = "neutral"
        elif fear_greed <= 75:
            regime = "greed"
        else:
            regime = "extreme_greed"
    else:
        regime = "unknown"

    # Factor in 7d trend
    if eth_7d < -15:
        regime = "bear_" + regime
    elif eth_7d > 15:
        regime = "bull_" + regime

    strategy["market_regime"] = regime
    strategy["fear_greed"] = fear_greed
    reasoning_parts.append(f"Market regime: {regime} | F&G: {fear_greed}/100 (7d avg: {avg_7d_fg:.0f})")
    reasoning_parts.append(f"ETH: 1h={eth_1h:+.2f}% 24h={eth_24h:+.2f}% 7d={eth_7d:+.2f}%")
    reasoning_parts.append(f"BTC: 24h={btc_24h:+.2f}% | Dominance: {btc_dom:.1f}%")

    # ── Adjust take-profit based on regime ────────────────────────────────
    old_tp = strategy["take_profit_pct"]
    if "extreme_fear" in regime:
        # In extreme fear: tighter take-profit (grab gains fast, market is volatile)
        new_tp = 5.0
        reasoning_parts.append("Extreme fear → tightening take-profit to 5% (high volatility, take gains fast)")
    elif "fear" in regime and eth_7d < -5:
        new_tp = 6.0
        reasoning_parts.append("Fear + downtrend → take-profit 6% (cautious)")
    elif "greed" in regime or (eth_7d > 10 and eth_24h > 2):
        new_tp = 12.0
        reasoning_parts.append("Greed/uptrend → widening take-profit to 12% (let winners run)")
    elif "bull" in regime:
        new_tp = 10.0
        reasoning_parts.append("Bull regime → take-profit 10%")
    else:
        new_tp = 8.0
        reasoning_parts.append("Neutral regime → standard take-profit 8%")
    
    if abs(new_tp - old_tp) >= 1:
        changes.append(f"take_profit: {old_tp}% → {new_tp}%")
        strategy["take_profit_pct"] = new_tp

    # ── Adjust stop-loss ────────────────────────────────────────────────
    old_sl = strategy["stop_loss_pct"]
    if "extreme_fear" in regime or "bear" in regime:
        # Tighter stop in fear/bear — don't let losses run
        new_sl = 7.0
        reasoning_parts.append("Bear/extreme fear → tightening stop-loss to 7%")
    elif "extreme_greed" in regime:
        new_sl = 12.0
        reasoning_parts.append("Extreme greed → wider stop 12% (less whipsaw risk)")
    else:
        new_sl = 10.0
    
    if abs(new_sl - old_sl) >= 1:
        changes.append(f"stop_loss: {old_sl}% → {new_sl}%")
        strategy["stop_loss_pct"] = new_sl

    # ── Adjust momentum threshold ──────────────────────────────────────
    old_mom = strategy["momentum_threshold_1h"]
    if "extreme_fear" in regime:
        # Lower threshold in fear — harder to get signals, accept weaker ones
        new_mom = 0.1
        reasoning_parts.append("Extreme fear → lowering 1h momentum threshold to 0.1% (scarce signals)")
    elif "greed" in regime or "bull" in regime:
        new_mom = 0.5
        reasoning_parts.append("Bull/greed → raising momentum threshold to 0.5% (be selective)")
    else:
        new_mom = 0.3
    
    if abs(new_mom - old_mom) >= 0.1:
        changes.append(f"momentum_1h_threshold: {old_mom} → {new_mom}")
        strategy["momentum_threshold_1h"] = new_mom

    # ── Adjust position sizing ─────────────────────────────────────────
    old_pos = strategy["max_position_pct"]
    if perf.get("recent_losses", 0) >= 3:
        # 3 recent losses → reduce size
        new_pos = 60.0
        reasoning_parts.append("3+ recent losses → reducing position size to 60%")
    elif "extreme_fear" in regime:
        new_pos = 70.0
        reasoning_parts.append("Extreme fear → 70% max position (keep more buffer)")
    elif "greed" in regime and perf.get("win_rate", 0) and perf["win_rate"] > 0.6:
        new_pos = 85.0
        reasoning_parts.append("Greed + >60% win rate → increasing position to 85%")
    else:
        new_pos = 80.0
    
    if abs(new_pos - old_pos) >= 5:
        changes.append(f"max_position: {old_pos}% → {new_pos}%")
        strategy["max_position_pct"] = new_pos

    # ── Asset selection ────────────────────────────────────────────────
    # Compare ETH vs SOL vs BTC momentum — pick strongest
    sol = market_data.get("SOL", {})
    sol_1h = sol.get("price_change_percentage_1h_in_currency", 0) or 0
    sol_24h = sol.get("price_change_percentage_24h", 0) or 0
    
    best_asset = "ETH-USD"
    best_score = eth_1h * 0.7 + eth_24h * 0.3
    
    sol_score = sol_1h * 0.7 + sol_24h * 0.3
    if sol_score > best_score + 0.5:  # SOL needs clear advantage
        best_asset = "SOL-USD"
        best_score = sol_score
        reasoning_parts.append(f"SOL showing stronger momentum ({sol_score:.2f}) vs ETH ({best_score:.2f}) → switching to SOL")
    
    if best_asset != strategy.get("preferred_asset"):
        changes.append(f"preferred_asset: {strategy['preferred_asset']} → {best_asset}")
        strategy["preferred_asset"] = best_asset

    # ── Log the update ────────────────────────────────────────────────
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    strategy["last_updated"] = now
    strategy["reasoning"] = " | ".join(reasoning_parts)

    if changes:
        strategy["adjustments_log"].append({
            "time": now,
            "changes": changes,
            "regime": regime,
            "fear_greed": fear_greed
        })
        # Keep last 48 adjustments
        strategy["adjustments_log"] = strategy["adjustments_log"][-48:]

    return strategy, changes

def run_research():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    state = load_state()
    
    # Fetch all data
    fear_greed, avg_7d_fg, fg_label = fetch_fear_greed()
    market_data = fetch_market_data()
    global_data = fetch_global()
    trending = fetch_trending()
    gainers = fetch_top_gainers()
    perf = analyze_performance(state)

    # Determine and save new strategy
    strategy, changes = determine_strategy(
        fear_greed, avg_7d_fg or 50,
        market_data, global_data, perf, state
    )
    save_strategy(strategy)

    # Build report
    eth = market_data.get("ETH", {})
    btc = market_data.get("BTC", {})
    sol = market_data.get("SOL", {})

    lines = [
        f"🔬 **RESEARCH UPDATE — {ts}**",
        f"",
        f"**Market Regime:** `{strategy['market_regime']}` | **F&G:** {fear_greed}/100 ({fg_label}) | 7d avg: {avg_7d_fg:.0f}",
        f"",
        f"**Asset Momentum:**",
        f"  ETH: 1h={eth.get('price_change_percentage_1h_in_currency',0):+.2f}% | 24h={eth.get('price_change_percentage_24h',0):+.2f}% | 7d={eth.get('price_change_percentage_7d_in_currency',0):+.2f}%",
        f"  BTC: 1h={btc.get('price_change_percentage_1h_in_currency',0):+.2f}% | 24h={btc.get('price_change_percentage_24h',0):+.2f}% | 7d={btc.get('price_change_percentage_7d_in_currency',0):+.2f}%",
        f"  SOL: 1h={sol.get('price_change_percentage_1h_in_currency',0):+.2f}% | 24h={sol.get('price_change_percentage_24h',0):+.2f}% | 7d={sol.get('price_change_percentage_7d_in_currency',0):+.2f}%",
        f"",
        f"**Performance:** {perf.get('note', 'No trades')} | Win rate: {(perf.get('win_rate') or 0)*100:.0f}% | Avg P&L: ${perf.get('avg_pnl') or 0:.2f}",
    ]

    if changes:
        lines += [f"", f"⚙️ **Strategy Updated:**"]
        for c in changes:
            lines.append(f"  • {c}")
    else:
        lines.append(f"")
        lines.append(f"✅ **Strategy unchanged** — current params optimal for conditions")

    lines += [
        f"",
        f"**Active Parameters:**",
        f"  Take-profit: {strategy['take_profit_pct']}% | Stop-loss: {strategy['stop_loss_pct']}% | Max position: {strategy['max_position_pct']}%",
        f"  Momentum threshold: {strategy['momentum_threshold_1h']}%/1h | Preferred asset: {strategy['preferred_asset']}",
    ]

    if trending:
        names = [c.get("name","?") for c in trending[:3]]
        lines += [f"", f"🔥 **Trending:** {', '.join(names)}"]

    if gainers:
        gainer_strs = [f"{g['symbol'].upper()} {g.get('price_change_percentage_24h',0):+.1f}%" for g in gainers[:3]]
        lines += [f"📈 **Top movers (24h):** {' | '.join(gainer_strs)}"]

    return "\n".join(lines)

if __name__ == "__main__":
    print(run_research())
