"""Live USD prices for Fireblocks assets, from CoinGecko's public API. Not derived from transaction history.

Why: the console shows a USD value for any asset with a market (e.g. HYPE), but the API exposes no read-price endpoint,
and last-transaction pricing (sweep_value.usd_price) returns nothing for an asset with no recent transfer. "No price" must
never be read as "$0", so every asset that cannot be priced is returned with a reason, never silently as zero.

How an asset is priced (nothing is guessed):
  mainnet native      -> curated symbol -> CoinGecko coin id
  mainnet token       -> its contract address on the chain's CoinGecko platform (exact match, no symbol lookup)
  testnet native      -> priced like its mainnet twin (the console reports USD on testnet transactions)
  testnet token       -> priced like its mainnet twin ONLY for a short curated symbol list (USDC, EURC, ...); a custom
                         demo token (dUSD, tMMF, ...) has no market and is reported as unpriced
  deprecated testnet native -> priced like its mainnet twin (the console still values it, e.g. a deprecated ETH_TEST6)
  other deprecated assets   -> unpriced
  config `pricing.overrides` -> an asset id with an explicit USD price (even 0) beats everything above. A custom token is
                         worth $0 in the console unless someone assigned a value in the tokenization engine, which the
                         API cannot read, so those values live in the config, never guessed.
A price is rejected if it is not a plain positive number in a sane range or if CoinGecko's own timestamp is stale.
Only symbols and contract addresses leave this machine, over HTTPS with certificate verification on. Never writes.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Optional

API = "https://api.coingecko.com/api/v3"
MAX_AGE_SECONDS = 3600
PRICE_FLOOR = Decimal("1e-9")
PRICE_CEIL = Decimal("1e7")
RETRY_STATUS = {429, 500, 502, 503, 504}

# Fireblocks native-asset symbol -> CoinGecko coin id (also used for testnet twins: ETH on any testnet is priced as ETH)
NATIVE_BY_SYMBOL: Dict[str, str] = {
    "ETH": "ethereum", "BTC": "bitcoin", "TBTC": "bitcoin", "SOL": "solana", "AVAX": "avalanche-2", "XLM": "stellar",
    "SEI": "sei-network", "HYPE": "hyperliquid", "MON": "monad", "TRX": "tron", "CC": "canton-network",
    "CC_TEST": "canton-network", "POL": "polygon-ecosystem-token", "XRP": "ripple", "SUI_TEST": "sui",
    "USDC": "usd-coin",   # ARC's native gas asset is USDC
}
# Tokens with a real market, used as the twin of a testnet token with the same symbol. Anything else on a testnet is custom.
TOKEN_BY_SYMBOL: Dict[str, str] = {
    "USDC": "usd-coin", "EURC": "euro-coin", "PYUSD": "paypal-usd", "USDG": "global-dollar", "UNI": "uniswap",
    "WETH": "weth", "USDT": "tether", "1INCH": "1inch", "AAVE": "aave",
}
# Fireblocks blockchain legacyId -> CoinGecko asset-platform id, for chains without a numeric EVM chain id
NON_EVM_PLATFORMS: Dict[str, str] = {"SOL": "solana", "TRX": "tron"}


@dataclass(frozen=True)
class AssetMeta:
    asset_id: str
    symbol: str
    address: str
    native: bool
    chain: str        # Fireblocks blockchain legacyId
    chain_id: str     # EVM chain id as a string, else whatever Fireblocks reports
    testnet: bool
    deprecated: bool


@dataclass(frozen=True)
class Quote:
    usd: Decimal
    source: str       # e.g. "coingecko:hyperliquid", "coingecko:base:0x8335..", "twin:coingecko:ethereum"
    as_of: int        # CoinGecko's own last-updated unix time


@dataclass
class PriceResult:
    quotes: Dict[str, Quote] = field(default_factory=dict)
    unpriced: Dict[str, str] = field(default_factory=dict)   # asset id -> why

    def decimals(self, asset_ids: Iterable[str]) -> Dict[str, Optional[Decimal]]:
        """Same shape the sweep/hide code already uses: asset id -> USD per unit, or None when unpriced."""
        return {a: (self.quotes[a].usd if a in self.quotes else None) for a in asset_ids}


def describe_assets(client, chains: Dict[str, Dict[str, Any]], asset_ids: Iterable[str]) -> Dict[str, Optional[AssetMeta]]:
    """AssetMeta per id via get_asset + the blockchain list. None when either cannot be resolved (treated as unpriced)."""
    out: Dict[str, Optional[AssetMeta]] = {}
    for aid in asset_ids:
        try:
            a = client.call("get_asset", id=aid) or {}
        except Exception:  # noqa: BLE001 - one unreadable asset must not stop the others
            out[aid] = None
            continue
        b = chains.get(a.get("blockchainId")) or {}
        on, bo = a.get("onchain") or {}, b.get("onchain") or {}
        if not b or not isinstance(bo.get("test"), bool):
            out[aid] = None
            continue
        out[aid] = AssetMeta(
            asset_id=aid, symbol=str(on.get("symbol") or a.get("displaySymbol") or "").upper(),
            address=str(on.get("address") or ""), native=a.get("id") == b.get("nativeAssetId"),
            chain=str(b.get("legacyId") or ""), chain_id=str(bo.get("chainId") or ""), testnet=bo["test"],
            deprecated=(a.get("metadata") or {}).get("deprecated") is True)
    return out


def http_get_json(url: str, *, timeout: float = 20.0, sleep: Callable[[float], None] = time.sleep, tries: int = 4) -> Any:
    """GET with exponential backoff on 429/5xx. https only; certificate verification is the urllib default and stays on."""
    if not url.startswith("https://"):
        raise ValueError("refusing a non-https price URL")
    last: Optional[Exception] = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "fbcleanup-prices/1"})
            with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - https enforced above
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in RETRY_STATUS:
                raise
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 2.0 * 2 ** attempt
        except (urllib.error.URLError, TimeoutError) as exc:
            last, delay = exc, 2.0 * 2 ** attempt
        if attempt < tries - 1:
            sleep(delay)
    raise RuntimeError(f"price request failed after {tries} tries: {last}")


def _valid(entry: Any, now: float) -> "tuple[Optional[Decimal], int, str]":
    """(price, as_of, "") or (None, 0, reason)."""
    if not isinstance(entry, dict):
        return None, 0, "not listed"
    usd, ts = entry.get("usd"), entry.get("last_updated_at")
    if isinstance(usd, bool) or not isinstance(usd, (int, float)):
        return None, 0, "no usd price in the response"
    p = Decimal(str(usd))
    if not (PRICE_FLOOR <= p <= PRICE_CEIL):
        return None, 0, f"implausible price {usd}"
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None, 0, "response has no timestamp"
    if now - ts > MAX_AGE_SECONDS:
        return None, 0, f"stale price ({int(now - ts)}s old)"
    return p, int(ts), ""


class LivePricer:
    def __init__(self, fetch: Callable[[str], Any] = http_get_json, now: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, pause: float = 2.5,
                 overrides: Optional[Dict[str, Decimal]] = None):
        self._fetch, self._now, self._sleep, self._pause = fetch, now, sleep, pause
        self._overrides = dict(overrides or {})
        self._platforms: Optional[Dict[str, str]] = None

    def _get(self, path: str, **params: str) -> Any:
        return self._fetch(f"{API}/{path}?" + urllib.parse.urlencode(params))

    def _platform_for(self, m: AssetMeta) -> Optional[str]:
        if m.chain in NON_EVM_PLATFORMS:
            return NON_EVM_PLATFORMS[m.chain]
        if self._platforms is None:
            data = self._get("asset_platforms")
            self._platforms = {str(p["chain_identifier"]): p["id"] for p in data
                               if isinstance(p, dict) and p.get("chain_identifier") is not None and p.get("id")}
        return self._platforms.get(m.chain_id)

    def price(self, metas: Dict[str, Optional[AssetMeta]]) -> PriceResult:
        res, now = PriceResult(), self._now()
        coin_for: Dict[str, "tuple[str, str]"] = {}      # asset id -> (coin id, source prefix)
        contracts: List[AssetMeta] = []
        for aid, m in metas.items():
            if aid in self._overrides:
                res.quotes[aid] = Quote(self._overrides[aid], "override:config", int(now))
            elif m is None:
                res.unpriced[aid] = "asset or its chain could not be resolved"
            elif m.deprecated and not (m.native and m.testnet):
                res.unpriced[aid] = "deprecated asset"
            elif m.native:
                cid = NATIVE_BY_SYMBOL.get(m.symbol)
                if cid:
                    coin_for[aid] = (cid, "twin:coingecko" if m.testnet else "coingecko")
                else:
                    res.unpriced[aid] = f"no CoinGecko mapping for native {m.symbol or aid}"
            elif m.testnet:
                cid = TOKEN_BY_SYMBOL.get(m.symbol)
                if cid:
                    coin_for[aid] = (cid, "twin:coingecko")
                else:
                    res.unpriced[aid] = f"custom testnet token {m.symbol or aid}: no market price"
            elif m.address:
                contracts.append(m)
            else:
                res.unpriced[aid] = "token has no contract address"

        ids = sorted({c for c, _ in coin_for.values()})
        if ids:
            data = self._get("simple/price", ids=",".join(ids), vs_currencies="usd", include_last_updated_at="true")
            for aid, (cid, prefix) in coin_for.items():
                p, ts, why = _valid((data or {}).get(cid), now)
                if p is None:
                    res.unpriced[aid] = f"{cid}: {why}"
                else:
                    res.quotes[aid] = Quote(p, f"{prefix}:{cid}", ts)

        for m in contracts:       # the free tier allows ONE contract address per request
            platform = self._platform_for(m)
            if not platform:
                res.unpriced[m.asset_id] = f"no CoinGecko platform for chain {m.chain} ({m.chain_id})"
                continue
            self._sleep(self._pause)
            try:
                data = self._get(f"simple/token_price/{platform}", contract_addresses=m.address, vs_currencies="usd",
                                 include_last_updated_at="true")
            except Exception as exc:  # noqa: BLE001
                res.unpriced[m.asset_id] = f"lookup failed: {str(exc).splitlines()[0][:120]}"
                continue
            entry = next((v for k, v in (data or {}).items() if str(k).lower() == m.address.lower()), None) \
                if isinstance(data, dict) else None
            p, ts, why = _valid(entry, now)
            if p is None:
                res.unpriced[m.asset_id] = f"{platform}:{m.address[:10]}: {why}"
            else:
                res.quotes[m.asset_id] = Quote(p, f"coingecko:{platform}:{m.address}", ts)
        return res
