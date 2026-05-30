# Handover — Ashburn SSH unblock + sleeve rebalance

**Date:** 2026-05-30
**For:** Claude running on user's main Windows device (the one with `/securite-transport`).
**From:** Claude running on Nuremberg Hetzner (`hl-bot` host, `/root/hl-bot`).

---

## What you need to do (TL;DR)

1. Get the user back into SSH on Ashburn (`5.161.252.215`).
2. Run a 2-sleeve rebalance deploy.
3. Add Nuremberg's pubkey to Ashburn's `authorized_keys` so Nuremberg-side Claude can take over future deploys.
4. (Optional) Re-sever the cross-box SSH after the deploy completes.

---

## Context — what's happening and why

The user runs Hyperliquid perp trading sleeves on a two-box setup:

| Box | Purpose | Status |
|---|---|---|
| **Ashburn Hetzner Cloud** `5.161.252.215` | LIVE trading sleeves | SSH is broken, needs unblock |
| **Nuremberg Hetzner Cloud** | Research, scanning, intake | Healthy, has the repo, can't reach Ashburn |

Live sleeves currently on Ashburn:
- `hl-bbf82c80-scalp-sleeve` — HYPE ultra-scalp, mirrors source `0xbbf82c80…` on Sub SwingDyn
- `hl-77998579-sleeve` — BTC+ETH swing on Sub Baf (currently broken because Sub Baf = $0)
- `hl-f2704e08-sleeve` — BTC swing on AkkaVault (just deployed; may need restart for new equity)
- `hl-pos-watch`, `hl-flip-alerts` — supporting alerts

**Earlier this session** we severed Nuremberg→Ashburn SSH for security (one-direction-of-compromise). Now the user wants Claude back in the deploy loop so we've been trying to re-add Nuremberg's pubkey. Every path we tried hit a wall:

1. **SSH from Windows with password** — Hetzner password reset didn't propagate without reboot. After reboot, host key changed → strict-check blocked SSH.
2. **Hetzner web console login** — same propagation issue, login refused.
3. **Hetzner Rescue System** — user got in successfully, ran a script to add Nuremberg's pubkey to the mounted disk (saw `OK`), rebooted. But subsequent SSH from Nuremberg still gets `Permission denied (publickey,password)`. The script may have written to the wrong partition, or rescue mounted a non-OS filesystem.

User then said: "ye make handover for the agent on my device. he will do it"

So: **you have `/securite-transport` available on the user's main Windows device** — use it (or whatever SSH-bootstrap tooling that skill provides) to re-establish access to Ashburn.

---

## Current real-world balances (as of 2026-05-30, all checked against HL `/info`)

| Sub | Address | Equity |
|---|---|---|
| Sub Baf | `0xdac952c205f60a3aab2e72fc4ec27e69a9c92246` | **$0** (drained) |
| Sub SwingDyn | `0x57e8b2f2627e13a5f0e090913f5ed6d507bea673` | **$151** |
| AkkaVault | `0xb3df35c5fc6e10508e449e3f508749a0a46054a9` | **$251** |
| MAIN | (user-owned, not in repo) | low — couldn't spare $100 for Sub Baf |

Total deployed: **$402**. Target was $500 with $100 locked on Sub Baf — but MAIN couldn't fund the Sub Baf leg, so for now Sub Baf stays $0 and `hl-77998579-sleeve` stays offline. ETH coverage gap remains until Sub Baf is funded.

---

## Step 1 — Get the user back into Ashburn SSH

In rough order of preference:

### Path A: Use `/securite-transport` skill

This is presumably the cleanest path since the user explicitly pointed at it. Run that skill on the Windows machine; it should handle SSH key install / password recovery / rescue automation for them.

### Path B: Manual fix on Windows side

If the skill doesn't directly do it, the manual SSH-unblock recipe:

