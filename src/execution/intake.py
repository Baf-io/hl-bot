"""
Brain intake (B4) — HMAC-signed signal receiver.

The LXC "news-brain" is authoritative on STRATEGY; this box is authoritative on
EXECUTION + RISK ENFORCEMENT. Trust nothing — re-verify everything:

  POST /intake  (Content-Type: application/json)
    headers: X-Signal-Id (idempotency), X-Signature (hex HMAC-SHA256 over RAW body)
  → verify HMAC over the raw bytes (constant-time), dedupe on X-Signal-Id, reject
    malformed/unsigned/stale (>STALE_SIGNAL_S), then INDEPENDENTLY re-enforce the
    risk-policy cardinals, then (unless INTAKE_ACK_ONLY) execute + own the lifecycle.
  ← {"accepted":bool, "reason":str, "order_ref":str|null}

ACK-ONLY mode (handshake): verify + policy-check + LOG the decision, place NO order.
Enable real execution only after the handshake passes.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections import deque

from aiohttp import web
from loguru import logger

from config import settings
from data.store import TradeSignal


class IntakeServer:
    def __init__(self, executor, risk, alerter=None):
        self.executor = executor
        self.risk = risk
        self.alerter = alerter
        self._seen: set[str] = set()          # idempotency keys (X-Signal-Id)
        self._seen_order: deque[str] = deque(maxlen=5000)  # bound memory

    # ── auth ──────────────────────────────────────────────────────────────────
    def _verify(self, raw: bytes, sig: str) -> bool:
        secret = settings.HLBOT_SHARED_SECRET
        if not secret or not sig:
            return False
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

    def _mark_seen(self, sid: str):
        if len(self._seen_order) == self._seen_order.maxlen:
            self._seen.discard(self._seen_order[0])
        self._seen_order.append(sid)
        self._seen.add(sid)

    # ── independent cardinal re-enforcement (reject if violated) ──────────────
    def _validate(self, p: dict) -> tuple[bool, str]:
        req = ["signal_id", "coin", "direction", "size_notional", "entry", "stop",
               "hold_seconds", "mode", "source", "ts_emitted"]
        for k in req:
            if k not in p:
                return False, f"missing field {k}"
        d = p["direction"]
        if d not in ("long", "short"):
            return False, "bad direction"
        if p["mode"] not in ("probe", "live"):
            return False, "bad mode"
        try:
            entry = float(p["entry"]); stop = float(p["stop"]); sz = float(p["size_notional"])
        except (TypeError, ValueError):
            return False, "non-numeric entry/stop/size"
        if time.time() - float(p["ts_emitted"]) > settings.STALE_SIGNAL_S:
            return False, "stale ts_emitted"
        # stop present + on the correct side of entry
        if entry <= 0 or stop <= 0:
            return False, "entry/stop must be > 0"
        if d == "long" and not stop < entry:
            return False, "long: stop must be below entry"
        if d == "short" and not stop > entry:
            return False, "short: stop must be above entry"
        eq = self.risk.portfolio_value
        if eq <= 0:
            return False, "no equity"
        # implied leverage ≤ 3x
        if sz / eq > settings.MAX_LEVERAGE:
            return False, f"implied leverage {sz/eq:.2f}x > {settings.MAX_LEVERAGE}x"
        # mode caps
        risk_usd = sz * abs(entry - stop) / entry
        if p["mode"] == "probe":
            if sz > settings.PROBE_MAX_NOTIONAL:
                return False, f"probe notional ${sz:.0f} > ${settings.PROBE_MAX_NOTIONAL}"
            if risk_usd > settings.PROBE_MAX_RISK_USD:
                return False, f"probe risk ${risk_usd:.2f} > ${settings.PROBE_MAX_RISK_USD}"
        else:  # live
            if p["source"] not in settings.KEEP_SOURCES:
                return False, f"live but source '{p['source']}' not KEEP-validated"
        # max 1 open position
        if len(self.risk.open_positions) >= settings.MAX_OPEN_POSITIONS:
            return False, f"max {settings.MAX_OPEN_POSITIONS} open position(s) reached"
        # no averaging into an existing coin
        if any(x.coin == p["coin"] for x in self.risk.open_positions):
            return False, f"already hold {p['coin']} (no averaging)"
        # kill-switches
        if getattr(self.risk, "_trading_halted", False):
            return False, "HALTED (daily loss)"
        if self.risk._weekly_pnl / eq <= -settings.WEEKLY_LOSS_HALT_PCT:
            return False, "HALTED (weekly loss)"
        return True, "ok"

    # ── HTTP handler ──────────────────────────────────────────────────────────
    async def intake(self, request: web.Request) -> web.Response:
        raw = await request.read()
        sid = request.headers.get("X-Signal-Id", "")
        sig = request.headers.get("X-Signature", "")
        if not self._verify(raw, sig):
            logger.warning(f"[Intake] ⛔ REJECT bad/absent HMAC (sid={sid[:16]})")
            return web.json_response({"accepted": False, "reason": "bad signature", "order_ref": None}, status=401)
        if not sid:
            return web.json_response({"accepted": False, "reason": "missing X-Signal-Id", "order_ref": None}, status=400)
        if sid in self._seen:
            return web.json_response({"accepted": False, "reason": "duplicate (idempotent)", "order_ref": None})
        try:
            p = json.loads(raw)
        except Exception:
            return web.json_response({"accepted": False, "reason": "bad json", "order_ref": None}, status=400)
        self._mark_seen(sid)   # signature + parse OK → consume the id (retries dedupe)
        ok, reason = self._validate(p)
        if not ok:
            logger.warning(f"[Intake] ⛔ REJECT sid={sid[:16]} {p.get('coin')} {p.get('mode')}: {reason}")
            return web.json_response({"accepted": False, "reason": reason, "order_ref": None})
        if settings.INTAKE_ACK_ONLY:
            logger.success(
                f"[Intake] ✅ ACK-ONLY (handshake, NO order) sid={sid[:16]} "
                f"{p['mode']} {p['direction']} {p['coin']} ${p['size_notional']:.0f} "
                f"entry {p['entry']} stop {p['stop']} ttl {p['hold_seconds']}s src={p['source']}"
            )
            return web.json_response({"accepted": True, "reason": "ack-only handshake — order NOT placed", "order_ref": None})
        # ── real execution (enabled after handshake) ──
        ref = await self._execute(p)
        return web.json_response({"accepted": ref is not None, "reason": "accepted" if ref else "enqueue failed", "order_ref": ref})

    async def _execute(self, p: dict) -> str | None:
        """Enqueue an entry to the executor carrying the brain's stop + TTL. The executor
        (B1) places the entry + native stop; the TTL exit (hold_seconds) is owned here/guardian."""
        entry = float(p["entry"]); stop = float(p["stop"])
        size_usd = float(p["size_notional"])
        # B2 — LIVE sizing is OURS, never the brain's number: derive notional so a stop-out
        # loses exactly RISK_PER_TRADE_PCT of equity (never feel-sized). Probe keeps its
        # ≤$50 cap untouched. Cap derived notional at the MAX_LEVERAGE implied-lev wall.
        if p["mode"] == "live":
            eq = self.risk.portfolio_value
            stop_frac = abs(entry - stop) / entry
            derived = (settings.RISK_PER_TRADE_PCT * eq) / stop_frac if stop_frac > 0 else 0.0
            derived = min(derived, settings.MAX_LEVERAGE * eq)
            logger.info(
                f"[Intake] B2 live-size {p['coin']}: stop {stop_frac*100:.2f}% → "
                f"${derived:.0f} notional (1%-risk=${settings.RISK_PER_TRADE_PCT*eq:.0f}); "
                f"brain asked ${size_usd:.0f}"
            )
            size_usd = derived
        sig = TradeSignal(
            strategy="brain", coin=p["coin"], direction=p["direction"],
            size_usd=size_usd, confidence=1.0,
            meta={"action": "enter", "leverage": min(settings.MAX_LEVERAGE, 3),
                  "source": p["source"], "stop_px": float(p["stop"]),
                  "hold_seconds": int(p["hold_seconds"]), "signal_id": p["signal_id"]},
        )
        await self.executor.enqueue(sig)
        logger.success(f"[Intake] ▶ ENQUEUED {p['direction']} {p['coin']} ${size_usd:.0f} (src {p['source']}, {p['mode']})")
        return p["signal_id"]

    async def status(self, request: web.Request) -> web.Response:
        """Brain polls this after sending: echoes the FILL + whether the protective stop is
        actually resting on HL (verified, not 'accepted'). 404-ish {pending} until the
        executor has processed the signal. This is the automatic stop-verification channel."""
        sid = request.match_info.get("sid", "")
        st = self.executor._signal_status.get(sid)
        if st is None:
            return web.json_response({"signal_id": sid, "status": "pending (not yet filled, rejected, or unknown id)"})
        return web.json_response(st)

    async def run(self):
        app = web.Application()
        app.router.add_post("/intake", self.intake)
        app.router.add_get("/status/{sid}", self.status)
        app.router.add_get("/health", lambda r: web.json_response({"ok": True, "ack_only": settings.INTAKE_ACK_ONLY}))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, settings.INTAKE_HOST, settings.INTAKE_PORT)
        await site.start()
        logger.info(
            f"[Intake] listening on http://{settings.INTAKE_HOST}:{settings.INTAKE_PORT}/intake "
            f"| {'ACK-ONLY (handshake)' if settings.INTAKE_ACK_ONLY else 'LIVE EXEC'} "
            f"| secret {'SET' if settings.HLBOT_SHARED_SECRET else 'MISSING'}"
        )
