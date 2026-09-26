"""x402-broker: run Hermes on models from an x402 inference broker, billed to the balance `/x402 topup` bought.

Companion to the x402 plugin, which owns the settings (plugins.entries.x402.settings: brokers, active_broker) and the
state file (<HERMES_HOME>/plugin-data/x402/state.json: broker.<name>.jwt). Context windows are the broker's balance
windows from /v1/models, so Hermes compresses history to fit and refuses models under its 64K minimum."""
import json
import logging
import time

from providers import register_provider
from providers.base import ProviderProfile

try:
    from .transport import STATS, make_client
except ImportError:
    from transport import STATS, make_client

logger = logging.getLogger(__name__)
DEFAULT_BROKERS = {"autumn8": {"url": "https://x402.autumn8.net"}}
_models: dict = {}


def _settings() -> dict:
    try:
        from hermes_cli.config import load_config_readonly
        entry = (((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}).get("x402") or {}
        return entry.get("settings") or entry.get("config") or {}
    except Exception:
        return {}


def _state_path():
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir("x402") / "state.json"


def _account():
    s = _settings()
    brokers = s.get("brokers") or DEFAULT_BROKERS
    name = s.get("active_broker") if s.get("active_broker") in brokers else next(iter(brokers))
    url = str(brokers[name]["url"]).rstrip("/")
    try:
        jwt = json.loads(_state_path().read_text()).get(f"broker.{name}.jwt")
    except Exception:
        jwt = None
    return name, url, jwt


def _on_balance(name: str, balance: float) -> None:
    """Keep the last known balance in the shared state file (best effort, same format the x402 plugin writes)."""
    try:
        p = _state_path()
        d = json.loads(p.read_text()) if p.exists() else {}
        d[f"broker.{name}.balance_usd"] = balance
        d[f"broker.{name}.updated"] = time.time()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.chmod(0o600)
        tmp.replace(p)
    except Exception:
        pass


def _catalog(url: str) -> dict:
    hit = _models.get(url)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    import httpx
    try:
        d = httpx.get(url + "/v1/models", timeout=10).json()
        rows = {m["id"]: m for m in d.get("data", d) if m.get("kind", "chat") == "chat"}
        _models[url] = (time.time(), rows)
        return rows
    except Exception:
        return hit[1] if hit else {}


class X402BrokerProfile(ProviderProfile):
    def create_client(self, **client_kwargs):
        return make_client(_account, _on_balance, **client_kwargs)

    def get_model_context_length(self, model):
        return int(_catalog(_account()[1]).get(model, {}).get("max_context") or 0) or None

    def fetch_models(self, **kwargs):
        rows = _catalog(_account()[1])
        return sorted(m for m, r in rows.items() if int(r.get("max_context") or 0) >= 64000) or None

    def get_usage_cost(self, model, usage):
        from decimal import Decimal
        from agent.usage_pricing import CostResult, format_cost_label
        row = _catalog(_account()[1]).get(model)
        if not row:
            return CostResult(amount_usd=None, status="unknown", source="none", label="n/a",
                              notes=("model not in the broker's catalog",))
        pin = Decimal(str(row.get("price_in_per_mtok") or 0))
        pout = Decimal(str(row.get("price_out_per_mtok") or 0))
        amount = (pin * Decimal(getattr(usage, "input_tokens", 0) or 0)
                  + pout * Decimal(getattr(usage, "output_tokens", 0) or 0)) / Decimal(1_000_000)
        return CostResult(amount_usd=amount, status="estimated", source="provider_cost_api",
                          label=format_cost_label(amount),
                          notes=(f"x402 broker list price; charged ${STATS['charged_usd']:.4f} over {STATS['calls']} "
                                 f"metered calls this process",))


profile = X402BrokerProfile(
    name="x402-broker",
    display_name="x402 broker (prepaid balance)",
    description="Open models on an x402 inference broker, billed per token to a USDC balance topped up with /x402 topup",
    api_mode="chat_completions",
    auth_type="api_key",
    env_vars=("X402_BROKER_API_KEY",),  # placeholder written by /x402; the transport sends the balance token instead
    base_url=_account()[1] + "/v1",
    supports_health_check=False,
    default_aux_model="gpt-oss-120b",
    fallback_models=("gpt-oss-120b", "gpt-oss-20b", "gemma-3-27b", "deepseek-v4-flash", "glm-5.2", "deepseek-v4-pro"),
)
register_provider(profile)
