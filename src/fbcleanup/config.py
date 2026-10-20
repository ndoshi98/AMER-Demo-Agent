"""Config + credential loading. Strict: unknown keys and wrong types are rejected."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

BASE_PATHS = ("sandbox", "us", "eu", "eu2")

_ALLOWED = {
    "workspace": {"base_path", "is_production", "expected_name"},
    "quarantine": {"min_consecutive_runs", "min_days_flagged"},
    "vaults": {"no_tx_within_days", "protect"},
    "wallets": {"no_tx_within_days", "protect"},
    "network_connections": {"no_tx_within_days", "protect"},
    "tags": {"protect_labels"},
    "users": {"inactive_days", "protect_roles", "protect_emails"},
    "audit": {"lookback_days", "activity_events"},
    "approvals": {"max_age_days"},
    "rate_limit": {"max_calls_per_second"},
    "paths": {"state_dir", "reports_dir", "approvals_dir"},
}
_PROTECT_KEYS = {"ids", "name_patterns", "customer_ref_id_prefixes", "tag_labels"}


class ConfigError(ValueError):
    pass


@dataclass
class Protect:
    ids: List[str] = field(default_factory=list)
    name_patterns: List[str] = field(default_factory=list)
    customer_ref_id_prefixes: List[str] = field(default_factory=list)
    tag_labels: List[str] = field(default_factory=list)


@dataclass
class Config:
    base_path: str = "us"
    is_production: bool = False
    expected_name: str = ""
    min_consecutive_runs: int = 2
    min_days_flagged: int = 14
    vault_no_tx_days: int = 90
    vault_protect: Protect = field(default_factory=Protect)
    wallet_no_tx_days: int = 180
    wallet_protect: Protect = field(default_factory=Protect)
    connection_no_tx_days: int = 180
    connection_protect: Protect = field(default_factory=Protect)
    tag_protect_labels: List[str] = field(default_factory=list)
    user_inactive_days: int = 180
    user_protect_roles: List[str] = field(default_factory=lambda: ["OWNER", "ADMIN"])
    user_protect_emails: List[str] = field(default_factory=list)
    audit_lookback_days: int = 365
    audit_activity_events: List[str] = field(default_factory=list)
    approval_max_age_days: int = 30
    max_calls_per_second: float = 3.0
    state_dir: str = "state"
    reports_dir: str = "reports"
    approvals_dir: str = "approvals"


def _section(raw: Dict[str, Any], name: str) -> Dict[str, Any]:
    sec = raw.get(name) or {}
    if not isinstance(sec, dict):
        raise ConfigError(f"'{name}' must be a mapping")
    unknown = set(sec) - _ALLOWED[name]
    if unknown:
        raise ConfigError(f"unknown key(s) in '{name}': {sorted(unknown)}")
    return sec


def _protect(sec: Dict[str, Any], allowed: set) -> Protect:
    p = sec.get("protect") or {}
    unknown = set(p) - allowed
    if unknown:
        raise ConfigError(f"unknown key(s) in protect: {sorted(unknown)}")
    return Protect(**{k: list(map(str, p.get(k) or [])) for k in allowed})


def _int(v: Any, name: str, minimum: int = 0) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
        raise ConfigError(f"'{name}' must be an integer >= {minimum}")
    return v


def parse_config(raw: Optional[Dict[str, Any]]) -> Config:
    raw = raw or {}
    unknown = set(raw) - set(_ALLOWED)
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {sorted(unknown)}")
    ws, qu = _section(raw, "workspace"), _section(raw, "quarantine")
    va, wa, nc = _section(raw, "vaults"), _section(raw, "wallets"), _section(raw, "network_connections")
    tg, us, au = _section(raw, "tags"), _section(raw, "users"), _section(raw, "audit")
    ap, rl, pa = _section(raw, "approvals"), _section(raw, "rate_limit"), _section(raw, "paths")

    base_path = ws.get("base_path", "us")
    if base_path not in BASE_PATHS:
        raise ConfigError(f"workspace.base_path must be one of {BASE_PATHS}")
    is_prod = ws.get("is_production", False)
    if not isinstance(is_prod, bool):  # the string "false" is truthy; refuse it outright
        raise ConfigError("workspace.is_production must be a YAML boolean (true/false)")

    cfg = Config(
        base_path=base_path,
        is_production=is_prod,
        expected_name=str(ws.get("expected_name") or ""),
        min_consecutive_runs=max(2, _int(qu.get("min_consecutive_runs", 2), "quarantine.min_consecutive_runs")),
        min_days_flagged=_int(qu.get("min_days_flagged", 14), "quarantine.min_days_flagged"),
        vault_no_tx_days=_int(va.get("no_tx_within_days", 90), "vaults.no_tx_within_days"),
        vault_protect=_protect(va, _PROTECT_KEYS),
        wallet_no_tx_days=_int(wa.get("no_tx_within_days", 180), "wallets.no_tx_within_days"),
        wallet_protect=_protect(wa, {"ids", "name_patterns"}),
        connection_no_tx_days=_int(nc.get("no_tx_within_days", 180), "network_connections.no_tx_within_days"),
        connection_protect=_protect(nc, {"ids"}),
        tag_protect_labels=list(map(str, tg.get("protect_labels") or [])),
        user_inactive_days=_int(us.get("inactive_days", 180), "users.inactive_days", 1),
        user_protect_roles=[str(r).upper() for r in (us.get("protect_roles") or ["OWNER", "ADMIN"])],
        user_protect_emails=[str(e).lower() for e in (us.get("protect_emails") or [])],
        audit_lookback_days=min(365, _int(au.get("lookback_days", 365), "audit.lookback_days", 1)),
        audit_activity_events=list(map(str, au.get("activity_events") or [])),
        approval_max_age_days=_int(ap.get("max_age_days", 30), "approvals.max_age_days", 1),
        max_calls_per_second=float(rl.get("max_calls_per_second", 3)),
        state_dir=str(pa.get("state_dir", "state")),
        reports_dir=str(pa.get("reports_dir", "reports")),
        approvals_dir=str(pa.get("approvals_dir", "approvals")),
    )
    if cfg.max_calls_per_second <= 0:
        raise ConfigError("rate_limit.max_calls_per_second must be > 0")
    return cfg


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as fh:
        return parse_config(yaml.safe_load(fh))


def load_env_file(path: str) -> None:
    """Minimal KEY=VALUE loader. Never overrides variables already set in the environment."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def load_credentials() -> Tuple[str, str]:
    """Return (api_key, secret_key_pem_text). Raises ConfigError with an actionable message."""
    api_key = os.environ.get("FIREBLOCKS_API_KEY", "").strip()
    secret_path = os.environ.get("FIREBLOCKS_SECRET_PATH", "").strip()
    if not api_key or api_key.startswith("<"):
        raise ConfigError("FIREBLOCKS_API_KEY is not set (see config/.env.example)")
    if not secret_path or secret_path.startswith("<"):
        raise ConfigError("FIREBLOCKS_SECRET_PATH is not set (see config/.env.example)")
    p = Path(secret_path).expanduser()
    if not p.is_file():
        raise ConfigError(f"FIREBLOCKS_SECRET_PATH does not point to a file: {p}")
    if p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"{p} is readable by group/others — chmod 600 it before use")
    return api_key, p.read_text(encoding="utf-8")


def enforce_prod_gate(cfg: Config, confirmed_prod: bool) -> None:
    if cfg.is_production and not confirmed_prod:
        raise SystemExit(
            "REFUSING: workspace.is_production is true. Re-run with --i-understand-this-is-prod "
            "(there is no config-file override)."
        )
