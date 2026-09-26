"""Shared fixtures: a signer on a random port with a temp keystore, and a fake x402 server that verifies EIP-3009
signatures the way a facilitator does (adapted from gpu-lessor/tests/mock_facilitator.py). No chain, no funds."""
from __future__ import annotations

import base64
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "signer"), str(ROOT)]

from x402signer.evm import TYPES  # noqa: E402
from x402signer.policy import Limits  # noqa: E402
from x402signer.server import serve  # noqa: E402

BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO = "0x8194ED5B09F86333C6eb11338Ba60C74d810Cf8f"


def b64(o):
    return base64.b64encode(json.dumps(o).encode()).decode()


def unb64(s):
    return json.loads(base64.b64decode(s))


def requirement(units=100000, network="eip155:8453", asset=BASE_USDC):
    return {"scheme": "exact", "network": network, "amount": str(units), "asset": asset, "payTo": PAY_TO,
            "maxTimeoutSeconds": 300, "extra": {"name": "USD Coin", "version": "2"}}


def verify(payment: dict) -> str | None:
    """None when the payment is a valid authorization for what it claims to accept, else the reason."""
    req, p = payment["accepted"], payment["payload"]
    a = p["authorization"]
    domain = {"name": req["extra"]["name"], "version": req["extra"]["version"],
              "chainId": int(req["network"].split(":")[1]), "verifyingContract": req["asset"]}
    msg = {"from": a["from"], "to": a["to"], "value": int(a["value"]), "validAfter": int(a["validAfter"]),
           "validBefore": int(a["validBefore"]), "nonce": bytes.fromhex(a["nonce"][2:])}
    if Account.recover_message(encode_typed_data(domain, TYPES, msg), signature=p["signature"]).lower() != a["from"].lower():
        return "signature does not recover to authorization.from"
    if a["to"].lower() != req["payTo"].lower():
        return "wrong payTo"
    if int(a["value"]) != int(req["amount"]):
        return "wrong amount"
    if not int(a["validAfter"]) <= time.time() <= int(a["validBefore"]):
        return "outside validity window"
    return None


class FakeX402:
    """/free -> 200; /paid (any method) -> 402 unless paid; /bin -> paid binary; /v1 -> x402 v1 style 402."""

    def __init__(self):
        self.units = 100000
        self.offers = None
        self.payments = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                if self.path.startswith("/free"):
                    return self._send(200, {"free": True}, "application/json")
                if self.path.startswith("/v1"):
                    return self._send(402, {"x402Version": 1, "accepts": []}, "application/json")
                required = {"x402Version": 2, "error": "Payment required",
                            "resource": {"url": f"http://localhost{self.path}", "description": "test"},
                            "accepts": outer.offers or [requirement(outer.units)]}
                sig = self.headers.get("PAYMENT-SIGNATURE")
                if not sig:
                    return self._send(402, required, "application/json", {"PAYMENT-REQUIRED": b64(required)})
                payment = unb64(sig)
                err = verify(payment)
                if err:
                    required["error"] = err
                    return self._send(402, required, "application/json", {"PAYMENT-REQUIRED": b64(required)})
                outer.payments.append({"payment": payment, "body": body})
                settle = {"success": True, "transaction": "0x" + "ab" * 32, "network": payment["accepted"]["network"],
                          "payer": payment["payload"]["authorization"]["from"]}
                if self.path.startswith("/bin"):
                    return self._send(200, b"RIFF....WAVEfmt ", "audio/wav", {"PAYMENT-RESPONSE": b64(settle)})
                echo = json.loads(body) if body else None
                return self._send(200, {"ok": True, "echo": echo}, "application/json",
                                  {"PAYMENT-RESPONSE": b64(settle), "X-Charged-Usd": "0.1"})

            def _send(self, code, obj, ctype, headers=None):
                data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def signer_env(tmp_path):
    """A running signer with one generated wallet; returns (url, signer_obj_data_dir)."""
    from x402signer.wallets import Keystore
    Keystore(tmp_path).add_evm("hermes")
    limits = Limits(per_payment_usd=1.0, per_day_usd=0.35, networks=("eip155:8453",))
    httpd = serve(str(tmp_path), host="127.0.0.1", port=0, limits=limits)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
    httpd.shutdown()


@pytest.fixture
def fake_server():
    s = FakeX402()
    yield s
    s.httpd.shutdown()


@pytest.fixture
def plugin(signer_env, tmp_path, monkeypatch):
    from x402 import tools, x402
    monkeypatch.setattr(tools, "_signer", x402.Signer(signer_env[0]))
    monkeypatch.setattr(tools, "DOWNLOAD_DIR", tmp_path / "downloads")
    return tools


