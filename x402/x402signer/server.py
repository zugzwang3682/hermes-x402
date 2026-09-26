"""The signer's HTTP API, for the Hermes x402 plugin on the private compose network. It never returns a key.

  GET  /health
  GET  /wallets            wallets (name, address), limits, spend in the last 24 hours
  GET  /balances           on-chain USDC per wallet and enabled network
  GET  /history?limit=20   recent signatures
  POST /quote              {"required": ..., "network"?: "..."} -> what /sign would pay, and whether the limits allow it
  POST /sign               {"required": <the server's PAYMENT-REQUIRED>, "network"?: "...", "wallet"?: "..."}
                           -> {"payment": <PAYMENT-SIGNATURE payload>, "usd", "network", "wallet", "address", ...}
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import evm
from .networks import ASSETS, NAMES, family, find_asset
from .policy import DAY, Ledger, Limits, PolicyError, check
from .wallets import Keystore

log = logging.getLogger("x402signer")


class Signer:
    def __init__(self, data_dir: str, limits: Limits):
        self.keystore = Keystore(data_dir)
        self.ledger = Ledger(data_dir)
        self.limits = limits
        self.lock = threading.Lock()  # one signature at a time, so two requests cannot both fit under the daily cap

    def wallets(self) -> dict:
        now = time.time()
        return {"wallets": [w.public() for w in self.keystore.list()], "limits": self.limits.public(),
                "spent_24h_usd": round(self.ledger.spent_usd(now - DAY), 6)}

    def balances(self) -> dict:
        out = []
        for w in self.keystore.list():
            for net in self.limits.networks:
                if family(net) != w.family:
                    continue
                for a in (a for a in ASSETS if a.network == net):
                    out.append({"wallet": w.name, "address": w.address, "network": net, "chain": NAMES.get(net, net),
                                "asset": a.symbol, "balance": evm.usdc_balance(a, w.address)})
        return {"balances": out}

    def choose(self, accepts: list, network: str | None) -> tuple[dict, object]:
        """The first requirement we can pay: exact scheme, an enabled network (the one asked for, if any), a known asset."""
        seen = []
        for req in accepts or []:
            net = str(req.get("network", ""))
            seen.append(f"{req.get('scheme')}@{net}")
            if req.get("scheme") != "exact" or net not in self.limits.networks:
                continue
            if network and net != network:
                continue
            asset = find_asset(net, req.get("asset", ""))
            if asset and family(net) == "evm":
                return req, asset
        wanted = f" on {network}" if network else ""
        raise PolicyError(f"nothing payable{wanted}: the server offers {seen or 'no options'}, "
                          f"this signer pays known USDC with the exact scheme on {', '.join(self.limits.networks)}")

    def _requirement(self, body: dict):
        required = body.get("required") or {}
        if required.get("x402Version") != 2:
            raise PolicyError(f"server speaks x402Version {required.get('x402Version')}; this signer speaks 2")
        req, asset = self.choose(required.get("accepts"), body.get("network"))
        return required, req, asset

    def quote(self, body: dict) -> dict:
        """What /sign would pay for this 402, without signing. Never raises on policy: it reports."""
        try:
            _, req, asset = self._requirement(body)
        except PolicyError as e:
            return {"payable": False, "reason": str(e)}
        usd = asset.units_to_usd(int(req["amount"]))
        out = {"usd": usd, "network": req["network"], "chain": NAMES.get(req["network"], req["network"]),
               "pay_to": req["payTo"], "asset": asset.symbol, "testnet": asset.testnet}
        try:
            wallet = self.keystore.get(body.get("wallet"), family(req["network"]))
            out["wallet"], out["address"] = wallet.name, wallet.address
            check(self.limits, self.ledger, req["network"], usd, req["payTo"])
            return {"payable": True, **out}
        except (PolicyError, LookupError) as e:
            return {"payable": False, "reason": str(e), **out}

    def sign(self, body: dict) -> dict:
        required, req, asset = self._requirement(body)
        units = int(req["amount"])
        usd = asset.units_to_usd(units)
        max_usd = body.get("max_usd")
        if max_usd is not None and usd > float(max_usd) + 1e-9:
            raise PolicyError(f"the server asks ${usd:.6f}, above the caller's ceiling of ${float(max_usd):.6f}")
        wallet = self.keystore.get(body.get("wallet"), family(req["network"]))
        with self.lock:
            check(self.limits, self.ledger, req["network"], usd, req["payTo"])
            payload = evm.sign_exact(wallet._key, wallet.address, req, asset)
            resource = required.get("resource")
            self.ledger.record(ts=time.time(), wallet=wallet.name, address=wallet.address, network=req["network"],
                               asset=asset.address, pay_to=req["payTo"], units=units, usd=usd,
                               resource=json.dumps(resource) if isinstance(resource, dict) else resource,
                               nonce=payload["authorization"]["nonce"])
        log.info("signed %.6f USD on %s from %s to %s", usd, req["network"], wallet.name, req["payTo"])
        return {"payment": {"x402Version": 2, "resource": resource, "accepted": req, "payload": payload},
                "usd": usd, "network": req["network"], "chain": NAMES.get(req["network"], req["network"]),
                "wallet": wallet.name, "address": wallet.address, "pay_to": req["payTo"],
                "explorer_tx": asset.explorer_tx, "testnet": asset.testnet}


def make_handler(signer: Signer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "x402-signer/0.1"

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _send(self, code: int, obj: dict):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            try:
                if u.path == "/health":
                    return self._send(200, {"ok": True})
                if u.path == "/wallets":
                    return self._send(200, signer.wallets())
                if u.path == "/balances":
                    return self._send(200, signer.balances())
                if u.path == "/history":
                    n = int(parse_qs(u.query).get("limit", ["20"])[0])
                    return self._send(200, {"history": signer.ledger.recent(min(max(n, 1), 200))})
                return self._send(404, {"error": "not found"})
            except Exception as e:
                log.exception("GET %s", u.path)
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def do_POST(self):
            path = urlparse(self.path).path
            if path not in ("/sign", "/quote"):
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 65536:
                    return self._send(413, {"error": "request too large"})
                body = json.loads(self.rfile.read(n) or b"{}")
                return self._send(200, signer.sign(body) if path == "/sign" else signer.quote(body))
            except (PolicyError, LookupError) as e:
                return self._send(403, {"error": str(e)})
            except (ValueError, KeyError, TypeError) as e:
                return self._send(400, {"error": f"bad request: {type(e).__name__}: {e}"})
            except Exception as e:
                log.exception("sign")
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    return Handler


def serve(data_dir: str, host: str = "0.0.0.0", port: int = 8402, limits: Limits | None = None) -> ThreadingHTTPServer:
    signer = Signer(data_dir, limits or Limits.from_env())
    httpd = ThreadingHTTPServer((host, port), make_handler(signer))
    log.info("x402 signer on %s:%d, limits %s, %d wallet(s)", host, port, signer.limits.public(),
             len(signer.keystore.list()))
    return httpd


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    serve(os.environ.get("X402_DATA", "/data"), port=int(os.environ.get("PORT", "8402"))).serve_forever()
