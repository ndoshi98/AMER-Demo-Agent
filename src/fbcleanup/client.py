"""SDK wrapper. Every API call goes through FireblocksClient.call(): the one place that
enforces dry-run, rate limiting and the append-only action journal.

There is deliberately NO policy-write op in OPS (see CLAUDE.md, constraint 5).
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# logical name -> (api_group attribute on the SDK client, candidate method names in preference order).
# Every entry was resolved against fireblocks==34.0.0 by `scripts/probe.py --offline`.
OPS: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    # --- reads ---
    "get_workspace": ("workspace", ("get_workspace",)),
    "list_vaults": ("vaults", ("get_paged_vault_accounts",)),
    "get_vault": ("vaults", ("get_vault_account",)),
    "list_transactions": ("transactions", ("get_transactions",)),
    "list_users": ("users", ("get_users",)),
    "list_console_users": ("console_user", ("get_console_users",)),
    "list_audit_logs": ("audit_logs", ("get_audit_logs",)),
    "list_internal_wallets": ("internal_wallets", ("get_internal_wallets",)),
    "list_external_wallets": ("external_wallets", ("get_external_wallets",)),
    "list_network_connections": ("network_connections", ("get_network_connections",)),
    "list_tags": ("tags", ("get_tags",)),
    "list_api_users": ("api_user", ("get_api_users",)),
    "list_user_groups": ("user_groups_beta", ("get_user_groups",)),
    "get_transaction": ("transactions", ("get_transaction",)),
    "estimate_fee": ("transactions", ("estimate_transaction_fee",)),
    "get_asset": ("blockchains_assets", ("get_asset",)),
    "list_blockchains": ("blockchains_assets", ("list_blockchains",)),
    "get_policy_legacy": ("policy_editor_beta", ("get_active_policy_legacy",)),
    "get_policy_v2": ("policy_editor_v2", ("get_active_policy",)),
    "get_policy_draft": ("policy_editor_v2", ("get_draft",)),
    "get_gas_station": ("gas_stations", ("get_gas_station_info",)),
    "get_gas_station_asset": ("gas_stations", ("get_gas_station_by_asset_id",)),
    # --- mutations (must also be listed in MUTATING_OPS) ---
    "hide_vault": ("vaults", ("hide_vault_account",)),
    "unhide_vault": ("vaults", ("unhide_vault_account",)),
    "delete_internal_wallet": ("internal_wallets", ("delete_internal_wallet",)),
    "delete_external_wallet": ("external_wallets", ("delete_external_wallet",)),
    "delete_network_connection": ("network_connections", ("delete_network_connection",)),
    "delete_tag": ("tags", ("delete_tag",)),
    "delete_console_user": ("console_user", ("delete_console_user",)),
    "create_transaction": ("transactions", ("create_transaction",)),  # fund movement: only sweep*.py use it
    "create_vault_asset": ("vaults", ("create_vault_account_asset",)),  # adds a wallet/address to a vault
    # overwrites the policy DRAFT only; nothing here publishes (CLAUDE.md constraint 5). Only write_tap_draft.py uses it.
    "update_policy_draft": ("policy_editor_v2", ("update_draft",)),
}

# kwargs each op is called with in this codebase; scripts/probe.py checks they exist on the resolved SDK method
OP_PARAMS: Dict[str, Tuple[str, ...]] = {
    "list_vaults": ("limit", "after"),
    "get_vault": ("vault_account_id",),
    "list_transactions": ("after", "before", "limit", "order_by", "sort", "assets", "status"),
    "list_audit_logs": ("start_time", "end_time", "page_size", "page_cursor", "order"),
    "list_tags": ("page_size",),
    "get_policy_v2": ("policy_type",),
    "get_policy_draft": ("policy_type",),
    "get_gas_station_asset": ("asset_id",),
    "update_policy_draft": ("update_draft_request", "idempotency_key"),
    "hide_vault": ("vault_account_id",),
    "unhide_vault": ("vault_account_id",),
    "delete_internal_wallet": ("wallet_id",),
    "delete_external_wallet": ("wallet_id",),
    "delete_network_connection": ("connection_id",),
    "delete_tag": ("tag_id",),
    "delete_console_user": ("id", "force"),
    "get_transaction": ("tx_id",),
    "estimate_fee": ("transaction_request",),
    "create_transaction": ("idempotency_key", "transaction_request"),
    "get_asset": ("id",),
    "list_blockchains": ("page_size", "page_cursor"),
    "create_vault_asset": ("vault_account_id", "asset_id"),
}

MUTATING_OPS = frozenset({
    "hide_vault", "unhide_vault", "delete_internal_wallet", "delete_external_wallet",
    "delete_network_connection", "delete_tag", "delete_console_user", "create_transaction",
    "create_vault_asset", "update_policy_draft",
})


REQUEST_TIMEOUT = 60  # seconds; the SDK has no default, and a dead socket otherwise hangs the process forever


class DryRun:
    """Returned by call() for a suppressed mutation."""
    def __repr__(self) -> str:
        return "<DRY_RUN>"


DRY_RUN = DryRun()


class ApiError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, code: Optional[str] = None, hint: str = ""):
        super().__init__(message)
        self.status, self.code, self.hint = status, code, hint

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base}\n  -> {self.hint}" if self.hint else base


class OpResolutionError(Exception):
    pass


def translate_error(exc: Exception) -> ApiError:
    """Map known failure modes to actionable text. Never suggests the force flag."""
    status = getattr(exc, "status", None)
    body = getattr(exc, "body", None) or ""
    text = f"{body} {exc}"
    code = None
    try:
        parsed = json.loads(body) if isinstance(body, str) and body.strip().startswith("{") else {}
        code = str(parsed.get("code")) if parsed.get("code") is not None else None
        msg = parsed.get("message") or str(exc)
    except ValueError:
        msg = str(exc)
    if "OWNER_ONLY" in text or code == "2760" or "2760" in text:
        return ApiError(f"Deletion refused: OWNER_ONLY (2760). {msg}", status, "2760",
                        "The workspace's 'Delete users' quorum is Owner-only. Review it in Settings > Quorums.")
    if "USER_REFERENCED_IN_TAP" in text:
        return ApiError(f"Deletion refused: USER_REFERENCED_IN_TAP. {msg}", status, "USER_REFERENCED_IN_TAP",
                        "Remove or repoint this user's references in the TAP policy first (a human publishes), then re-run.")
    if "USER_PENDING_ONBOARDING" in text:
        return ApiError(f"Deletion refused: USER_PENDING_ONBOARDING. {msg}", status, "USER_PENDING_ONBOARDING",
                        "This user never finished setup — there is nothing to delete. Revoke the invite in the console.")
    return ApiError(f"{type(exc).__name__}: {msg}", status, code)


def _unwrap(result: Any) -> Any:
    """SDK calls return Future[ApiResponse[Model]]; unwrap to the model."""
    if hasattr(result, "result") and callable(result.result):
        result = result.result()
    if hasattr(result, "data") and hasattr(result, "status_code"):
        result = result.data
    return result


def to_plain(obj: Any) -> Any:
    """Convert SDK pydantic models into plain dicts/lists (camelCase aliases preserved)."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(by_alias=True, mode="json")
    if hasattr(obj, "actual_instance") and obj.actual_instance is not None:
        return to_plain(obj.actual_instance)
    if isinstance(obj, (list, tuple)):
        return [to_plain(o) for o in obj]
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    return obj


