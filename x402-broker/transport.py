"""An OpenAI client for an x402 broker's balance mode: every request carries the balance token (JWT) that `/x402 topup`
stored, read fresh per request, and goes to the broker that is active at that moment. The broker meters per token and
reports `X-Charged-Usd` / `X-Balance-Usd`. This client never pays: when the balance runs out it fails with a message
that says how to top up. No Hermes imports, so tests can drive it directly."""
from __future__ import annotations

import json
import threading
from typing import Callable
from urllib.parse import urlparse

import httpx

STATS = {"calls": 0, "charged_usd": 0.0, "balance_usd": None}
_lock = threading.Lock()


def _error(request: httpx.Request, status: int, message: str, code: str) -> httpx.Response:
    body = json.dumps({"error": {"message": message, "type": "x402_balance", "code": code}}).encode()
    return httpx.Response(status, headers={"content-type": "application/json"}, content=body, request=request)


class BalanceTransport(httpx.BaseTransport):
    """`account()` returns (broker_name, broker_url, jwt | None) for the active broker, per request."""

    def __init__(self, account: Callable[[], tuple[str, str, str | None]], on_balance: Callable | None = None,
                 inner: httpx.BaseTransport | None = None):
        self.account, self.on_balance = account, on_balance
        self.inner = inner or httpx.HTTPTransport(retries=0)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        name, base, jwt = self.account()
        if not jwt:
            return _error(request, 402, f"no x402 balance on {name} yet — run `x402 topup <usd>` "
                                        f"(Slack: !x402 topup 5)", "no_balance")
        b = urlparse(base)
        path = request.url.path
        i = path.find("/v1/")
        url = request.url.copy_with(scheme=b.scheme, host=b.hostname, port=b.port,
                                    path=(b.path.rstrip("/") + path[i:]) if i >= 0 else path)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("authorization", "host")}
        headers["Authorization"] = f"Bearer {jwt}"
        out = httpx.Request(request.method, url, headers=headers, content=request.read(), extensions=request.extensions)
        resp = self.inner.handle_request(out)
        charged, bal = resp.headers.get("x-charged-usd"), resp.headers.get("x-balance-usd")
        if "text/event-stream" in resp.headers.get("content-type", ""):
            # A streamed response's headers go out before the request is metered, so they show the balance before
            # this charge. Only non-streamed responses carry a settled balance; `x402 balance` asks the broker.
            charged = bal = None
        if charged or bal:
            with _lock:
                STATS["calls"] += 1
                STATS["charged_usd"] += float(charged or 0)
                if bal is not None:
                    STATS["balance_usd"] = float(bal)
            if bal is not None and self.on_balance:
                try:
                    self.on_balance(name, float(bal))
                except Exception:
                    pass
        if resp.status_code in (401, 402):
            resp.read()
            try:
                detail = resp.json()
                detail = detail.get("detail", detail)
            except ValueError:
                detail = {}
            detail = detail if isinstance(detail, dict) else {"error": str(detail)}
            if resp.status_code == 401:
                return _error(request, 401, f"the x402 balance token for {name} expired — run `x402 topup <usd>` "
                                            f"(any amount ≥ $0.10 renews it)", "token_expired")
            if detail.get("code") == "insufficient_balance" or "balance" in str(detail.get("error", "")):
                left = detail.get("balance_usd")
                return _error(request, 402, f"x402 balance {'$%.4f ' % left if left is not None else ''}on {name} is too "
                                            f"low for this request — run `x402 topup <usd>` (Slack: !x402 topup 5)",
                              "insufficient_balance")
        return resp

    def close(self) -> None:
        self.inner.close()


_OPENAI_KWARGS = ("api_key", "organization", "project", "base_url", "timeout", "max_retries", "default_headers",
                  "default_query")


def make_client(account, on_balance=None, inner: httpx.BaseTransport | None = None, **client_kwargs):
    """openai.OpenAI whose requests go to the active broker with the balance token. Ignores unknown kwargs."""
    import openai
    kw = {k: v for k, v in client_kwargs.items() if k in _OPENAI_KWARGS and v is not None}
    kw["api_key"] = kw.get("api_key") or "x402-balance"  # replaced per request by the JWT
    kw["base_url"] = kw.get("base_url") or account()[1] + "/v1"
    kw["http_client"] = httpx.Client(transport=BalanceTransport(account, on_balance, inner),
                                     timeout=kw.get("timeout") or 600)
    return openai.OpenAI(**kw)
