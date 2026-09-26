"""Hermes implementation of commands.Host, plus the JSON state file shared with the x402-broker provider.

State lives in <HERMES_HOME>/plugin-data/x402/state.json (plugins/plugin_storage.plugin_data_dir, an unhashed path the
provider plugin can find). Settings live in config.yaml under plugins.entries.x402.settings."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

try:
    from .signers import make_signer
    from . import brokers as B
except ImportError:
    from x402.signers import make_signer
    from x402 import brokers as B

PROVIDER = "x402-broker"
PLACEHOLDER_ENV = "X402_BROKER_API_KEY"
PLACEHOLDER = "x402-balance"
SETTING_KEYS = ("brokers", "active_broker", "default_model", "signer", "signer_url", "limits")
_lock = threading.Lock()


def data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return Path(plugin_data_dir("x402"))


class JsonState:
    """Tiny atomic JSON key/value file (same shape the provider reads)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    def get(self, key: str, default=None):
        return self._read().get(key, default)

    def set(self, key: str, value) -> None:
        with _lock:
            d = self._read()
            d[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
            with os.fdopen(fd, "w") as f:
                json.dump(d, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)


class HermesHost:
    interactive = False

    def __init__(self, ctx, interactive: bool = False):
        self.ctx = ctx
        self.interactive = interactive

    # settings / state
    def settings(self) -> dict:
        return {k: v for k in SETTING_KEYS if (v := self.ctx.get_config(k)) is not None}

    def set_setting(self, key: str, value) -> None:
        self.ctx.set_config(key, value)

    @property
    def state(self) -> JsonState:
        return JsonState(data_dir() / "state.json")

    def signer(self):
        s = self.settings()
        return make_signer(s, data_dir(), lambda: B.all_payees(s))

    # models
    def _config(self) -> dict:
        from hermes_cli.config import load_config_readonly
        return load_config_readonly() or {}

    def main_model(self) -> tuple[str, str]:
        m = self._config().get("model") or {}
        if isinstance(m, str):
            return "", m
        return str(m.get("provider") or ""), str(m.get("default") or m.get("model") or "")

    def main_model_usable(self) -> bool:
        prov, model = self.main_model()
        if not prov or prov == "auto" and not model:
            return False
        try:
            from hermes_cli.runtime_provider import resolve_runtime_provider
            rt = resolve_runtime_provider(requested=prov, target_model=model or None)
            return bool(rt.get("api_key") or rt.get("command") or rt.get("base_url", "").startswith("process://"))
        except Exception:
            return False

    def _model_block(self) -> dict:
        m = self._config().get("model") or {}
        return {k: m.get(k) for k in ("provider", "default", "base_url", "api_mode")} if isinstance(m, dict) else {}

    def restore_main_model(self) -> str:
        prev = self.state.get("previous_main_model")
        if not prev or not prev.get("provider"):
            return "no earlier main model recorded; pick one with /model"
        for k, v in prev.items():
            self._write(f"model.{k}", v if v is not None else "")
        self.state.set("previous_main_model", None)
        return f"Main model restored to {prev.get('default')} ({prev.get('provider')})."

    def set_main_model(self, model: str) -> str:
        self.ensure_placeholder()
        prov, cur = self.main_model()
        if prov and prov != PROVIDER:
            self.state.set("previous_main_model", self._model_block())
        try:
            from hermes_cli.model_switch import persist_model_selection, switch_model
            r = switch_model(model, current_provider=prov, current_model=cur, is_global=True, explicit_provider=PROVIDER)
            if r.success:
                persist_model_selection(r)
                note = f" ({r.warning_message})" if r.warning_message else ""
                return (f"Main model set to {model} on the x402 broker{note}. New messages use it; "
                        f"a chat that ran /model keeps its own choice until /new.")
            err = r.error_message
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        self._write("model.provider", PROVIDER)
        self._write("model.default", model)
        return f"Main model set to {model} on the x402 broker (direct config write; model switch said: {err})."

    def subagent_model(self) -> tuple[str, str]:
        d = self._config().get("delegation") or {}
        return str(d.get("provider") or ""), str(d.get("model") or "")

    def set_subagents(self, model: str) -> str:
        if model is None:
            self._write("delegation.provider", "")
            self._write("delegation.model", "")
            return "Sub-agents now use the main model again."
        self._write("delegation.provider", PROVIDER)
        self._write("delegation.model", model)
        return f"Sub-agents now run on {model} on the x402 broker, billed to the balance."

    def _write(self, key: str, value) -> None:
        from hermes_cli.config import get_config_path
        from utils import atomic_roundtrip_yaml_update
        atomic_roundtrip_yaml_update(get_config_path(), key, value)

    def ensure_placeholder(self) -> None:
        """Hermes needs an API key for the provider before it will build a client; payment is the real credential, and
        the transport replaces this with the balance token. Multiplexed gateways read only the profile .env."""
        try:
            from agent.secret_scope import get_secret
            if get_secret(PLACEHOLDER_ENV):
                return
        except Exception:
            pass
        from hermes_cli.config import save_env_value
        save_env_value(PLACEHOLDER_ENV, PLACEHOLDER)