# ---------------------------------------------------------------------------------------------------------------------
# A fake x402 broker with the Autumn Eight broker's balance path: x402 top-up -> JWT, metered chat with Bearer.

def fake_jwt(wallet: str, exp: float) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"sub": wallet, "exp": exp}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


MODELS = [
    {"id": "gpt-oss-120b", "kind": "chat", "max_context": 131072, "max_context_per_call": 16384,
     "price_in_per_mtok": 0.2, "price_out_per_mtok": 0.8, "tools": True, "available": True},
    {"id": "qwen3-8b", "kind": "chat", "max_context": 8192, "max_context_per_call": 8192,
     "price_in_per_mtok": 0.05, "price_out_per_mtok": 0.15, "tools": None, "available": True},
    {"id": "bge-m3", "kind": "embed"},
]


class FakeBroker:
    def __init__(self):
        self.balances, self.tokens, self.chats, self.topups = {}, {}, [], []
        self.charge = 0.01
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj, headers=None):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _wallet(self):
                tok = self.headers.get("Authorization", "")[7:]
                w = outer.tokens.get(tok)
                if not w or w[1] < time.time():
                    return None
                return w[0]

            def do_GET(self):
                if self.path == "/.well-known/broker.json":
                    return self._json(200, {"doc": {"broker": {"id": "fake", "payment_mode": "x402", "topup": outer.url + "/v1/topup",
                                                               "accepts": [{"network": "eip155:8453", "payTo": PAY_TO,
                                                                            "asset": BASE_USDC}]}}})
                if self.path == "/v1/models":
                    return self._json(200, {"object": "list", "data": MODELS})
                if self.path == "/v1/balance":
                    w = self._wallet()
                    if not w:
                        return self._json(401, {"detail": {"error": "invalid or expired token"}})
                    return self._json(200, {"wallet": w, "balance_usd": outer.balances[w]})
                return self._json(404, {"detail": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                if self.path.startswith("/v1/topup"):
                    usd = float(self.path.split("usd=")[1])
                    required = {"x402Version": 2, "resource": {"url": outer.url + self.path},
                                "accepts": [requirement(int(round(usd * 1e6)))]}
                    sig = self.headers.get("PAYMENT-SIGNATURE")
                    if not sig:
                        return self._json(402, required, {"PAYMENT-REQUIRED": b64(required)})
                    payment = unb64(sig)
                    if verify(payment):
                        return self._json(402, required, {"PAYMENT-REQUIRED": b64(required)})
                    w = payment["payload"]["authorization"]["from"]
                    outer.balances[w] = outer.balances.get(w, 0) + usd
                    tok = fake_jwt(w, time.time() + 30 * 86400)
                    outer.tokens[tok] = (w, time.time() + 30 * 86400)
                    outer.topups.append(usd)
                    return self._json(200, {"wallet": w, "balance_usd": outer.balances[w], "jwt": tok, "receipt": {}},
                                      {"PAYMENT-RESPONSE": b64({"success": True, "transaction": "0x" + "ef" * 32})})
                if self.path == "/v1/chat/completions":
                    if not self.headers.get("Authorization"):
                        return self._json(402, {"x402Version": 2, "accepts": [requirement(5000)]})
                    w = self._wallet()
                    if not w:
                        return self._json(401, {"detail": {"error": "invalid or expired token"}})
                    if outer.balances[w] < outer.charge:
                        return self._json(402, {"detail": {"error": "insufficient balance", "code": "insufficient_balance",
                                                           "balance_usd": outer.balances[w]}})
                    req = json.loads(body)
                    outer.chats.append({"model": req["model"], "auth": self.headers.get("Authorization")})
                    if req.get("stream"):
                        # like the real broker: headers (with the pre-charge balance) go out, then the charge lands
                        pre = outer.balances[w]
                        outer.balances[w] -= outer.charge
                        chunk = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": req["model"],
                                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": "pong"},
                                              "finish_reason": "stop"}]}
                        data = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("X-Balance-Usd", f"{pre:.8f}")
                        self.end_headers()
                        return self.wfile.write(data)
                    outer.balances[w] -= outer.charge
                    return self._json(200, {"id": "c", "object": "chat.completion", "created": 0, "model": req["model"],
                                            "choices": [{"index": 0, "finish_reason": "stop",
                                                         "message": {"role": "assistant", "content": "pong"}}],
                                            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}},
                                      {"X-Charged-Usd": f"{outer.charge:.8f}", "X-Balance-Usd": f"{outer.balances[w]:.8f}"})
                return self._json(404, {"detail": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.setenv("X402_ALLOW_HTTP", "1")
    b = FakeBroker()
    from x402 import brokers
    brokers._cache.clear()
    yield b
    b.httpd.shutdown()