```powershell
# Clear stale host key from PowerShell's known_hosts
ssh-keygen -R 5.161.252.215

# Try SSH — if the user's laptop key is still in Ashburn's authorized_keys,
# this should connect without password.
ssh root@5.161.252.215
```

If that fails (publickey rejected), they need password — Hetzner Cloud panel → server → **Rescue & Power → Reset root password → REBOOT the server** (password is applied via cloud-init at boot, not live). Then SSH with the new password.

### Path C: Rescue mode redux (if Paths A and B fail)

The user already went through rescue once. To retry:

1. Hetzner panel → `5.161.252.215` → **Rescue** → enable Linux 64-bit, set rescue password
2. **Reboot the server** (rescue takes effect at next boot)
3. SSH `root@5.161.252.215` from PowerShell using the rescue password (note: rescue uses fresh host keys, so `ssh-keygen -R 5.161.252.215` again first)
4. Once at `root@rescue ~#`, run the **VERBOSE** key-add script below (the previous version may have written to the wrong partition):

```bash
# Verbose key add — explicitly shows which partition was mounted and prints
# the resulting authorized_keys content so you can verify before reboot.
mkdir -p /mnt/target
ROOTPART=""
echo "=== Disk layout ==="
lsblk -o NAME,SIZE,FSTYPE,LABEL /dev/sda

echo ""
echo "=== Searching for OS root partition ==="
for P in $(lsblk -lnpo NAME,TYPE /dev/sda | awk '$2=="part"{print $1}'); do
    mount -o ro "$P" /mnt/target 2>/dev/null || { echo "$P: mount failed (skip)"; continue; }
    if [ -d /mnt/target/root ] && [ -d /mnt/target/etc ] && [ -f /mnt/target/etc/os-release ]; then
        echo "$P: looks like OS root (has /root, /etc, /etc/os-release)"
        echo "  os-release first line: $(head -1 /mnt/target/etc/os-release)"
        ROOTPART="$P"
        umount /mnt/target
        break
    else
        echo "$P: not OS root (missing /root or /etc/os-release)"
        umount /mnt/target
    fi
done

if [ -z "$ROOTPART" ]; then
    echo "FAIL: no OS root partition found. Show 'lsblk' output and STOP."
    exit 1
fi

echo ""
echo "=== Mounting $ROOTPART read-write and adding key ==="
mount "$ROOTPART" /mnt/target

# Add Nuremberg pubkey
mkdir -p /mnt/target/root/.ssh
chmod 700 /mnt/target/root/.ssh
NEW_KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIO463MhyqJZVExft5aRfuhTO/vhYeDVaGYrfIIoEB4RD hl-bot-nuremberg→ashburn'

# Idempotent append — don't add twice
if ! grep -qF "AAAAC3NzaC1lZDI1NTE5AAAAIO463MhyqJZVExft5aRfuhTO/vhYeDVaGYrfIIoEB4RD" /mnt/target/root/.ssh/authorized_keys 2>/dev/null; then
    echo "$NEW_KEY" >> /mnt/target/root/.ssh/authorized_keys
fi
chmod 600 /mnt/target/root/.ssh/authorized_keys

echo ""
echo "=== Resulting /mnt/target/root/.ssh/authorized_keys ==="
cat /mnt/target/root/.ssh/authorized_keys
echo ""
echo "=== Permissions ==="
ls -la /mnt/target/root/.ssh/

sync
umount /mnt/target
echo ""
echo "===== SUCCESS — key written, safe to disable rescue and reboot ====="
```

After running it, **verify you see the Nuremberg pubkey line in the printed authorized_keys**. Then Hetzner panel → Rescue tab → disable rescue → reboot.

Once normal OS is back, verify Nuremberg can reach Ashburn:

```bash
# From Nuremberg side (this is what Nuremberg-Claude will test)
ssh -i /root/.ssh/hl-migrate-ashburn -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@5.161.252.215 'hostname && uptime'
```

Expect: `ubuntu-8gb-ash-2` + uptime line. If still `Permission denied`, key wasn't written to the correct partition — re-enter rescue and pay attention to which partition the script identified.

