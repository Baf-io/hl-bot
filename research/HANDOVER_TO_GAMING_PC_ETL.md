# Handover: heavy ETL on gaming PC → me (hl-bot trading agent)

You're the LXC Claude. The user wired up a gaming PC with serious compute + Tailscale
+ shared OneDrive but **direct VPS→gaming-PC SSH failed** during setup (Windows OpenSSH
strict-perms hell). New plan: **you orchestrate the gaming PC**, results flow back to
me through paths that already work (HMAC intake POST for real-time, git for batch).

User explicitly does NOT want to type any more Windows commands. Get the gaming PC
running headless via whatever access you already have (OneDrive shared folder + a
Python script the user double-clicks once, RDP via Tailscale, scheduled task — pick
what's least painful for them).

---

## The three deliverables (in priority order)

### 1. Cross-venue trader entity graph
**Purpose:** stop counting the same operator's wallet 3× across HL + Drift + Jupiter.
Output a stable `entity_id → [aliases]` map; I dedup against it everywhere a roster of
addresses is iterated (`hl-flip-alerts`, `hl-source-health`, `COPYABLE_DB`).

**Output:** `research/entity_clusters.jsonl`, one entity per line:

```json
{
  "entity_id": "ent_<8-char hash>",
  "primary_addr": "0x77998579f578c01030db65e75edc47bfe890c291",
  "aliases": [
    {"chain": "evm", "addr": "0x..."},
    {"chain": "sol", "addr": "abc...XYZ"}
  ],
  "confidence": 0.92,
  "evidence": ["same first-funder 0x4976a4a0", "co-active hours 73%", "common counterparty 0x123..."],
  "primary_venue": "hyperliquid",
  "first_seen_ts": 1779999999,
  "updated_ts": 1780000099
}
```

**Refresh cadence:** weekly. Commit to git when changed. I git-pull and consume.

**Confidence rules:** ≥0.8 = treat as one entity. 0.5-0.8 = surface for review, don't auto-dedup. <0.5 = drop.

### 2. Whale-flow real-time signal
**Purpose:** when a known-whale wallet moves big size in a direction (CEX deposit, big DEX swap, vault deposit), emit a directional signal BEFORE it shows up in HL.

**Transport:** HMAC POST to VPS intake. Use the existing `HLBOT_SHARED_SECRET` env. Endpoint: `http://100.115.113.91:8787/intake`. Schema (note the new fields):

```json
{
  "signal_id": "whaleflow_<uuid>",
  "ts_emitted": 1780000099,
  "source": "whaleflow",
  "source_type": "whaleflow",            // NEW — tells VPS this isn't a copy signal
  "coin": "BTC",
  "direction": "short",
  "mode": "probe",                       // stays probe until brain promotes to KEEP
  "size_notional": 50,                   // VPS ignores this and uses its 1%-risk sizing
  "entry": 73500,                        // suggested entry (VPS may discretion)
  "stop": 75200,
  "hold_seconds": 14400,                 // 4h TTL — whale signals decay fast
  "evidence": {
    "whale": "0xABC...",
    "action": "deposit_to_binance",
    "amount_usd": 50000000,
    "confidence": 0.78
  }
}
```

VPS-side I need to add `"whaleflow"` to the recognized source-types so the intake doesn't reject as `not KEEP-validated`. I'll do that on first-receive.

**Rate budget:** ≤10 signals/day. If you're emitting more, you're scraping noise. Tighten the whale-watchlist.

### 3. Cross-venue funding divergence
**Purpose:** when Binance/Bybit/OKX funding diverges from HL by ≥30bps/8h, that's a leading indicator HL is about to move. Emit a directional ntfy to the user (don't auto-trade — discretionary).

**Transport:** also HMAC POST to intake, with `source_type: "funding_div"` and `mode: "info_only"`. The VPS-side change I'll add: `info_only` mode logs + ntfys but doesn't execute. Lets us see signal quality before risking capital.

**Schema:**
```json
{
  "signal_id": "fundingdiv_<coin>_<unix_min>",
  "ts_emitted": 1780000099,
  "source": "fundingdiv",
  "source_type": "funding_div",
  "mode": "info_only",
  "coin": "BTC",
  "direction": "short",                  // direction the cross-venue cohort implies
  "evidence": {
    "hl_8h": 0.0001,
    "binance_8h": 0.0034,
    "bybit_8h": 0.0029,
    "okx_8h": 0.0031,
    "spread_bps": 33,
    "venues_aligned": 3
  }
}
```

**Cadence:** poll cross-venue funding every 5 min. Only emit when:
- Spread ≥ 30 bps
- ≥2 venues agree on direction
- Last signal for this coin was ≥30 min ago (anti-spam)

---

## HARD RULES (don't make me write these again)

1. **No secrets in transcripts.** If you echo or print `HLBOT_SHARED_SECRET`, `HELIUS_API_KEY`, etc., the user has to rotate again. They've already done it once today. See VPS `memory/never-display-secrets.md`.
2. **Idempotency on `signal_id`.** Intake dedups by it. Re-sends of the same signal are free; double-execution is not.
3. **HMAC over the RAW body bytes.** Use `hashlib.sha256` with the shared secret. Same pattern your brain pipeline already uses for tweet signals — just a different `source_type`.
4. **No Windows hand-holding for the user.** If you need to script anything on the gaming PC, write it once, drop it in OneDrive, have them double-click it, done.
5. **Heartbeat every 10 min** to `http://100.115.113.91:8787/heartbeat` (I'll add this endpoint on first-receive): `{"box": "gaming-pc", "pipeline": "whaleflow", "last_run_ts": ..., "last_success_ts": ..., "queue_depth": ...}`. Lets me see in `journalctl -u hl-bot` whether the pipeline's alive.

## WHAT I'LL ADD ON VPS SIDE

On first signal from any of these three pipelines I'll commit:

1. `src/execution/intake.py` — accept `source_type` in `("whaleflow", "funding_div")` without requiring KEEP graduation. Probe-sized at ≤$50 for whaleflow; `info_only` mode for funding_div (no execution, just log+ntfy).
2. `/heartbeat` endpoint on the intake server, in-memory state dict, exposed at `/heartbeat-status` for me to view via `curl`.
3. Entity-graph dedup in `hl-flip-alerts` and `hl-source-health` — when iterating sources, group by `entity_id`.

## WHEN YOU'RE READY

Just start sending signals to intake. The first POST tells me you're up. I'll see HMAC validation in `journalctl -u hl-bot | grep Intake` and respond if anything's off (signature mismatch, schema invalid). After 3-5 successful round-trips on each pipeline, we're production.
