"""
find_traders.py — Hyperliquid Leaderboard Deep Scanner v3
══════════════════════════════════════════════════════════
3-stage scan. Finds the highest-frequency, strongest win-rate, hottest-streak
traders on Hyperliquid for copy trading.

  Stage 1 — leaderboard filter: PnL / volume / weekly positive
  Stage 2 — fill history:       frequency, hold time, recent WR, streak
  Stage 3 — deep dive (top 10): per-coin WR, weekly consistency,
                                 max loss streak, live positions

Output: data/traders.json  (bot loads this automatically on restart)

Run on VPS:
    cd /root/hl-bot && python src/find_traders.py

Then:
    # Paste the .env lines printed at the end, then:
    sudo systemctl restart hl-bot
"""
import asyncio, sys, json, time, os
import pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import aiohttp
from scan_common import hold_stats, legacy_avg_hold, fmt_hold, fmt_hold_short

STATS_URL   = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
HL_REST     = "https://api.hyperliquid.xyz/info"
OUTPUT_FILE = "data/traders.json"

# ── Stage 1 filters ───────────────────────────────────────────────────────────
MIN_ALLTIME_PNL  = 50_000
MIN_WEEK_PNL     = 0            # net positive this week
MIN_DAY_VLM      = 2_000        # active today
MIN_ALLTIME_VLM  = 100_000
STAGE1_LIMIT     = 200          # how many to deep-scan

# ── Stage 2 filters (loose — score does the real ranking) ────────────────────
MIN_TRADES_PER_DAY  = 1.0
MAX_AVG_HOLD_HOURS  = 24.0
MIN_WIN_RATE_RECENT = 0.50
MIN_FILLS           = 10
MIN_TRADES_7D       = 3
STAGE2_TOP_N        = 10        # feed best 10 into Stage 3

# ── Final output ──────────────────────────────────────────────────────────────
TOP_N = 15                      # traders written to traders.json


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def parse_windows(perfs):
    out = {}
    for item in perfs:
        if isinstance(item, list) and len(item) == 2:
            window, stats = item
            out[window] = {
                "pnl": float(stats.get("pnl", 0)),
                "vlm": float(stats.get("vlm", 0)),
                "wr":  float(stats.get("winRate", 0.5)),
            }
    return out


def calc_streak(closes: list[dict]) -> int:
    """Return current win (+) or loss (−) streak from a sorted close list."""
    streak = 0
    for f in reversed(closes):
        pnl = float(f.get("closedPnl", 0))
        if pnl == 0:
            break
        if streak == 0:
            streak = 1 if pnl > 0 else -1
        elif streak > 0 and pnl > 0:
            streak += 1
        elif streak < 0 and pnl < 0:
            streak -= 1
        else:
            break
    return streak


# Hold time now comes from scan_common.hold_stats (episode reconstruction over full
# history; counts still-open positions). The old 30-day Open→Close pairing produced
# "?" for exactly the longest-hold traders — see scan_common.py for the full writeup.


# ══════════════════════════════════════════════════════════════════════════════
# Stage 2 — fill profile
# ══════════════════════════════════════════════════════════════════════════════