---

## Step 2 — Run the 2-sleeve rebalance deploy

Once SSH is unblocked, the deploy is a single paste-block. **Target state:**

| Sleeve | Sub | Equity | Status after deploy |
|---|---|---|---|
| `hl-bbf82c80-scalp-sleeve` | SwingDyn | $151 | rescaled budget $200→$150 |
| `hl-f2704e08-sleeve` | AkkaVault | $251 | restarted to pick up new equity |
| `hl-77998579-sleeve` | Sub Baf | $0 | left stopped, no rescale yet |

Paste this on Ashburn (in user's SSH session, or via Nuremberg-Claude after key is added):

```bash
# 1) Rescale bbf82c80 scalp sleeve from $200 → $150 equity
sudo systemctl stop hl-bbf82c80-scalp-sleeve

sudo sed -i \
  -e 's/^Environment=SCALP_TOTAL_BUDGET=.*/Environment=SCALP_TOTAL_BUDGET=150/' \
  -e 's/^Environment=SCALP_LEG_SIZE=.*/Environment=SCALP_LEG_SIZE=150/' \
  -e 's/^Environment=SCALP_DAILY_HALT=.*/Environment=SCALP_DAILY_HALT=10/' \
  /etc/systemd/system/hl-bbf82c80-scalp-sleeve.service

# Verify
grep '^Environment=SCALP_' /etc/systemd/system/hl-bbf82c80-scalp-sleeve.service

# 2) Reload + start
sudo systemctl daemon-reload
sudo systemctl start hl-bbf82c80-scalp-sleeve

# 3) Make sure f2704e08 sleeve unit exists. If not yet on the box, it needs
#    to be created first — see "f2704e08 unit content" appendix below.
if [ ! -f /etc/systemd/system/hl-f2704e08-sleeve.service ]; then
    echo "MISSING: hl-f2704e08-sleeve.service — install it from the appendix below first"
else
    sudo systemctl restart hl-f2704e08-sleeve
fi

# 4) Verify
echo "=== Service status ==="
sudo systemctl is-active hl-bbf82c80-scalp-sleeve hl-f2704e08-sleeve

echo ""
echo "=== Recent journal (last 15 lines each) ==="
sudo journalctl -u hl-bbf82c80-scalp-sleeve -n 15 --no-pager
echo "---"
sudo journalctl -u hl-f2704e08-sleeve -n 15 --no-pager
```

### Appendix: `hl-f2704e08-sleeve.service` unit content (only if file is missing)

```ini
[Unit]
Description=0xf2704e08 BTC swing sleeve (asymmetric payoff trader) on AkkaVault
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/hl-bot
Environment=PYTHONPATH=/root/hl-bot/src:/root/hl-bot
Environment=SLEEVE_NAME=f2704e08
Environment=SLEEVE_SOURCE=0xf2704e08a4d989f76171c9389665e77c870345a7
Environment=SLEEVE_SUB=0xb3df35c5fc6e10508e449e3f508749a0a46054a9
Environment=SLEEVE_COINS=BTC
Environment=SLEEVE_MARGIN_PCT=0.40
Environment=SLEEVE_LEV=3
Environment=SLEEVE_MAX_CONCURRENT=1
Environment=SLEEVE_ADOPT_BAND_PCT=0
Environment=SLEEVE_STOP_PCT=0.10
Environment=SLEEVE_HALT_USD=15
Environment=SLEEVE_POLL_S=20
ExecStart=/root/hl-bot/.venv/bin/python -u scripts/watch_swing_sleeve.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Write to `/etc/systemd/system/hl-f2704e08-sleeve.service`, then `sudo systemctl daemon-reload && sudo systemctl enable --now hl-f2704e08-sleeve`.

**Pre-flight for f2704e08:** the `bot-ashburn` HL agent needs to be approved on AkkaVault (`0xb3df35c5…`) on the HL frontend, else first order rejects. User said they did this earlier — confirm by checking journal: a `leverage set: BTC 3x iso → ok` log line confirms agent is approved.

---

## Step 3 — What to expect in the journals (sanity check)

### `hl-bbf82c80-scalp-sleeve` clean restart:
```
budget=$150.0 leg=$150.0 lev=5x sl=1.5% time_stop=900s daily_halt=$10.0 cooldown=60s LIVE=True
leverage set: HYPE 5x iso → ok
WS subscribed: allMids + userFills:src + userFills:sub + trades:HYPE
```
Then silent until bbf82c80 fires. He averages ~1.7 HYPE entries/day, mostly UTC 22:00–02:00.

### `hl-f2704e08-sleeve` clean restart:
```
[f2704e08] start | src=0xf2704e08… sub=0xb3df35c5… coins=['BTC']
leverage set: BTC 3x iso → ok
baseline: BTC <his current szi>   # he's holding BTC long $100k @40x — NOT mirrored (rule #3)
```
Then idle until he goes flat on BTC and reopens. Could be hours.

---

## Step 4 — Notes for Nuremberg-Claude after SSH works

Once cross-box SSH is back, ping Nuremberg-Claude (or have the user say "Claude on Nuremberg, take over"). Nuremberg has:
- The repo at `/root/hl-bot` (master branch, latest commits)
- The full conversation context in this session
- A background "scalp mirror" watcher running at pid 253194 logging to `/tmp/scalp_mirror.log` that cross-validates bbf82c80 mirroring from this box

Nuremberg-Claude will:
1. Verify SSH works (`ssh -i /root/.ssh/hl-migrate-ashburn root@5.161.252.215 'hostname && uptime'`)
2. Confirm both sleeves are active via remote `systemctl is-active`
3. Tail journals briefly to confirm rulebooks fired correctly
4. Update CLAUDE.md to reflect the new 2-sleeve state (Sub Baf $0 / SwingDyn $151 / AkkaVault $251)
5. (Optional) Add ntfy alert if either sleeve dies

---

## Step 5 — After deploy completes (security hygiene)

Once everything is running and verified, the user may want to re-sever Nuremberg→Ashburn SSH for blast-radius isolation. To do this:

```bash
# On Ashburn — remove the Nuremberg pubkey line
sudo sed -i '/hl-bot-nuremberg.ashburn/d' /root/.ssh/authorized_keys

# Verify only the user's main-device key remains
sudo cat /root/.ssh/authorized_keys
```

This is optional — the user knows their threat model. The migration runbook had us severing it post-cutover, so the pattern is to re-sever after each maintenance window.

---

## Things you should NOT do

- **Don't touch MAIN.** Per CLAUDE.md rule #1, bot strategies never touch MAIN. MAIN is user-discretion-only. Trades on MAIN are user's BTC short + funding-carry positions.
- **Don't adopt held positions.** Sleeves baseline-seed (record but don't mirror) any open positions at startup. Wait for the source to go flat then re-enter before we fire.
- **Don't push to git remote without explicit user OK.** Local commits are fine; pushing isn't.
- **Don't read or display `.env` or any secrets file.** Hard rule per memory `never-display-secrets.md`. Verify env values by behavior (journal logs), not by reading `.env`.

---

## Open questions to ask user (if you need to)

1. **Sub Baf funding** — when will MAIN have ~$100 to spare for re-lighting the lead77 sleeve? (ETH gap stays open meanwhile.)
2. **Re-sever SSH after deploy?** — keep cross-box SSH alive for ongoing convenience, or strip it after each maintenance?
3. **Anything else broke during the access ordeal?** — e.g., if rescue mode disturbed any disk state, watch for boot warnings in journal.

---

Good luck. Repo state is committed up to `e24bd6e` (the 2-candidate shadow expansion + 7d backfill). Nothing in flight that the Windows side needs to git-pull urgently.
