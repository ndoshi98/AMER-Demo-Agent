import urllib.error
from decimal import Decimal

import pytest

from fbcleanup import prices as P

NOW = 1_800_000_000


def meta(aid, symbol, *, native=False, testnet=False, deprecated=False, address="", chain="ETH", chain_id="1"):
    return P.AssetMeta(aid, symbol, address, native, chain, chain_id, testnet, deprecated)


class Fake:
    """Records URLs and answers from a path-prefix table."""
    def __init__(self, table):
        self.table, self.urls = table, []

    def __call__(self, url):
        self.urls.append(url)
        for prefix, resp in self.table.items():
            if f"/v3/{prefix}" in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected url {url}")


def pricer(table):
    f = Fake(table)
    return P.LivePricer(fetch=f, now=lambda: NOW, sleep=lambda s: None, pause=0), f


def coin(usd, age=10):
    return {"usd": usd, "last_updated_at": NOW - age}


def test_mainnet_native_is_priced_from_the_coin_id():
    p, f = pricer({"simple/price": {"hyperliquid": coin(78.8)}})
    r = p.price({"HYPE_ETH": meta("HYPE_ETH", "HYPE", native=True, chain="HYPE_ETH", chain_id="999")})
    assert r.quotes["HYPE_ETH"].usd == Decimal("78.8") and r.quotes["HYPE_ETH"].source == "coingecko:hyperliquid"
    assert "ids=hyperliquid" in f.urls[0] and not r.unpriced


def test_unmapped_native_is_unpriced_with_a_reason_never_zero():
    p, _ = pricer({})
    r = p.price({"TEMPO": meta("TEMPO", "TEMPO", native=True, chain="TEMPO", chain_id="4217")})
    assert "TEMPO" not in r.quotes and "no CoinGecko mapping" in r.unpriced["TEMPO"]
    assert r.decimals(["TEMPO"]) == {"TEMPO": None}


def test_testnet_native_uses_its_mainnet_twin_and_says_so():
    p, _ = pricer({"simple/price": {"ethereum": coin(2300)}})
    r = p.price({"ETH_TEST5": meta("ETH_TEST5", "ETH", native=True, testnet=True, chain="ETH_TEST5", chain_id="11155111")})
    assert r.quotes["ETH_TEST5"].source == "twin:coingecko:ethereum"


def test_custom_testnet_token_has_no_market_price():
    p, _ = pricer({})
    r = p.price({"DUSD": meta("DUSD", "DUSD", testnet=True, address="0xabc", chain="BASECHAIN_ETH_TEST5", chain_id="84532")})
    assert "custom testnet token" in r.unpriced["DUSD"]


def test_well_known_testnet_token_uses_the_symbol_twin():
    p, _ = pricer({"simple/price": {"usd-coin": coin(0.9994)}})
    r = p.price({"USDC_T": meta("USDC_T", "USDC", testnet=True, address="0x036", chain="ETH_TEST5", chain_id="11155111")})
    assert r.quotes["USDC_T"].usd == Decimal("0.9994")


def test_mainnet_token_is_priced_by_contract_on_the_chains_platform_one_address_per_request():
    addr = "0xAbCdEf0123456789aBcDeF0123456789AbCdEf01"
    p, f = pricer({"asset_platforms": [{"id": "ethereum", "chain_identifier": 1}],
                   "simple/token_price/ethereum": {addr.lower(): coin(0.0012)}})
    r = p.price({"HEX": meta("HEX", "HEX", address=addr)})
    assert r.quotes["HEX"].usd == Decimal("0.0012") and r.quotes["HEX"].source.endswith(addr)
    assert sum("token_price" in u for u in f.urls) == 1


def test_mainnet_token_with_unknown_platform_or_listing_is_unpriced():
    p, _ = pricer({"asset_platforms": [{"id": "ethereum", "chain_identifier": 1}]})
    r = p.price({"X": meta("X", "X", address="0xdead", chain="NEWCHAIN", chain_id="424242")})
    assert "no CoinGecko platform" in r.unpriced["X"]
    p2, _ = pricer({"asset_platforms": [{"id": "ethereum", "chain_identifier": 1}], "simple/token_price/ethereum": {}})
    assert "not listed" in p2.price({"Y": meta("Y", "Y", address="0xbeef")}).unpriced["Y"]