async def profile_trader(session, address: str) -> tuple[dict | None, list]:
    """
    Returns (stats_dict, raw_fills).
    raw_fills is kept for Stage 3 re-use — avoids re-fetching.
    """
    try:
        async with session.post(
            HL_REST,
            json={"type": "userFills", "user": address},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            fills = await resp.json(content_type=None)

        if not fills or len(fills) < MIN_FILLS:
            return None, []

        now_ms   = time.time() * 1000
        month_ms = 30 * 86_400_000
        week_ms  =  7 * 86_400_000

        fills = sorted(fills, key=lambda f: float(f.get("time", 0)))
        recent30 = [f for f in fills if now_ms - float(f.get("time", 0)) < month_ms]
        last7d   = [f for f in fills if now_ms - float(f.get("time", 0)) < week_ms]

        if len(recent30) < MIN_FILLS:
            return None, fills

        trades_per_day = len(recent30) / 30

        all_closes    = [f for f in fills    if float(f.get("closedPnl", 0)) != 0]
        recent_closes = [f for f in recent30 if float(f.get("closedPnl", 0)) != 0]

        def wr(subset):
            if len(subset) < 3:
                return 0.5
            wins = sum(1 for f in subset if float(f.get("closedPnl", 0)) > 0)
            return wins / len(subset)

        win_rate_all    = wr(all_closes)
        win_rate_recent = wr(all_closes[-50:])   # last 50 across full history

        streak    = calc_streak(all_closes)
        hs        = hold_stats(fills, now_ms)   # full history, episode-based, incl. open
        avg_hold  = legacy_avg_hold(hs)

        notionals    = [float(f.get("sz", 0)) * float(f.get("px", 0)) for f in recent30 if float(f.get("sz", 0)) > 0]
        avg_notional = sum(notionals) / len(notionals) if notionals else 0

        last_trade_h = (now_ms - float(fills[-1].get("time", 0))) / 3_600_000

        return {
            "trades_per_day":  trades_per_day,
            "avg_hold_h":      avg_hold,
            "med_hold_h":      hs["med_closed_h"],
            "med_open_h":      hs["med_open_h"],
            "n_open_pos":      hs["n_open"],
            "pct_intraday":    hs["pct_intraday"],
            "pct_multiday":    hs["pct_multiday"],
            "hold_str":        fmt_hold_short(hs),
            "hold_full":       fmt_hold(hs),
            "win_rate_all":    win_rate_all,
            "win_rate_recent": win_rate_recent,
            "current_streak":  streak,
            "trades_7d":       len(last7d),
            "last_trade_h":    last_trade_h,
            "avg_notional":    avg_notional,
            "total_fills":     len(fills),
            "closes_found":    len(all_closes),
        }, fills

    except Exception:
        return None, []


def passes_stage2(p: dict) -> bool:
    return (
        p["trades_per_day"]      >= MIN_TRADES_PER_DAY
        and p["avg_hold_h"]      <= MAX_AVG_HOLD_HOURS
        and p["win_rate_recent"] >= MIN_WIN_RATE_RECENT
        and p["trades_7d"]       >= MIN_TRADES_7D
        and p["last_trade_h"]    <  72
    )


def composite_score(t: dict) -> float:
    freq   = t.get("trades_per_day", 0)
    wr     = t.get("win_rate_recent", 0.5)
    streak = t.get("current_streak", 0)
    hold   = max(t.get("avg_hold_h", 99), 0.1)
    pnl    = t.get("alltime_pnl", 0)
    last_h = t.get("last_trade_h", 999)

    freq_score   = min(freq / 10.0, 1.0)
    wr_score     = max(0, min((wr - 0.50) / 0.30, 1.0))
    streak_score = max(-0.5, min(streak / 10.0, 1.0))
    hold_score   = max(0, 1.0 - hold / 12.0)
    pnl_score    = min(pnl / 1_000_000, 1.0)
    recency      = +0.10 if last_h < 6 else (0.0 if last_h < 24 else (-0.10 if last_h < 72 else -0.30))

    return (
        freq_score   * 0.30
        + wr_score   * 0.25
        + streak_score * 0.20
        + hold_score * 0.15
        + pnl_score  * 0.10
        + recency
    )


# ══════════════════════════════════════════════════════════════════════════════
# Stage 3 — deep dive (top 10 only)
# ══════════════════════════════════════════════════════════════════════════════

async def deep_dive(session, address: str, fills: list[dict]) -> dict:
    """
    Extra signals computed from cached fills + one live state call.
    Returns dict merged into the trader record.
    """
    now_ms   = time.time() * 1000
    week_ms  =  7 * 86_400_000

    all_closes = [f for f in fills if float(f.get("closedPnl", 0)) != 0]

    # ── Per-coin win rate (top 6 coins by trade count) ────────────────────────
    coin_stats: dict[str, dict] = {}
    for f in all_closes:
        coin = f.get("coin", "?")
        pnl  = float(f.get("closedPnl", 0))
        if coin not in coin_stats:
            coin_stats[coin] = {"wins": 0, "losses": 0, "pnl": 0.0}
        if pnl > 0:
            coin_stats[coin]["wins"] += 1
        else:
            coin_stats[coin]["losses"] += 1
        coin_stats[coin]["pnl"] += pnl

    top_coins = sorted(
        coin_stats.items(),
        key=lambda kv: kv[1]["wins"] + kv[1]["losses"],
        reverse=True
    )[:6]

    coin_summary = []
    for coin, s in top_coins:
        total = s["wins"] + s["losses"]
        coin_summary.append({
            "coin": coin,
            "trades": total,
            "wr": round(s["wins"] / total, 3) if total else 0,
            "pnl": round(s["pnl"]),
        })

    # ── Weekly PnL consistency (last 8 calendar weeks) ────────────────────────
    weekly: dict[int, float] = {}
    for f in all_closes:
        ts_ms   = float(f.get("time", 0))
        week_id = int(ts_ms / week_ms)
        weekly[week_id] = weekly.get(week_id, 0) + float(f.get("closedPnl", 0))

    recent_weeks = sorted(weekly.items())[-8:]
    profitable_weeks = sum(1 for _, pnl in recent_weeks if pnl > 0)
    week_consistency = profitable_weeks / len(recent_weeks) if recent_weeks else 0

    # ── Max consecutive loss streak (risk calibration) ────────────────────────
    max_loss_streak = 0
    cur_losses      = 0
    for f in all_closes:
        if float(f.get("closedPnl", 0)) < 0:
            cur_losses += 1
            max_loss_streak = max(max_loss_streak, cur_losses)
        else:
            cur_losses = 0

    # ── Live positions (what are they in RIGHT NOW?) ──────────────────────────
    live_positions = []
    try:
        async with session.post(
            HL_REST,
            json={"type": "clearinghouseState", "user": address},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            state = await resp.json(content_type=None)
        for ap in state.get("assetPositions", []):
            pos  = ap.get("position", {})
            szi  = float(pos.get("szi", 0))
            if szi == 0:
                continue
            coin     = pos.get("coin", "")
            entry_px = float(pos.get("entryPx") or 0)
            notional = abs(szi) * entry_px
            upnl     = float(pos.get("unrealizedPnl") or 0)
            lev      = float(pos.get("leverage", {}).get("value", 1)) if isinstance(pos.get("leverage"), dict) else float(pos.get("leverage") or 1)
            live_positions.append({
                "coin": coin,
                "side": "long" if szi > 0 else "short",
                "notional": round(notional),
                "upnl":     round(upnl, 2),
                "leverage": round(lev, 1),
            })
    except Exception:
        pass

    return {
        "coin_breakdown":     coin_summary,
        "week_consistency":   round(week_consistency, 3),
        "profitable_weeks":   profitable_weeks,
        "total_weeks_scanned": len(recent_weeks),
        "max_loss_streak":    max_loss_streak,
        "live_positions":     live_positions,
        "live_position_count": len(live_positions),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Output helpers
# ══════════════════════════════════════════════════════════════════════════════

def print_summary_table(traders: list[dict], title: str):
    print(f"\n{'═'*110}")
    print(f"  {title}")
    print(f"{'═'*110}")
    print(f"  {'#':<3} {'Address':<14} {'PnL':>10} {'WR-rec':>7} {'WR-all':>7} "
          f"{'T/day':>6} {'Hold':>7} {'Streak':>7} {'7d':>5} {'Active':>9} {'Score':>7}")
    print(f"  {'-'*105}")
    for i, t in enumerate(traders, 1):
        ss   = f"+{t['current_streak']}" if t['current_streak'] > 0 else str(t['current_streak'])
        lh   = f"{t['last_trade_h']:.0f}h ago"
        hold = t.get('hold_str', '?')
        print(
            f"  {i:<3} {t['address'][:12]+'…':<14} "
            f"${t['alltime_pnl']:>9,.0f} "
            f"{t['win_rate_recent']:>7.1%} "
            f"{t['win_rate_all']:>7.1%} "
            f"{t['trades_per_day']:>6.1f} "
            f"{hold:>7} "
            f"{ss:>7} "
            f"{t['trades_7d']:>5} "
            f"{lh:>9} "
            f"{t['score']:>7.4f}"
        )


def print_deep_card(i: int, t: dict):
    """Print the full Stage 3 card for one trader."""
    dd = t.get("_deep", {})
    ss = f"+{t['current_streak']}" if t['current_streak'] > 0 else str(t['current_streak'])

    print(f"\n  ┌─ #{i} {t['address']} ({'score: ' + str(round(t['score'], 4))})")
    print(f"  │  PnL=${t['alltime_pnl']:,.0f}  "
          f"WR-recent={t['win_rate_recent']:.1%}  WR-all={t['win_rate_all']:.1%}  "
          f"streak={ss}  T/day={t['trades_per_day']:.1f}  hold={t.get('hold_full', '?')}")
    pi, pm = t.get('pct_intraday'), t.get('pct_multiday')
    if pi is not None:
        verdict = ("SCALPER (not copy-able)" if pi >= 0.6
                   else "SWING (copy-able)" if pm and pm >= 0.5 else "mixed cadence")
        print(f"  │  cadence: intraday<1h={pi:.0%}  multiday>=24h={pm:.0%}  "
              f"open_pos={t.get('n_open_pos', 0)}  → {verdict}")

    # Weekly consistency
    wc  = dd.get("week_consistency", 0)
    pw  = dd.get("profitable_weeks", 0)
    tw  = dd.get("total_weeks_scanned", 0)
    mls = dd.get("max_loss_streak", 0)
    print(f"  │  Weekly consistency: {pw}/{tw} profitable weeks ({wc:.0%})  |  Max loss streak: {mls}")

    # Per-coin breakdown
    coins = dd.get("coin_breakdown", [])
    if coins:
        parts = [f"{c['coin']}({c['wr']:.0%} WR, {c['trades']}T, ${c['pnl']:+,.0f})" for c in coins]
        print(f"  │  Top coins: {' | '.join(parts)}")

    # Live positions
    live = dd.get("live_positions", [])
    if live:
        lp = [f"{p['side'].upper()} {p['coin']} ${p['notional']:,.0f} {p['leverage']}x (uPnL ${p['upnl']:+,.0f})" for p in live]
        print(f"  │  LIVE NOW ({len(live)}): {' · '.join(lp)}")
    else:
        print(f"  │  LIVE NOW: flat (no open positions)")

    # Verdict
    issues = []
    if t['avg_hold_h'] > 8:
        issues.append(f"hold={t['avg_hold_h']:.1f}h (long)")
    if mls >= 8:
        issues.append(f"max_loss_streak={mls} (high)")
    if wc < 0.6:
        issues.append(f"weekly consistency={wc:.0%} (inconsistent)")
    if t['current_streak'] < 0:
        issues.append(f"currently on loss streak ({ss})")

    if not issues:
        verdict = "✅ STRONG — recommend whitelist"
    elif len(issues) == 1:
        verdict = f"⚠️  CAUTION — {issues[0]}"
    else:
        verdict = f"❌ SKIP — {', '.join(issues)}"

    print(f"  └─ {verdict}")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    print("═" * 65)
    print("  HL-BOT TRADER SCANNER v3")
    print("  3-stage: leaderboard → fill history → deep dive")
    print("═" * 65)

    # ── Stage 1 ────────────────────────────────────────────────────────────────
    print("\nSTAGE 1: Fetching leaderboard…")
    async with aiohttp.ClientSession() as s:
        async with s.get(STATS_URL, timeout=aiohttp.ClientTimeout(total=60)) as r:
            data = await r.json(content_type=None)

    rows = data.get("leaderboardRows", []) if isinstance(data, dict) else data
    print(f"  Total traders on HL: {len(rows):,}")

    parsed = []
    for entry in rows:
        addr = entry.get("ethAddress", "")
        if not addr or len(addr) != 42:
            continue
        w = parse_windows(entry.get("windowPerformances", []))
        parsed.append({
            "address":     addr,
            "alltime_pnl": w.get("allTime", {}).get("pnl", 0),
            "week_pnl":    w.get("week",    {}).get("pnl", 0),
            "day_vlm":     w.get("day",     {}).get("vlm", 0),
            "alltime_vlm": w.get("allTime", {}).get("vlm", 0),
            "alltime_wr":  w.get("allTime", {}).get("wr", 0.5),
        })

    s1 = [
        t for t in parsed
        if t["alltime_pnl"] >= MIN_ALLTIME_PNL
        and t["week_pnl"]   >= MIN_WEEK_PNL
        and t["day_vlm"]    >= MIN_DAY_VLM
        and t["alltime_vlm"] >= MIN_ALLTIME_VLM
    ]
    s1.sort(key=lambda t: t["alltime_pnl"], reverse=True)
    candidates = s1[:STAGE1_LIMIT]
    print(f"  Stage 1 filter: {len(s1):,} qualify → scanning top {len(candidates)}")

    # ── Stage 2 ────────────────────────────────────────────────────────────────
    print(f"\nSTAGE 2: Fill history + streak analysis ({len(candidates)} traders)…")
    print("  (~2 min)\n")

    results   = []
    fill_cache: dict[str, list] = {}   # address → raw fills for Stage 3

    async with aiohttp.ClientSession() as session:
        for i in range(0, len(candidates), 15):
            batch = candidates[i:i+15]
            coros = [profile_trader(session, t["address"]) for t in batch]
            outs  = await asyncio.gather(*coros)

            for trader, (profile, fills) in zip(batch, outs):
                if profile is None:
                    continue
                merged = {**trader, **profile}
                merged["score"] = composite_score(merged)
                fill_cache[trader["address"]] = fills

                ok   = passes_stage2(profile)
                flag = "✅" if ok else "  "
                ss   = f"+{profile['current_streak']}" if profile['current_streak'] > 0 else str(profile['current_streak'])
                print(
                    f"  {flag} {trader['address'][:14]}… "
                    f"WR={profile['win_rate_recent']:.0%} "
                    f"T/d={profile['trades_per_day']:.1f} "
                    f"hold={profile['hold_str']} "
                    f"streak={ss} "
                    f"score={merged['score']:.4f}"
                )
                if ok:
                    results.append(merged)

            await asyncio.sleep(0.3)

    results.sort(key=lambda t: t["score"], reverse=True)

    # ── Stage 3 ────────────────────────────────────────────────────────────────
    stage3_targets = results[:STAGE2_TOP_N]

    print(f"\nSTAGE 3: Deep dive on top {len(stage3_targets)} traders…")
    print("  (per-coin WR, weekly consistency, max loss streak, live positions)\n")

    async with aiohttp.ClientSession() as session:
        for t in stage3_targets:
            fills = fill_cache.get(t["address"], [])
            dd    = await deep_dive(session, t["address"], fills)
            t["_deep"] = dd
            await asyncio.sleep(0.2)

    # ── Summary table ──────────────────────────────────────────────────────────
    top = results[:TOP_N]
    print_summary_table(top, f"🏆 TOP {len(top)} TRADERS — RANKED BY COPY-TRADING SCORE")

    # ── Deep cards for top 10 ─────────────────────────────────────────────────
    print(f"\n{'═'*65}")
    print(f"  STAGE 3 DETAIL — TOP {len(stage3_targets)}")
    print(f"{'═'*65}")
    for i, t in enumerate(stage3_targets, 1):
        print_deep_card(i, t)

    # ── Write output ───────────────────────────────────────────────────────────
    out = []
    for t in top:
        dd = t.get("_deep", {})
        out.append({
            "address":            t["address"],
            "alltime_pnl":        round(t["alltime_pnl"]),
            "win_rate_recent":    round(t["win_rate_recent"], 4),
            "win_rate_all":       round(t["win_rate_all"], 4),
            "trades_per_day":     round(t["trades_per_day"], 2),
            "avg_hold_h":         round(t["avg_hold_h"], 2),
            "med_hold_h":         round(t["med_hold_h"], 2) if t.get("med_hold_h") is not None else None,
            "med_open_h":         round(t["med_open_h"], 2) if t.get("med_open_h") is not None else None,
            "n_open_pos":         t.get("n_open_pos", 0),
            "pct_intraday":       round(t["pct_intraday"], 3) if t.get("pct_intraday") is not None else None,
            "pct_multiday":       round(t["pct_multiday"], 3) if t.get("pct_multiday") is not None else None,
            "current_streak":     t["current_streak"],
            "trades_7d":          t["trades_7d"],
            "last_trade_h":       round(t["last_trade_h"], 1),
            "avg_notional":       round(t.get("avg_notional", 0)),
            "week_consistency":   dd.get("week_consistency", 0),
            "max_loss_streak":    dd.get("max_loss_streak", 0),
            "live_position_count": dd.get("live_position_count", 0),
            "top_coins":          [c["coin"] for c in dd.get("coin_breakdown", [])[:3]],
            "score":              round(t["score"], 5),
        })

    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(out, f, indent=2)

    print(f"\n\n✅ Wrote {len(out)} traders to {OUTPUT_FILE}")

    # ── Final whitelist recommendation ─────────────────────────────────────────
    # Only recommend traders with ✅ STRONG verdict
    def is_strong(t: dict) -> bool:
        dd  = t.get("_deep", {})
        mls = dd.get("max_loss_streak", 99)
        wc  = dd.get("week_consistency", 0)
        return (
            t["avg_hold_h"]      <  8
            and mls              <  8
            and wc               >= 0.6
            and t["current_streak"] >= 0
        )

    strong = [t for t in stage3_targets if is_strong(t)]
    recommend = strong[:5] if strong else stage3_targets[:5]

    print(f"\n{'═'*65}")
    print("  📋 WHITELIST RECOMMENDATION")
    print(f"{'═'*65}")
    for t in recommend:
        dd = t.get("_deep", {})
        ss = f"+{t['current_streak']}" if t['current_streak'] >= 0 else str(t['current_streak'])
        coins_str = ", ".join(c["coin"] for c in dd.get("coin_breakdown", [])[:3])
        print(
            f"  {t['address']}\n"
            f"    WR={t['win_rate_recent']:.0%} | T/day={t['trades_per_day']:.1f} | "
            f"hold={t['avg_hold_h']:.1f}h | streak={ss} | "
            f"weeks={dd.get('profitable_weeks',0)}/{dd.get('total_weeks_scanned',0)} | "
            f"max_loss={dd.get('max_loss_streak',0)} | coins={coins_str}"
        )

    whitelist = ",".join(t["address"] for t in recommend)
    print(f"\n  Add to /root/hl-bot/.env:")
    print(f"  COPY_TRADER_WHITELIST={whitelist}")
    print(f"  STRATEGY_LEADERBOARD_COPY=true")
    print(f"\n  Then: sudo systemctl restart hl-bot")


if __name__ == "__main__":
    asyncio.run(main())
