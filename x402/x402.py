"""x402 v2 client for the Hermes plugin. It speaks HTTP to the paid server and asks the signer for signatures;
it never holds a key. No Hermes imports here, so tests can drive it directly.

Flow: send the request; on 402 read PAYMENT-REQUIRED (header, else body), ask the signer to sign, resend with
PAYMENT-SIGNATURE, and read the settlement from PAYMENT-RESPONSE."""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field

import httpx

SIGNER_URL = os.environ.get("X402_SIGNER_URL", "http://x402-signer:8402").rstrip("/")
USER_AGENT = "hermes-x402/0.1"


class X402Error(Exception):
    pass


def b64(obj) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def unb64(s: str):
    return json.loads(base64.b64decode(s))


@dataclass
class Result:
    response: httpx.Response
    required: dict | None = None      # the 402 the server sent, when there was one
    signed: dict | None = None        # what the signer returned (usd, network, wallet, ...)
    settlement: dict | None = None    # the server's PAYMENT-RESPONSE
    notes: list = field(default_factory=list)


class Signer:
    def __init__(self, url: str = SIGNER_URL, timeout: float = 20):
        self.url, self.timeout = url.rstrip("/"), timeout

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            r = httpx.request(method, self.url + path, json=body, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise X402Error(f"cannot reach the x402 signer at {self.url}: {e}") from e
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"error": r.text}
        if r.status_code != 200:
            raise X402Error(data.get("error") or f"signer returned HTTP {r.status_code}")
        return data

    def wallets(self) -> dict:
        return self._call("GET", "/wallets")

    def balances(self) -> dict:
        return self._call("GET", "/balances")

    def history(self, limit: int = 10) -> dict:
        return self._call("GET", f"/history?limit={int(limit)}")

    def quote(self, required: dict, network: str | None = None, wallet: str | None = None) -> dict:
        return self._call("POST", "/quote", {"required": required, "network": network, "wallet": wallet})

    def sign(self, required: dict, max_usd: float, network: str | None = None, wallet: str | None = None) -> dict:
        return self._call("POST", "/sign", {"required": required, "max_usd": max_usd, "network": network,
                                            "wallet": wallet})


def payment_required(r: httpx.Response) -> dict:
    hdr = r.headers.get("payment-required")
    if hdr:
        try:
            return unb64(hdr)
        except Exception as e:
            raise X402Error(f"unreadable PAYMENT-REQUIRED header: {e}") from e
    try:
        body = r.json()
    except Exception:
        raise X402Error("the server answered 402 without x402 payment requirements (no PAYMENT-REQUIRED header, "
                        "no JSON body)")
    if body.get("x402Version") == 1:
        raise X402Error("the server speaks x402 v1 (X-PAYMENT); only v2 is supported")
    return body


def _send(client: httpx.Client, method: str, url: str, headers: dict, content: bytes | None) -> httpx.Response:
    # Redirects are not followed: a redirect after a 402 would carry the payment header to another URL.
    return client.request(method, url, headers=headers, content=content)


def probe(method: str, url: str, headers: dict | None = None, content: bytes | None = None,
          timeout: float = 60) -> Result:
    """Send the request unpaid. A 402 comes back with its requirements parsed; anything else is returned as is."""
    h = {"User-Agent": USER_AGENT, **(headers or {})}
    with httpx.Client(timeout=timeout, follow_redirects=False) as c:
        r = _send(c, method, url, h, content)
    return Result(r, payment_required(r) if r.status_code == 402 else None)


def fetch(method: str, url: str, max_usd: float, signer: Signer, headers: dict | None = None,
          content: bytes | None = None, network: str | None = None, wallet: str | None = None,
          timeout: float = 120) -> Result:
    """Request, and pay once if the server answers 402 with a price at or under max_usd."""
    h = {"User-Agent": USER_AGENT, **(headers or {})}
    with httpx.Client(timeout=timeout, follow_redirects=False) as c:
        r = _send(c, method, url, h, content)
        if r.status_code != 402:
            return Result(r)
        required = payment_required(r)
        signed = signer.sign(required, max_usd=max_usd, network=network, wallet=wallet)
        r2 = _send(c, method, url, {**h, "PAYMENT-SIGNATURE": b64(signed["payment"])}, content)
    res = Result(r2, required, signed)
    pr = r2.headers.get("payment-response")
    if pr:
        try:
            res.settlement = unb64(pr)
        except Exception:
            res.notes.append("PAYMENT-RESPONSE header was unreadable")
    if r2.status_code == 402:
        try:
            again = payment_required(r2)
            reason = again.get("error") or again.get("reason") or "no reason given"
        except X402Error:
            reason = r2.text[:300]
        res.notes.append(f"the server refused the payment: {reason}. The signature was issued, so the signer counts "
                         "it against the daily limit even if it never settles.")
    return res
