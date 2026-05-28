# Smart Money Candidates — Research Handoff
Generated: 2026-05-28 | Source: overnight deep-scan of 1,050+ HL wallets
Filters: taker>=90%, no perfect 100% WR, n>=100 closed trades, positive realized PnL, paper-loss check, active or maxed fills

---

## HOW TO USE THIS FILE
Each wallet below has been vetted for copyability:
- **taker%** = execution style (>90% = takes liquidity = signal is instant, copyable)
- **WR** = win rate on CLOSED trades only (not unrealized)
- **n** = number of closed trades on dominant side (bigger = more trustworthy)
- **span** = account age in days
- **dom_pnl** = realized PnL on their dominant direction
- **MAXED** = hit 2000-fill API cap = extremely active
- **ACTIVE** = currently has open positions or maxed fills

Eliminated: makers, 100% WR (fake/thin), thin samples (<100), WR/PnL mismatch, scale-adders hiding paper losses, hyna:/@ dominated coins

---

## COMMODITY SHORT (23 qualified, top 3 shown)

### #1 ACTIVE — 0x9c972d06eceee9dc08e2d295742d2045f8e54fa2
- Direction: SHORT | WR: 99.3% | n=442 | PnL: $134,652
- Span: 77 days | Fills: MAXED (2000) | Status: ACTIVE
- Open: 1 position, upnl=+$6,140
- Top coins: xyz:CL (crude oil), xyz:CBRS, xyz:MU
- Note: Highest PnL commodity short, currently positioned, 77-day proven record

### #1 TRACK RECORD — 0xc7be26aba75daba73d9f4c202a16fb5ca7abc238
- Direction: SHORT | WR: 93.0% | n=330 | PnL: $87,322
- Span: 491 days | Fills: 1,250 | Status: FLAT (no open position)
- Top coins: xyz:CL (crude oil), xyz:BRENTOIL, vntl:ANTHROPIC
- Note: Most battle-tested commodity short. 491-day track record. Waits for setups, currently flat.

### #3 — 0x98399de6ca852dd8b7dd21225a32c28c95574b25
- Direction: SHORT | WR: 84.4% | n=972 | PnL: $37,467
- Span: 102 days | Fills: MAXED | Status: ACTIVE
- Open: 2 positions, upnl=+$17,169
- Top coins: ETH, HYPE, BTC, xyz:SILVER

---

## COMMODITY LONG (25 qualified, top 4 shown)

### #1 TRACK RECORD — 0xd360ecb91406717ad13c4fae757b69b417e2af6b
- Direction: LONG | WR: 89.6% | n=809 | PnL: $103,938
- Span: 925 days | Fills: 1,996 | Status: FLAT
- Top coins: HYPE, xyz:SILVER, flx:OIL, xyz:CL
- Note: LONGEST track record of ANY wallet in dataset. 925 days = 2.5 years. Still near-maxed fills.

### #2 PnL — 0x757bb286acc54032172594e2ae11f02442d7c6ea
- Direction: LONG | WR: 76.7% | n=503 | PnL: $407,461
- Span: 499 days | Fills: MAXED | Status: ACTIVE (flat positions)
- Top coins: xyz:XYZ100, xyz:CL, xyz:BRENTOIL, HYPE
- Note: Biggest realized PnL in commodity longs. 499-day track record.

### #3 HIGH WR — 0x6b7076bfad92dc960ef690ea29db1998fcbb2317
- Direction: LONG | WR: 93.6% | n=860 | PnL: $73,148
- Span: 23 days | Fills: MAXED | Status: ACTIVE
- Top coins: xyz:CL, xyz:BRENTOIL, xyz:SPCX, ZEC
- Note: Highest WR + largest sample in commodity longs. Short span — possibly automated.

### #4 — 0xcb3ee3f1b22cdff8b6ca4839cd0784d34b85dea9
- Direction: LONG | WR: 80.2% | n=491 | PnL: $160,879
- Span: 50 days | Fills: MAXED | Status: ACTIVE
- Open: 1 position, upnl=+$121,288
- Top coins: BTC, xyz:SILVER, xyz:SP500

---

## EQUITY SHORT (8 qualified, top 3 shown)

### #1 — 0xa243ecc646b1ce99c8c2810b6581cce76ec6f989
- Direction: SHORT | WR: 79.5% | n=782 | PnL: $159,563
- Span: 139 days | Fills: MAXED | Status: ACTIVE
- Open: 1 position, upnl=-$4,634 (small manageable loss)
- Top coins: BTC, xyz:SP500, ONDO
- Note: Best combo of PnL + span + activity in equity shorts

### #2 HIGH WR — 0x9321d8117e73b0c79035f0e87debcfd8dbb1d75a
- Direction: SHORT | WR: 98.5% | n=1,156 | PnL: $141,062
- Span: 99 days | Fills: MAXED | Status: ACTIVE
- Open: 4 positions, upnl=+$14,465
- Top coins: ZEC, ETH, HYPE, xyz:RIVN
- Note: Largest n of any equity short. ZEC flag — may distort classification.

### #3 — 0xe2f86feccb1207710a442bc4e27a0701a290275a
- Direction: SHORT | WR: 99.4% | n=334 | PnL: $116,603
- Span: 119 days | Fills: MAXED | Status: ACTIVE
- Open: 1 position, upnl=+$318
- Top coins: BTC, HYPE, xyz:CBRS

---

## EQUITY LONG (23 qualified, top 3 shown)

### #1 TRACK RECORD — 0x27c5fdef9a082abd0711c611dbde9d7db9611aae
- Direction: LONG | WR: 90.1% | n=343 | PnL: $187,945
- Span: 801 days | Fills: 857 | Status: ACTIVE
- Open: 1 position, upnl=+$4,210
- Top coins: HYPE, BTC, xyz:NVDA
- Note: 801-day track record, still active. Most battle-tested equity long.

