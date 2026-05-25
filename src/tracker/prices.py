"""
Price layer for the tracker — Pyth Hermes (free, no key, low-latency).

Covers crypto + US equities + FX/metals from one endpoint, so it prices everything the
HL traders touch (incl. equity perps like MU/CRCL). HL's own mids are still preferred for
HL-listed perps; Pyth fills cross-asset / off-HL reference prices. Feed-ids are resolved
once and cached.
"""
import requests

HERMES = "https://hermes.pyth.network"
_ID_CACHE: dict[tuple[str, str], str] = {}


def feed_id(symbol: str, asset_type: str = "crypto") -> str | None:
    """Resolve a Pyth price-feed id for BASE/USD. Cached. asset_type: crypto|equity|fx|metal."""
    key = (symbol.upper(), asset_type)
    if key in _ID_CACHE:
        return _ID_CACHE[key]
    r = requests.get(f"{HERMES}/v2/price_feeds",
                     params={"query": symbol, "asset_type": asset_type}, timeout=15).json()
    chosen = None
    for f in r:
        a = f.get("attributes", {})
        if a.get("base", "").upper() == symbol.upper() and a.get("quote_currency", "USD") == "USD":
            chosen = f["id"]; break
    if chosen is None and r:
        chosen = r[0]["id"]
    if chosen:
        _ID_CACHE[key] = chosen
    return chosen


def prices(ids: list[str]) -> dict[str, float]:
    """Latest USD prices for a list of feed-ids → {feed_id: price}."""
    if not ids:
        return {}
    r = requests.get(f"{HERMES}/v2/updates/price/latest",
                     params=[("ids[]", i) for i in ids], timeout=15).json()
    out = {}
    for p in r.get("parsed", []):
        out[p["id"]] = int(p["price"]["price"]) * 10 ** int(p["price"]["expo"])
    return out


def price(symbol: str, asset_type: str = "crypto") -> float | None:
    """Convenience: one symbol → USD price (None if no feed)."""
    fid = feed_id(symbol, asset_type)
    if not fid:
        return None
    return prices([fid]).get(fid)
