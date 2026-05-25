"""
Stop-verification + fail-safe proof (the brain's resume blocker).

Demonstrates two contract invariants WITHOUT touching the live book:
  1. _place_protective_stop trusts EYES-ON-BOOK, not the ack. An order that ACKs
     but is NOT actually resting on HL is treated as a FAILURE.
  2. _open_position force-closes (fail-safe) when the stop can't be confirmed, and
     records the outcome in _signal_status for the brain's /status poll.

Run: PYTHONPATH=src .venv/bin/python tests/test_stop_verification.py
"""
import asyncio, sys
from unittest.mock import Mock, AsyncMock, MagicMock

sys.path.insert(0, "src")
sys.path.insert(0, ".")   # repo root for `config` package
from execution.executor import Executor
from data.store import TradeSignal


def _ok_ack(oid):
    return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}

def _err_ack():
    return {"status": "ok", "response": {"data": {"statuses": [{"error": "insufficient margin"}]}}}


async def test_stop_resting_confirmed():
    exe = Executor(MagicMock())
    exe._exchange = Mock(); exe._exchange.order = Mock(return_value=_ok_ack(111))
    exe._info = Mock(); exe._info.open_orders = Mock(return_value=[{"oid": 111, "coin": "ETH"}])
    ok = await exe._place_protective_stop("ETH", entry_is_buy=True, size=0.0237, entry_px=2112.5, stop_px=2029.0)
    assert ok is True, "confirmed-resting stop should return True"
    assert exe._last_stop == {"coin": "ETH", "oid": 111, "px": 2029.0}, exe._last_stop
    print("  ✅ stop ACKed AND resting on book → True, oid captured")


async def test_stop_acked_but_not_resting():
    exe = Executor(MagicMock())
    exe._exchange = Mock(); exe._exchange.order = Mock(return_value=_ok_ack(222))
    exe._info = Mock(); exe._info.open_orders = Mock(return_value=[])  # NOT on book
    ok = await exe._place_protective_stop("ETH", entry_is_buy=True, size=0.0237, entry_px=2112.5, stop_px=2029.0)
    assert ok is False, "ACK without a resting order MUST be treated as failure"
    assert exe._last_stop is None
    print("  ✅ stop ACKed but NOT resting → False (ack ≠ protection)")


async def test_stop_exchange_error():
    exe = Executor(MagicMock())
    exe._exchange = Mock(); exe._exchange.order = Mock(return_value=_err_ack())
    exe._info = Mock()
    ok = await exe._place_protective_stop("ETH", entry_is_buy=True, size=0.0237, entry_px=2112.5, stop_px=2029.0)
    assert ok is False
    print("  ✅ exchange-rejected stop → False")


async def _build_open_exe(stop_result: bool):
    exe = Executor(MagicMock())
    exe._exchange = Mock()
    exe._info = Mock()
    exe._get_mid_price = AsyncMock(return_value=2100.0)
    exe._get_sz_decimals = Mock(return_value=4)
    exe._place_market = AsyncMock(return_value={"fake": True})
    exe._parse_fill = Mock(return_value=(True, 2100.0, None))
    exe._place_market_close = AsyncMock()
    exe._place_protective_stop = AsyncMock(return_value=stop_result)
    exe.risk.register_fill = Mock(return_value=1)
    exe.risk.store.log_trade = AsyncMock()
    exe.risk.close_position = Mock()
    exe.squeeze_guard = None
    exe._last_stop = {"coin": "ETH", "oid": 999, "px": 2016.0}
    return exe


async def test_failsafe_closes_when_stop_fails():
    exe = await _build_open_exe(stop_result=False)
    sig = TradeSignal(strategy="brain", coin="ETH", direction="long", size_usd=50.0,
                      confidence=1.0, meta={"action": "enter", "signal_id": "fs-1", "stop_px": 2016.0})
    await exe._open_position(sig, 50.0)
    assert exe._place_market_close.called, "fail-safe must market-close the position"
    assert exe.risk.close_position.called, "fail-safe must release the risk slot"
    st = exe._signal_status["fs-1"]
    assert st["stop_resting"] is False and st["outcome"].startswith("FAIL-SAFE"), st
    print(f"  ✅ stop fails → position force-closed; /status echoes: {st['outcome']}")


async def test_status_echo_on_success():
    exe = await _build_open_exe(stop_result=True)
    sig = TradeSignal(strategy="brain", coin="ETH", direction="long", size_usd=50.0,
                      confidence=1.0, meta={"action": "enter", "signal_id": "ok-1", "stop_px": 2016.0})
    await exe._open_position(sig, 50.0)
    assert not exe._place_market_close.called, "must NOT close when stop confirmed"
    st = exe._signal_status["ok-1"]
    assert st["stop_resting"] is True and st["stop_oid"] == 999 and st["stop_px"] == 2016.0, st
    print(f"  ✅ stop confirmed → position held; /status echoes oid={st['stop_oid']} px=${st['stop_px']}")


async def main():
    tests = [
        ("stop confirmed resting", test_stop_resting_confirmed),
        ("stop ACKed but not resting (the trap)", test_stop_acked_but_not_resting),
        ("stop exchange error", test_stop_exchange_error),
        ("FAIL-SAFE auto-close on stop failure", test_failsafe_closes_when_stop_fails),
        ("/status echo on success", test_status_echo_on_success),
    ]
    for name, t in tests:
        print(f"[{name}]")
        await t()
    print(f"\n{len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    asyncio.run(main())