def test_solana_token_uses_the_solana_platform_without_fetching_platforms():
    mint = "ExampleSolanaMint1111111111111111111111111"
    p, f = pricer({"simple/token_price/solana": {mint: coin(1.0)}})
    r = p.price({"SOL_USDC": meta("SOL_USDC", "USDC", address=mint, chain="SOL", chain_id="4sGj")})
    assert "SOL_USDC" in r.quotes and not any("asset_platforms" in u for u in f.urls)


@pytest.mark.parametrize("bad", [{"usd": 0, "last_updated_at": NOW}, {"usd": -1, "last_updated_at": NOW},
                                 {"usd": True, "last_updated_at": NOW}, {"usd": "5", "last_updated_at": NOW},
                                 {"usd": 1e9, "last_updated_at": NOW}, {"usd": 5}, None])
def test_implausible_or_malformed_prices_are_rejected(bad):
    p, _ = pricer({"simple/price": {"ethereum": bad}})
    r = p.price({"ETH": meta("ETH", "ETH", native=True)})
    assert "ETH" not in r.quotes and "ETH" in r.unpriced


def test_stale_price_is_rejected():
    p, _ = pricer({"simple/price": {"ethereum": coin(2300, age=P.MAX_AGE_SECONDS + 1)}})
    assert "stale" in p.price({"ETH": meta("ETH", "ETH", native=True)}).unpriced["ETH"]


def test_deprecated_token_and_unresolvable_assets_are_unpriced_not_guessed():
    p, f = pricer({})
    r = p.price({"OLD": meta("OLD", "OLD", deprecated=True, address="0x1"), "GONE": None})
    assert r.unpriced["OLD"] == "deprecated asset" and "could not be resolved" in r.unpriced["GONE"] and not f.urls


def test_deprecated_testnet_native_is_still_priced_like_its_twin():
    p, _ = pricer({"simple/price": {"ethereum": coin(2300)}})
    r = p.price({"ETH_TEST6": meta("ETH_TEST6", "ETH", native=True, testnet=True, deprecated=True)})
    assert r.quotes["ETH_TEST6"].usd == Decimal("2300")


def test_override_beats_everything_including_zero_for_a_custom_token_and_makes_no_request():
    f = Fake({})
    p = P.LivePricer(fetch=f, now=lambda: NOW, sleep=lambda s: None, pause=0,
                     overrides={"DUSD": Decimal(0), "CUSD": Decimal(1)})
    r = p.price({"DUSD": meta("DUSD", "DUSD", testnet=True, address="0xa"), "CUSD": None,
                 "OTHER": meta("OTHER", "OTHER", testnet=True, address="0xb")})
    assert r.quotes["DUSD"].usd == 0 and r.quotes["DUSD"].source == "override:config"
    assert r.quotes["CUSD"].usd == 1 and "OTHER" in r.unpriced and not f.urls


def test_a_failed_token_lookup_leaves_that_asset_unpriced_and_others_alone():
    p, _ = pricer({"asset_platforms": [{"id": "ethereum", "chain_identifier": 1}],
                   "simple/token_price/ethereum": RuntimeError("boom")})
    r = p.price({"A": meta("A", "A", address="0xaa")})
    assert "lookup failed" in r.unpriced["A"]


def test_http_get_json_refuses_non_https():
    with pytest.raises(ValueError):
        P.http_get_json("http://api.coingecko.com/api/v3/ping")


def test_http_get_json_backs_off_on_429_then_succeeds(monkeypatch):
    calls, sleeps = [], []

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok": 1}'

    def fake_open(req, timeout):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.HTTPError(req.full_url, 429, "slow", {}, None)
        return Resp()

    monkeypatch.setattr(P.urllib.request, "urlopen", fake_open)
    assert P.http_get_json("https://x.test/a", sleep=sleeps.append) == {"ok": 1}
    assert sleeps == [2.0, 4.0]


def test_http_get_json_does_not_retry_a_404(monkeypatch):
    def fake_open(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, None)
    monkeypatch.setattr(P.urllib.request, "urlopen", fake_open)
    with pytest.raises(urllib.error.HTTPError):
        P.http_get_json("https://x.test/a", sleep=lambda s: None)
