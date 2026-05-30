# VPS migration plan: Nuremberg → AWS us-east-1

**Why:** `api.hyperliquid.xyz` is fronted by AWS CloudFront (we resolve to a Frankfurt
edge IP, `18.245.60.x`, `fra60.r.cloudfront.net`). The TLS connect is fast (~5-10ms)
because we hit the CDN locally, but the full POST roundtrip takes **240-270ms** — the
hidden cost is CloudFront → HL's origin server, which is almost certainly in
us-east-1. Moving the VPS there should drop total order-RTT by **~100-200ms**.

This is the single biggest latency win available, and it's blocking for any
HF-cadence sleeve (scalp / ultra-scalp tiers in v2.3 spec). The bbf82c80 scalp
sleeve currently paper-runs from Nuremberg; flipping to LIVE without this
migration ships ~250-300ms of total tap-to-fire latency, which the math says is
on the wrong side of his per-trip edge floor.

---

## Pre-migration checks (do these first to confirm assumptions)

| Check | Method | Pass criteria |
|---|---|---|
| Confirm HL origin is in us-east-1 | Spin up a $5 lightsail in us-east-1, run the same POST loop, measure RTT | us-east-1 RTT < 50ms (vs Nuremberg ~250ms) |
| Confirm WS push latency improvement | Same lightsail, subscribe to allMids + measure inter-tick gap | Should match Nuremberg (CDN is global) |
| Confirm no IP-restriction on HL agent | The new agent address `0x000D11A3F5...` is wallet-bound, not IP-bound | Test: send a probe order from lightsail signed with the agent key |

If all 3 pass → proceed. If origin RTT from us-east-1 is also slow, HL is somewhere
else (Tokyo? Singapore?) and we'd need to probe other regions before migrating.

## Migration runbook

### Phase 1 — provision (no downtime, parallel to current VPS)
1. AWS us-east-1, t3.small EC2 (Ubuntu 24.04). Same shape as current Nuremberg.
2. Clone `git@github.com:Baf-io/hl-bot.git`
3. Install Python 3.14, venv, deps from `requirements.txt`
4. Install systemd units (copy from current VPS `/etc/systemd/system/hl-*.service`)
5. **Do NOT copy `.env` yet** — keys are at REST and don't cross machines

### Phase 2 — credential migration (controlled rotation, 5min downtime)
1. On Nuremberg VPS: `sudo systemctl stop hl-77998579-sleeve hl-bot hl-pos-watch hl-flip-alerts hl-shadow-scan hl-bbf82c80-scalp-sleeve hl-source-health hl-candle-log hl-btc-alert hl-tracker-scan`
2. On HL frontend (user): create a NEW API agent address (paranoid: don't reuse the one tied to Nuremberg's history)
3. User saves new agent private key to password manager + pastes ONLY the public address back
4. User edits `.env` on us-east-1 box directly (paste the new key there, never on Nuremberg)
5. On HL frontend (user): approve new agent on MAIN + Sub Baf + SwingDyn
6. On us-east-1: `systemctl start` all services in same order they ran on Nuremberg
7. Verify each service comes up clean via `journalctl -u <name> -f`
8. On Nuremberg: leave services stopped. Wait 24h to confirm us-east-1 stable. Then revoke old agent in HL frontend + destroy Nuremberg VPS.

### Phase 3 — measure improvement
1. Re-run the same `curl POST /info` probe loop from us-east-1
2. Compare RTT: expect ~30-80ms vs Nuremberg's 240-270ms
3. Restart bbf82c80 scalp sleeve and let it run in PAPER for 1h to verify clean
   operation in new region
4. The `_latency_summary_loop` in `watch_scalp_sleeve.py` will now show the actual
   improvement on his real fills

### Phase 4 — flip scalp sleeve to LIVE
Only after Phase 3 confirms ~50-100ms detection latency to his fills (was ~150-200ms
from Nuremberg). Flip `SCALP_LIVE=true` in systemd unit, restart.

---

## Rollback plan

If anything looks wrong in Phase 2-3:
1. On HL frontend: revoke us-east-1 agent (un-approves it on all accounts)
2. Re-approve the OLD Nuremberg agent (if still valid) or create a new one
3. Edit Nuremberg `.env`, restart services
4. Wait for clean operation, then troubleshoot us-east-1

The old VPS stays untouched for 24h post-migration as the rollback target.

---

## Cost note

t3.small in us-east-1: ~$15/month. Same shape as current Nuremberg.
Total monthly cost change: ~zero (we're swapping providers, not adding).

---

## Estimated effort

| Phase | Time | Blocker |
|---|---|---|
| Pre-migration checks | 1h | needs lightsail or temp EC2 |
| Phase 1 provision | 2h | AWS account + initial setup |
| Phase 2 credential rotation | 30min | needs user at HL frontend |
| Phase 3 measure | 1h | runtime observation |
| Phase 4 flip live | 5min | gated on Phase 3 results |

**Total: ~4-5h elapsed, ~1h of user attention** (mostly for the HL frontend agent rotation).

---

## What NOT to migrate

- `data/research2/*.jsonl` — local research data, can stay on Nuremberg until decom
- LXC scanner box — separate machine, separate IP, stays where it is (it's read-only,
  doesn't need to be near HL origin)

The migration is just the VPS that runs the trading services.
