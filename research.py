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

def fetch_glint_signals():
    """
    Read recent Glint signals from the #tradingbot Discord channel via bot API.
    Glint posts as embeds — we fetch channel messages using the bot token.
    Returns list of parsed signal dicts with full embed content.
    """
    import subprocess, os
    signals = []
    try:
        # Get bot token from env
        env_file = os.path.expanduser("~/.openclaw/.env")
        token = None
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    if line.startswith("DISCORD_BOT_TOKEN="):
                        token = line.strip().split("=", 1)[1].strip('"\'')
                        break

        if not token:
            return signals

        # Fetch messages from channel via Discord API
        headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
        r = requests.get(
            "https://discord.com/api/v10/channels/1488011252256211004/messages?limit=10",
            headers=headers, timeout=8
        )
        if r.status_code != 200:
            return signals

        messages = r.json()
        for msg in messages:
            author = msg.get("author", {}).get("username", "")
            if "glint" not in author.lower() and "Glint" not in str(msg.get("embeds", "")):
                continue

            # Extract embed content
            embeds = msg.get("embeds", [])
            for embed in embeds:
                title = embed.get("title", "")
                desc = embed.get("description", "")
                fields = embed.get("fields", [])
                full_text = f"{title} {desc} " + " ".join(f.get("value","") for f in fields)
                if full_text.strip():
                    signals.append({"raw": full_text.strip(), "source": "Glint", "sentiment": "neutral"})

            # Also check plain text content (some webhooks send as content)
            content = msg.get("content", "")
            if content and len(content) > 20:
                signals.append({"raw": content, "source": "Glint", "sentiment": "neutral"})

    except Exception as e:
        pass

    return signals

def parse_glint_sentiment(signals):
    """
    Analyze Glint signals for crypto market impact.
    Returns (sentiment_score, key_signals) where sentiment_score is -1 to +1.
    """
    score = 0
    key = []
    
    for s in signals:
        raw = s.get("raw", "").lower()
        
        # Bullish signals
        if any(kw in raw for kw in ["rate cut", "fed cut", "bullish", "rally", "etf approved", "institutional"]):
            score += 0.5
            key.append(f"🟢 BULLISH: {s['raw'][:80]}")
        
        # Bearish signals  
        elif any(kw in raw for kw in ["rate hike", "crash", "ban", "regulation", "sec", "arrest", "hack", "exploit"]):
            score -= 0.5
            key.append(f"🔴 BEARISH: {s['raw'][:80]}")
        
        # Neutral/uncertain
        else:
            key.append(f"🟡 NEUTRAL: {s['raw'][:80]}")
    
    return max(-1, min(1, score)), key

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

