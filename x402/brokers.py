"""Brokers: x402 inference brokers the user can top up and run models on.

Settings (plugins.entries.x402.settings):
  brokers: {name: {url}}          the Autumn Eight broker is preconfigured; add or replace freely
  active_broker: name
  default_model: gpt-oss-120b     set as the main model after a top-up when none is configured

State (plugin-data/x402/state.json, per profile): broker.<name>.jwt / .wallet / .balance_usd / .updated.
The x402-broker model provider reads the same state file and settings, so a top-up here is live there at once."""
from __future__ import annotations

import base64
import json
import time

import httpx

DEFAULT_BROKERS = {"autumn8": {"url": "https://x402.autumn8.net"}}
DEFAULT_ACTIVE = "autumn8"
DEFAULT_MODEL = "gpt-oss-120b"
MIN_AGENT_CONTEXT = 64000  # Hermes refuses to run an agent on less
_CACHE_TTL = 300
_cache: dict = {}


def brokers(settings: dict) -> dict:
    return dict(settings.get("brokers") or DEFAULT_BROKERS)


def active_name(settings: dict) -> str:
    name = settings.get("active_broker") or DEFAULT_ACTIVE
    return name if name in brokers(settings) else next(iter(brokers(settings)))


def url_of(settings: dict, name: str | None = None) -> str:
    name = name or active_name(settings)
    b = brokers(settings).get(name)
    if not b:
        raise LookupError(f"no broker named {name!r}; see `x402 broker list`")
    return str(b["url"]).rstrip("/")


def _get(url: str, headers: dict | None = None, timeout: float = 15) -> dict:
    r = httpx.get(url, headers=headers or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _cached(key: str, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def offer(url: str) -> dict:
    """/.well-known/broker.json `doc`: payment mode, accepted networks and receiving addresses, top-up URL."""
    return _cached("offer:" + url, lambda: _get(url + "/.well-known/broker.json")["doc"])


def models(url: str) -> list[dict]:
    """Chat models from /v1/models, with the balance window (`max_context`) and per-call cap."""
    def load():
        d = _get(url + "/v1/models")
        rows = d.get("data", d) if isinstance(d, dict) else d
        return [m for m in rows if m.get("kind", "chat") == "chat"]
    return _cached("models:" + url, load)


def payees(url: str) -> tuple[str, ...]:
    try:
        return tuple(a["payTo"] for a in offer(url).get("broker", {}).get("accepts", []) if a.get("payTo"))
    except Exception:
        return ()


def all_payees(settings: dict) -> tuple[str, ...]:
    out: list[str] = []
    for b in brokers(settings).values():
        out += payees(str(b["url"]).rstrip("/"))
    return tuple(out)


def validate(url: str) -> dict:
    """Refuse a URL that is not an x402 broker with a top-up endpoint."""
    doc = offer(url)
    b = doc.get("broker") or {}
    if b.get("payment_mode") != "x402":
        raise ValueError(f"{url} is not in x402 payment mode (payment_mode={b.get('payment_mode')!r})")
    if not b.get("topup"):
        raise ValueError(f"{url} advertises no top-up endpoint, so balance mode cannot work")
    if not b.get("accepts"):
        raise ValueError(f"{url} lists no payment networks")
    return doc


def jwt_expiry(jwt: str) -> float | None:
    try:
        body = jwt.split(".")[1]
        return float(json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))["exp"])
    except Exception:
        return None


class Store:
    """Per-broker balance state on top of a get/set store (Hermes ctx.state, or a dict in tests)."""

    def __init__(self, state):
        self.state = state

    def get(self, name: str) -> dict:
        return {k: self.state.get(f"broker.{name}.{k}") for k in ("jwt", "wallet", "balance_usd", "updated")}

    def put(self, name: str, **fields):
        for k, v in fields.items():
            self.state.set(f"broker.{name}.{k}", v)
        self.state.set(f"broker.{name}.updated", time.time())


def agent_models(url: str) -> list[dict]:
    return [m for m in models(url) if int(m.get("max_context") or 0) >= MIN_AGENT_CONTEXT and m.get("available", True)]


def model_row(url: str, model: str) -> dict | None:
    return next((m for m in models(url) if m["id"] == model), None)
