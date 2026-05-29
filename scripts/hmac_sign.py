#!/usr/bin/env python3
"""
hmac_sign.py — sign a JSON body with HLBOT_SHARED_SECRET without ever displaying the secret.

Reads the secret via `config.settings` (which loads via python-dotenv), computes the HMAC-SHA256
over the RAW body bytes, and prints ONLY the hex signature on stdout. The secret value never
touches stdout/stderr.

Usage:
  echo '{"signal_id":"x"}' | python scripts/hmac_sign.py
    → prints `<64-hex>` on stdout

  python scripts/hmac_sign.py --probe-intake
    → fires a synthetic info_only funding_div signal at the local intake, prints accept/reject

  python scripts/hmac_sign.py --probe-heartbeat box=self-test pipeline=smoke
    → POSTs a synthetic heartbeat, prints accept/reject
"""
import sys, os, json, hmac, hashlib, time
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
from config import settings

def sign_bytes(b: bytes) -> str:
    secret = settings.HLBOT_SHARED_SECRET
    if not secret:
        print("ERR: HLBOT_SHARED_SECRET not loaded by settings", file=sys.stderr)
        sys.exit(2)
    return hmac.new(secret.encode(), b, hashlib.sha256).hexdigest()

def _intake_url(path: str) -> str:
    return f"http://{settings.INTAKE_HOST}:{settings.INTAKE_PORT}{path}"

def _probe_intake():
    import urllib.request
    sid = f"probe_{int(time.time())}"
    body_dict = {
        "signal_id": sid,
        "ts_emitted": int(time.time()),
        "source": "hmac-probe",
        "source_type": "funding_div",
        "mode": "info_only",
        "coin": "BTC",
        "direction": "short",
        "evidence": {"smoke": True, "spread_bps": 1},
    }
    body = json.dumps(body_dict).encode()
    sig = sign_bytes(body)
    req = urllib.request.Request(_intake_url("/intake"), data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Signature": sig, "X-Signal-Id": sid})
    with urllib.request.urlopen(req, timeout=8) as r:
        print(r.read().decode())

def _probe_heartbeat(kvs):
    import urllib.request
    payload = {k: v for k, v in (s.split("=", 1) for s in kvs if "=" in s)}
    payload.setdefault("box", "self-test"); payload.setdefault("pipeline", "smoke")
    payload["last_run_ts"] = int(time.time()); payload["last_success_ts"] = int(time.time())
    body = json.dumps(payload).encode()
    req = urllib.request.Request(_intake_url("/heartbeat"), data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        print(r.read().decode())

def main():
    args = sys.argv[1:]
    if args and args[0] == "--probe-intake":
        _probe_intake(); return
    if args and args[0] == "--probe-heartbeat":
        _probe_heartbeat(args[1:]); return
    body = sys.stdin.buffer.read()
    print(sign_bytes(body))

if __name__ == "__main__":
    main()