def determine_strategy(fear_greed, avg_7d_fg, market_data, global_data, perf, state, **kwargs):
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
    # Goal: $40 → $1000 in 30 days requires ~13.5% daily compounding.
    # MINIMUM take-profit floor is 8% regardless of regime — we cannot afford to
    # exit positions early at 5% when we need 13%+ daily compounding to hit target.
    if "extreme_fear" in regime:
        # Extreme fear = volatility = bigger swings available. Use trailing stop to lock gains.
        new_tp = 10.0
        reasoning_parts.append("Extreme fear → TP=10% (volatility = bigger swings; use trailing stop, not early exit)")
    elif "fear" in regime and eth_7d < -5:
        new_tp = 8.0
        reasoning_parts.append("Fear + downtrend → take-profit 8% (floor for goal math)")
    elif "greed" in regime or (eth_7d > 10 and eth_24h > 2):
        new_tp = 15.0
        reasoning_parts.append("Greed/uptrend → widening take-profit to 15% (let winners run hard)")
    elif "bull" in regime:
        new_tp = 12.0
        reasoning_parts.append("Bull regime → take-profit 12%")
    else:
        new_tp = 10.0
        reasoning_parts.append("Neutral regime → take-profit 10% (goal-adjusted floor)")
    
    # Apply TP change only on significant shift (≥1%) or very first run
    if abs(new_tp - old_tp) >= 1.0:
        changes.append(f"take_profit: {old_tp}% → {new_tp}%")
        strategy["take_profit_pct"] = new_tp
    elif abs(new_tp - old_tp) >= 0.01:
        # Small drift (e.g., glint residual) — silently snap to regime target, no log
        strategy["take_profit_pct"] = new_tp

    # ── Adjust stop-loss: always derived from TP to maintain 2:1 ratio ──
    # DEBOUNCED: only log changes if they actually move the value.
    # We always SET the SL to maintain the ratio, but only LOG when it changes.
    old_sl = strategy["stop_loss_pct"]
    new_sl = round(strategy["take_profit_pct"] / 2.0, 2)  # 2:1 TP:SL ratio
    # Always apply the derived SL — this keeps the ratio correct
    strategy["stop_loss_pct"] = new_sl
    if abs(new_sl - old_sl) > 0.05:
        changes.append(f"stop_loss: {old_sl}% → {new_sl}% (derived from TP/2.0)")
        reasoning_parts.append(f"Stop-loss set to {new_sl}% (TP÷2.0 ratio — 2:1 R:R maintained)")

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

    # Only log actual changes — no empty or near-empty entries
    if len(changes) >= 1:
        # Debounce: skip if this exact change set was logged in the last run
        prev = strategy.get("adjustments_log", [])
        if prev:
            last_changes = prev[-1].get("changes", [])
            if changes == last_changes:
                pass  # Skip duplicate — same adjustments as last run
            else:
                strategy["adjustments_log"].append({
                    "time": now,
                    "changes": changes,
                    "regime": regime,
                    "fear_greed": fear_greed
                })
        else:
            strategy["adjustments_log"].append({
                "time": now,
                "changes": changes,
                "regime": regime,
                "fear_greed": fear_greed
            })
        # Keep last 48 adjustments
        strategy["adjustments_log"] = strategy["adjustments_log"][-48:]

    # ── Glint macro signal override ───────────────────────────────────────
    # If Glint has a strong bullish/bearish signal, adjust take-profit.
    # Debounced: only re-apply if glint_score has changed since last run (avoids
    # the base-TP-reset → GLINT-cut → base-TP-reset oscillation every hour).
    glint_score = kwargs.get("glint_score", 0)
    prev_glint_score = strategy.get("glint_score", 0)
    strategy["glint_score"] = glint_score  # persist for debounce next run
    glint_changed = abs(glint_score - prev_glint_score) >= 0.1
    if glint_score >= 0.5:
        old_tp = strategy["take_profit_pct"]
        new_tp = min(old_tp * 1.5, 15.0)  # boost take-profit by 50%, cap at 15%
        if new_tp > old_tp + 1 and glint_changed:
            changes.append(f"take_profit GLINT BOOST: {old_tp}% → {new_tp:.1f}% (macro bullish signal)")
            strategy["take_profit_pct"] = new_tp
            reasoning_parts.append(f"Glint macro signal bullish ({glint_score:+.1f}) → extending take-profit to {new_tp:.1f}%")
        elif new_tp > old_tp + 1:
            # Apply silently (no log entry) — glint hasn't changed, just re-applying stable override
            strategy["take_profit_pct"] = new_tp
    elif glint_score <= -0.5:
        old_tp = strategy["take_profit_pct"]
        new_tp = max(old_tp * 0.7, 4.5)  # shrink take-profit, take gains fast — floor 4.5%
        if new_tp < old_tp - 0.5:
            # Always co-adjust SL to maintain 1.5:1 TP:SL ratio
            new_sl = round(new_tp / 1.5, 2)
            if glint_changed:
                changes.append(f"take_profit GLINT CUT: {old_tp}% → {new_tp:.1f}% (macro bearish signal)")
                changes.append(f"stop_loss GLINT CO-ADJUST: {strategy['stop_loss_pct']}% → {new_sl}% (TP:SL ratio lock 1.5:1)")
                reasoning_parts.append(f"Glint macro signal bearish ({glint_score:+.1f}) → tightening take-profit to {new_tp:.1f}%, SL to {new_sl}%")
            # Always apply the override regardless (keeps values consistent), just don't spam the log
            strategy["take_profit_pct"] = new_tp
            strategy["stop_loss_pct"] = new_sl

    # ── Final ratio lock: enforce TP:SL ≥ 2:1 after all adjustments ──
    # Since SL is already derived from TP/2.0 above, this should be a no-op
    # unless GLINT or something else mutated the ratio. Guard belt.
    final_tp = strategy["take_profit_pct"]
    final_sl = strategy["stop_loss_pct"]
    target_min_sl = round(final_tp / 2.0, 2)
    if final_sl > 0 and final_sl != target_min_sl:
        if abs(final_sl - target_min_sl) > 0.05:
            changes.append(f"stop_loss ratio-locked: {final_sl}% → {target_min_sl}% (enforce TP:SL=2:1)")
        strategy["stop_loss_pct"] = target_min_sl

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
    glint_signals = fetch_glint_signals()
    glint_score, glint_key = parse_glint_sentiment(glint_signals)

    # Determine and save new strategy
    strategy, changes = determine_strategy(
        fear_greed, avg_7d_fg or 50,
        market_data, global_data, perf, state,
        glint_score=glint_score
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

    if glint_key:
        lines += [f"", f"⚡ **Glint Macro Signals (score: {glint_score:+.1f}):**"]
        for k in glint_key[:3]:
            lines.append(f"  {k}")

    if trending:
        names = [c.get("name","?") for c in trending[:3]]
        lines += [f"", f"🔥 **Trending:** {', '.join(names)}"]

    if gainers:
        gainer_strs = [f"{g['symbol'].upper()} {g.get('price_change_percentage_24h',0):+.1f}%" for g in gainers[:3]]
        lines += [f"📈 **Top movers (24h):** {' | '.join(gainer_strs)}"]

    # ── Strategy improvement research ──────────────────────────────────────
    improvement_notes = run_strategy_improvement_research(state, strategy, perf, market_data)
    if improvement_notes:
        lines += ["", "🧠 **Strategy Improvement Notes:**"]
        for note in improvement_notes:
            lines.append(f"  • {note}")

    return "\n".join(lines)


def run_strategy_improvement_research(state, strategy, perf, market_data):
    """
    Meta-research: analyze performance and find ways to improve.
    Returns list of actionable improvement notes.
    """
    notes = []
    trades = state.get("trades", [])
    completed = [t for t in trades if t.get("pnl") is not None]

    # ── Performance analysis ──
    if completed:
        wins = [t for t in completed if t["pnl"] > 0]
        losses = [t for t in completed if t["pnl"] < 0]
        total_pnl = sum(t["pnl"] for t in completed)
        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        win_rate = len(wins) / len(completed)

        if win_rate < 0.4 and len(completed) >= 3:
            notes.append(f"Win rate {win_rate*100:.0f}% is below target — consider tightening entry criteria or wider stop-loss to avoid early shakeouts")

        if avg_loss and avg_win and abs(avg_loss) > avg_win:
            notes.append(f"Avg loss (${avg_loss:.2f}) exceeds avg win (${avg_win:.2f}) — risk/reward ratio unfavorable, consider tighter stops or larger take-profit targets")

        if len(losses) >= 2:
            consecutive = 0
            for t in reversed(completed):
                if t["pnl"] < 0: consecutive += 1
                else: break
            if consecutive >= 2:
                notes.append(f"{consecutive} consecutive losses — entering cooldown mode recommendation: skip next entry, wait for stronger signal")

    # ── Fee impact analysis ──
    total_trades = len(completed)
    if total_trades > 0:
        est_fees = total_trades * 2 * 0.012  # 1.2% each side
        pct_of_portfolio = est_fees / 40 * 100
        if pct_of_portfolio > 5:
            notes.append(f"Fees eating ~{pct_of_portfolio:.1f}% of capital ({total_trades} trades × 2.4% round-trip) — reduce trade frequency or target larger moves")

    # ── Market opportunity research ──
    eth = market_data.get("ETH", {})
    btc = market_data.get("BTC", {})
    eth_7d = eth.get("price_change_percentage_7d_in_currency", 0) or 0
    btc_7d = btc.get("price_change_percentage_7d_in_currency", 0) or 0

    if eth_7d < -10 and btc_7d < -10:
        notes.append("Both ETH and BTC down >10% on 7d — consider reducing position size until trend stabilizes")
    elif eth_7d > 10:
        notes.append(f"ETH up {eth_7d:.1f}% on 7d — bullish trend, could widen take-profit target to capture larger moves")

    # ── Position sizing improvement ──
    portfolio_val = state.get("balance_history", [{}])[-1].get("value", 40)
    if portfolio_val > 50 and strategy.get("max_position_pct", 80) < 85:
        notes.append(f"Portfolio at ${portfolio_val:.2f} — could increase position size to 85% now that we have more buffer")

    # ── Timing patterns ──
    if completed:
        # Check if losses happen at certain times (simple check)
        loss_times = [t["time"][11:13] for t in completed if t["pnl"] < 0 and t.get("time")]
        if loss_times:
            from collections import Counter
            bad_hours = Counter(loss_times).most_common(1)
            if bad_hours and bad_hours[0][1] >= 2:
                notes.append(f"Pattern detected: losses cluster around {bad_hours[0][0]}:00 UTC — consider avoiding entries at that hour")

    if not notes:
        notes.append("Strategy performing within parameters — no changes recommended this cycle")

    return notes

if __name__ == "__main__":
    print(run_research())