class _RawRule:
    """Carries a rule dict through the SDK's serializer untouched. PolicyRule.from_dict rejects assetTypes ['*']
    (a oneOf bug in the generated models), so the draft is built without model validation."""
    def __init__(self, data: Dict[str, Any]):
        self._data = data

    def __bool__(self) -> bool:
        return True

    def to_dict(self) -> Dict[str, Any]:
        return self._data


def _coerce(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """The SDK wants model objects, not dicts; the journal keeps the readable dict."""
    tr = kwargs.get("transaction_request")
    if isinstance(tr, dict):
        from fireblocks.models.transaction_request import TransactionRequest
        return {**kwargs, "transaction_request": TransactionRequest.from_dict(tr)}
    ud = kwargs.get("update_draft_request")
    if isinstance(ud, dict):
        from fireblocks.models.policy_type import PolicyType
        from fireblocks.models.update_draft_request import UpdateDraftRequest
        req = UpdateDraftRequest.model_construct(policy_types=[PolicyType(t) for t in ud["policyTypes"]],
                                                 rules=[_RawRule(r) for r in ud["rules"]])
        return {**kwargs, "update_draft_request": req}
    return kwargs


def public_methods(group_obj: Any) -> list:
    return sorted(n for n in dir(group_obj) if not n.startswith("_") and callable(getattr(group_obj, n, None)))


class FireblocksClient:
    def __init__(self, sdk: Any, *, dry_run: bool = True, run_id: str = "adhoc",
                 journal_path: Optional[str] = None, max_calls_per_second: float = 3.0,
                 is_production: bool = False, confirmed_prod: bool = False, sleep=time.sleep, clock=time.monotonic):
        if is_production and not confirmed_prod:
            raise PermissionError("production workspace requires --i-understand-this-is-prod")
        self.sdk, self.dry_run, self.run_id = sdk, dry_run, run_id
        self.journal_path = Path(journal_path) if journal_path else None
        self._min_interval = 1.0 / max_calls_per_second
        self._last_call = 0.0
        self._lock = threading.Lock()
        self._sleep, self._clock = sleep, clock

    @classmethod
    def connect(cls, api_key: str, secret_key: str, base_path: str, **kw: Any) -> "FireblocksClient":
        from fireblocks.base_path import BasePath
        from fireblocks.client import Fireblocks
        from fireblocks.client_configuration import ClientConfiguration

        paths = {"sandbox": BasePath.Sandbox, "us": BasePath.US, "eu": BasePath.EU, "eu2": BasePath.EU2}
        sdk = Fireblocks(ClientConfiguration(api_key=api_key, secret_key=secret_key, base_path=paths[base_path]))
        return cls(sdk, **kw)

    # -- internals ---------------------------------------------------------------------------
    def _resolve(self, op: str):
        group, candidates = OPS[op]
        group_obj = getattr(self.sdk, group, None)
        if group_obj is not None:
            for name in candidates:
                fn = getattr(group_obj, name, None)
                if callable(fn):
                    return fn
        exposed = public_methods(group_obj) if group_obj is not None else "<api group missing on SDK client>"
        raise OpResolutionError(f"op '{op}': none of {candidates} found on sdk.{group}. SDK exposes: {exposed}")

    def _throttle(self) -> None:
        with self._lock:
            wait = self._min_interval - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
            self._last_call = self._clock()

    def _journal(self, op: str, kwargs: Dict[str, Any], outcome: str, error: str = "") -> None:
        if not self.journal_path:
            return
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": int(time.time()), "run_id": self.run_id, "op": op, "kwargs": kwargs,
                 "dry_run": self.dry_run, "outcome": outcome, "error": error}
        with open(self.journal_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    # -- the choke point ---------------------------------------------------------------------
    def call(self, op: str, **kwargs: Any) -> Any:
        if op not in OPS:
            raise KeyError(f"unknown op '{op}' — add it to OPS deliberately")
        mutating = op in MUTATING_OPS
        if mutating and self.dry_run:
            self._journal(op, kwargs, "dry_run")
            return DRY_RUN
        fn = self._resolve(op)
        sdk_kwargs = dict(_coerce(kwargs), _request_timeout=REQUEST_TIMEOUT)
        attempts = 1 if mutating else 4  # never auto-retry a mutation
        for attempt in range(attempts):
            self._throttle()
            try:
                result = to_plain(_unwrap(fn(**sdk_kwargs)))
                if mutating:
                    self._journal(op, kwargs, "ok")
                return result
            except Exception as exc:  # noqa: BLE001 - translated below
                status = getattr(exc, "status", None)
                if not mutating and attempt < attempts - 1 and (status == 429 or (status or 0) >= 500):
                    self._sleep(2 ** attempt)
                    continue
                err = translate_error(exc)
                if mutating:
                    self._journal(op, kwargs, "error", str(err))
                raise err from exc
        raise AssertionError("unreachable")
