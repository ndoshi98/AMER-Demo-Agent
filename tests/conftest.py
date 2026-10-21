from concurrent.futures import Future

import pytest

from fbcleanup.client import FireblocksClient


class _Group:
    def __init__(self, log, group, responses):
        self._log, self._group, self._responses = log, group, responses

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if (self._group, name) not in self._responses:
            raise AttributeError(name)

        def call(**kw):
            assert kw.pop("_request_timeout", None), "every SDK call must carry a request timeout"
            self._log.append((self._group, name, kw))
            val = self._responses[(self._group, name)]
            val = val(**kw) if callable(val) else val
            f = Future()
            f.set_result(val)  # mimic the SDK's Future[...] return
            return f

        return call

    def __dir__(self):
        return [n for (g, n) in self._responses if g == self._group]


class FakeSDK:
    GROUPS = ("vaults", "console_user", "external_wallets", "internal_wallets", "network_connections", "tags",
              "workspace", "transactions", "users", "audit_logs", "policy_editor_v2", "policy_editor_beta", "api_user", "blockchains_assets")

    def __init__(self, responses=None):
        self.log = []
        self.responses = responses or {}
        for g in self.GROUPS:
            setattr(self, g, _Group(self.log, g, self.responses))

    def calls(self, name=None):
        return [c for c in self.log if name is None or c[1] == name]


@pytest.fixture
def make_client(tmp_path):
    def _make(responses=None, dry_run=True, run_id="r1"):
        sdk = FakeSDK(responses)
        cl = FireblocksClient(sdk, dry_run=dry_run, run_id=run_id, journal_path=str(tmp_path / "journal.jsonl"),
                              max_calls_per_second=1000)
        return cl, sdk

    return _make
