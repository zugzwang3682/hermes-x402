"""x402 v2 "exact" scheme on EVM chains: an EIP-3009 TransferWithAuthorization signed off-chain.

The payer only signs; the server's facilitator submits the transfer and pays the gas, so a wallet needs USDC and
no ETH. Adapted from gpu-lessor/buyer/x402client.py."""
from __future__ import annotations

import json
import os
import secrets
import time
import urllib.request

from eth_account import Account

from .networks import DEFAULT_RPC, Asset

TYPES = {"TransferWithAuthorization": [
    {"name": "from", "type": "address"}, {"name": "to", "type": "address"}, {"name": "value", "type": "uint256"},
    {"name": "validAfter", "type": "uint256"}, {"name": "validBefore", "type": "uint256"},
    {"name": "nonce", "type": "bytes32"}]}

MAX_TIMEOUT_S = 600  # never sign an authorization that stays valid longer than this


def _hex(b) -> str:
    h = b.hex()
    return h if h.startswith("0x") else "0x" + h


def sign_exact(key: str, address: str, req: dict, asset: Asset, now: int | None = None) -> dict:
    """The scheme payload for one requirement: {signature, authorization}."""
    chain_id = int(req["network"].split(":")[1])
    now = int(time.time()) if now is None else now
    nonce = "0x" + secrets.token_hex(32)
    timeout = min(int(req.get("maxTimeoutSeconds") or 300), MAX_TIMEOUT_S)
    auth = {"from": address, "to": req["payTo"], "value": str(int(req["amount"])), "validAfter": str(now - 60),
            "validBefore": str(now + timeout), "nonce": nonce}
    extra = req.get("extra") or {}
    domain = {"name": extra.get("name") or asset.eip712_name, "version": extra.get("version") or asset.eip712_version,
              "chainId": chain_id, "verifyingContract": asset.address}
    msg = {"from": address, "to": req["payTo"], "value": int(auth["value"]), "validAfter": int(auth["validAfter"]),
           "validBefore": int(auth["validBefore"]), "nonce": bytes.fromhex(nonce[2:])}
    signed = Account.sign_typed_data(key, domain, TYPES, msg)
    return {"signature": _hex(signed.signature), "authorization": auth}


def rpc_url(network: str) -> str | None:
    return os.environ.get("X402_RPC_" + network.replace(":", "_").upper()) or DEFAULT_RPC.get(network)


def usdc_balance(asset: Asset, address: str, timeout: float = 8) -> float | None:
    """balanceOf(address) over JSON-RPC, in dollars; None when the RPC cannot be reached."""
    url = rpc_url(asset.network)
    if not url:
        return None
    data = "0x70a08231" + address[2:].lower().rjust(64, "0")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": asset.address, "data": data}, "latest"]}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json", "User-Agent": "hermes-x402-signer"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read())
        return int(res["result"], 16) / 10 ** asset.decimals
    except Exception:
        return None
