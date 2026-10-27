from pathlib import Path

import pytest

from fbcleanup.config import ConfigError, parse_config, scope_paths, workspace_slug


def test_slug_is_filesystem_safe_and_stable():
    assert workspace_slug("Neil Doshi - Testnet [ApI Test]") == "neil-doshi-testnet-api-test"
    assert workspace_slug("AMER (demo)") == "amer-demo"


def test_expected_name_is_required_to_scope_paths():
    for name in ("", "  ", "[]"):
        with pytest.raises(ConfigError):
            workspace_slug(name)
    with pytest.raises(ConfigError):
        scope_paths(parse_config({}), Path("/r"))


def test_two_workspaces_never_share_state_reports_or_approvals():
    a = parse_config({"workspace": {"expected_name": "Testnet One"}})
    b = parse_config({"workspace": {"expected_name": "AMER (demo)"}})
    sa, sb = scope_paths(a, Path("/r")), scope_paths(b, Path("/r"))
    assert sa == Path("/r/state/testnet-one") and sb == Path("/r/state/amer-demo")
    for attr in ("state_dir", "reports_dir", "approvals_dir"):
        assert getattr(a, attr) != getattr(b, attr)
        assert getattr(a, attr).endswith("testnet-one") and getattr(b, attr).endswith("amer-demo")


def test_env_file_override_replaces_environment_and_requires_both_keys(tmp_path, monkeypatch):
    import pytest
    from fbcleanup.config import ConfigError, load_env_file
    monkeypatch.setenv("FIREBLOCKS_API_KEY", "from-env")
    monkeypatch.setenv("FIREBLOCKS_SECRET_PATH", "/env/path")
    f = tmp_path / "e"
    f.write_text("FIREBLOCKS_API_KEY=from-file\nFIREBLOCKS_SECRET_PATH=/file/path\n")
    load_env_file(str(f))
    assert __import__("os").environ["FIREBLOCKS_API_KEY"] == "from-env"
    load_env_file(str(f), override=True)
    assert __import__("os").environ["FIREBLOCKS_API_KEY"] == "from-file"
    f.write_text("FIREBLOCKS_API_KEY=only-key\n")
    with pytest.raises(ConfigError):
        load_env_file(str(f), override=True)
    with pytest.raises(ConfigError):
        load_env_file(str(tmp_path / "missing"), override=True)


def test_pricing_overrides_parse_and_reject_bad_values():
    from decimal import Decimal
    from fbcleanup.config import parse_config
    assert parse_config({"pricing": {"overrides": {"DUSD": 0, "CUSD": "1.5"}}}).price_overrides == {"DUSD": Decimal(0), "CUSD": Decimal("1.5")}
    assert parse_config({}).price_overrides == {}
    for bad in ({"A": -1}, {"A": True}, {"A": "abc"}, {"A": float("inf")}, ["A"]):
        with pytest.raises(ConfigError):
            parse_config({"pricing": {"overrides": bad}})
