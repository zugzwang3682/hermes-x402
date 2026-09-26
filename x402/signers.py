"""The two signers behind one interface: wallets / balances / history / quote / sign.

- local (default): keys and ledger in <HERMES_HOME>/plugin-data/x402/signer, signed in this process. Easy, but the
  agent's terminal can read files under the Hermes home, so keep balances small.
- remote: a separate x402-signer container holds the keys; this process only asks it to sign.

Both enforce the same limits (x402signer/policy.py), including the top-up bucket for payments to a configured broker's
receiving address."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from .x402 import Signer as RemoteSigner, X402Error
    from .x402signer.policy import Limits, PolicyError
except ImportError:  # tests import the package as `x402`
    from x402.x402 import Signer as RemoteSigner, X402Error
    from x402.x402signer.policy import Limits, PolicyError


def _core_class():
    """The in-process signer needs eth-account; import it only when the built-in signer is actually used."""
    try:
        from .x402signer.server import Signer
    except ImportError as e:
        if "eth_account" in str(e):
            raise X402Error("the built-in signer needs eth-account: reinstall the plugin (hermes plugins install) or "
                            "set plugins.entries.x402.settings.signer: remote") from e
        from x402.x402signer.server import Signer
    return Signer

DEFAULT_LIMITS = {"per_payment_usd": 1.0, "per_day_usd": 5.0, "networks": ["eip155:8453"],
                  "topup_per_payment_usd": 20.0, "topup_per_day_usd": 20.0}


class LocalSigner:
    """In-process signer. `limits_fn` is called per request so broker payees and limit changes apply at once."""

    def __init__(self, data_dir: str | os.PathLike, limits_fn):
        self.data_dir = Path(data_dir)
        self.limits_fn = limits_fn
        self._core = _core_class()(str(self.data_dir), limits_fn())

    @property
    def keystore(self):
        return self._core.keystore

    def _fresh(self):
        self._core.limits = self.limits_fn()
        return self._core

    def wallets(self) -> dict:
        return self._fresh().wallets()

    def balances(self) -> dict:
        return self._fresh().balances()

    def history(self, limit: int = 10) -> dict:
        return {"history": self._core.ledger.recent(int(limit))}

    def quote(self, required: dict, network: str | None = None, wallet: str | None = None) -> dict:
        return self._fresh().quote({"required": required, "network": network, "wallet": wallet})

    def sign(self, required: dict, max_usd: float, network: str | None = None, wallet: str | None = None) -> dict:
        try:
            return self._fresh().sign({"required": required, "max_usd": max_usd, "network": network, "wallet": wallet})
        except (PolicyError, LookupError) as e:
            raise X402Error(str(e)) from e


def limits_from_settings(settings: dict, topup_payees: tuple[str, ...]) -> Limits:
    lim = {**DEFAULT_LIMITS, **(settings.get("limits") or {})}
    return Limits(float(lim["per_payment_usd"]), float(lim["per_day_usd"]), tuple(lim["networks"]),
                  float(lim["topup_per_payment_usd"]), float(lim["topup_per_day_usd"]), tuple(topup_payees))


def make_signer(settings: dict, data_dir: str | os.PathLike, topup_payees_fn):
    """settings: the plugin's settings dict. `signer: remote` + `signer_url` selects the container."""
    if (settings.get("signer") or "local") == "remote":
        return RemoteSigner(settings.get("signer_url") or os.environ.get("X402_SIGNER_URL") or "http://x402-signer:8402")
    return LocalSigner(Path(data_dir) / "signer", lambda: limits_from_settings(settings, topup_payees_fn()))