### #2 PnL MONSTER — 0x143c28ae5b8642f58c98b8a6f82a0f314d23f6ab
- Direction: LONG | WR: 95.3% | n=1,610 | PnL: $1,193,536
- Span: 26 days | Fills: MAXED | Status: ACTIVE
- Open: 1 position (HYPE @ 48.159), upnl=+$44,140
- Top coins: xyz:MU, xyz:SKHX, HYPE, xyz:AMD
- ⚠ WARNING: 26-day span with 1,610 closed trades = ~62 trades/day. Likely automated. Real edge but watch for sudden stops.

### #3 — 0x0c44471cc8516125ead11e09446302ae20540ed4
- Direction: LONG | WR: 96.6% | n=385 | PnL: $241,017
- Span: 45 days | Fills: MAXED | Status: ACTIVE
- Open: 3 positions, upnl=+$356,132
- Top coins: HYPE, xyz:MU, BTC, ZEC

---

## CRYPTO SHORT (14 qualified, top 3 shown)

### #1 — 0x2f01afc9f9437c4fcb8f2e8612c57bf522bcc81d
- Direction: SHORT only (never longs) | WR: 95.7% | n=350 | PnL: $311,764
- Span: 238 days | Fills: 966 | Status: ACTIVE
- Open: SHORT ETH @ 2522.28 (upnl=+$358,114) + SHORT BTC @ 66,439 (upnl=-$23,053)
- Top coins: ETH, BTC
- Note: Pure short specialist, 238-day record. Shorted ETH near exact top. $647k total (realized + paper).

### #2 TRACK RECORD — 0x8eb3f4d468a9763ee66ceb63a0c64b7044c2aa2d
- Direction: SHORT | WR: 77.4% | n=421 | PnL: $150,010
- Span: 281 days | Fills: 1,147 | Status: ACTIVE
- Open: 1 position, upnl=+$39,703
- Top coins: ETH, ASTER, STBL
- Note: Longest active crypto short track record (281 days)

### #3 — 0x55a8f87e59611f86a31a58b361038f36fbded9ea
- Direction: SHORT | WR: 84.7% | n=704 | PnL: $24,526
- Span: 47 days | Fills: MAXED | Status: ACTIVE
- Open: 1 position, upnl=+$35,019
- Top coins: SOL, ETH, HYPE (liquid — most copyable coins)

---

## CRYPTO LONG (40 qualified, top 3 shown)

### #1 — 0x24a44aef48aeb27c7708dabfccda14b41fbf0ae1
- Direction: LONG | WR: 99.6% | n=805 | PnL: $462,616
- Span: 299 days | Fills: MAXED | Status: ACTIVE
- Open: 4 positions, upnl=+$160,946
- Top coins: HYPE, ETH, XPL, ASTER
- Note: Best overall crypto long. 299-day record, $623k total. Undisputed #1.

### #2 — 0x88696c4985f64ea1ebfb9e46c1b47197f7a244ab
- Direction: LONG | WR: 88.4% | n=786 | PnL: $361,153
- Span: 96 days | Fills: MAXED | Status: ACTIVE
- Open: 1 position, upnl=-$31,828 (monitor)
- Top coins: BTC, NIL, CC

### #3 — 0x3cce108f30e46eb0dc6257e31044f9aebf89427c
- Direction: LONG | WR: 79.6% | n=901 | PnL: $149,184
- Span: 115 days | Fills: MAXED | Status: ACTIVE
- Status: FLAT (no open position)
- Top coins: HYPE, SOL, BTC

---

## SUMMARY TABLE — TOP PICKS PER SLOT

| Slot | Address | Dir | WR | PnL | Span | Status |
|------|---------|-----|----|-----|------|--------|
| COMMODITY_SHORT | 0x9c972d06ec | SHORT | 99.3% | $135k | 77d | ACTIVE |
| COMMODITY_SHORT_VET | 0xc7be26ab | SHORT | 93.0% | $87k | 491d | FLAT |
| COMMODITY_LONG | 0xd360ecb9 | LONG | 89.6% | $104k | 925d | FLAT |
| COMMODITY_LONG_ACT | 0x6b7076bf | LONG | 93.6% | $73k | 23d | ACTIVE |
| EQUITY_SHORT | 0xa243ecc6 | SHORT | 79.5% | $160k | 139d | ACTIVE |
| EQUITY_LONG | 0x27c5fdef | LONG | 90.1% | $188k | 801d | ACTIVE |
| CRYPTO_SHORT | 0x2f01afc9 | SHORT | 95.7% | $312k | 238d | ACTIVE |
| CRYPTO_LONG | 0x24a44aef | LONG | 99.6% | $463k | 299d | ACTIVE |

---

## ANOMALIES ELIMINATED (do not use)
- **SCALE_ADDERS**: Open losers forever, close winners fast. WR inflated. 43 wallets removed.
- **FEE EATERS**: 0x0e9295ed — 95% WR but $1,111 total PnL on 2,000 trades. Copying costs more than they make.
- **THIN SAMPLE**: 100%+ WR on <50 trades = noise. 124 wallets removed.
- **WR/PNL MISMATCH**: High WR, negative PnL = wins small loses big. 33 wallets removed.
- **NON-STANDARD MARKETS**: hyna:, @XXX tokens — thin/illiquid, not mirriorable. Removed.

## SCAN STATUS
- Wallets scanned: 1,050 of ~3,880 (27%)
- Scan running overnight on Gaming PC
- Morning report will auto-generate at completion
- Re-run this analysis on full overnight_scan.jsonl for final rankings
