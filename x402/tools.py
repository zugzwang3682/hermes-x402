"""Tool schemas and handlers for the x402 plugin."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from . import x402

AUTO_APPROVE_USD = float(os.environ.get("X402_AUTO_APPROVE_USD", "0.05"))
DOWNLOAD_DIR = Path(os.environ.get("X402_DOWNLOAD_DIR", os.path.join(os.environ.get("HERMES_HOME", "/opt/data"),
                                                                     "x402", "downloads")))
TEXT_TYPES = ("application/json", "text/", "application/xml", "application/x-ndjson", "application/javascript")
KEEP_HEADERS = ("content-type", "content-length", "location", "retry-after")

try:  # inside Hermes
    from tools.registry import tool_error, tool_result
except ImportError:  # tests
    def tool_result(data=None, **kw):
        return json.dumps(data if data is not None else kw, ensure_ascii=False)

    def tool_error(message, **extra):
        return json.dumps({"error": str(message), **extra}, ensure_ascii=False)

_signer = None          # tests pin a signer here
_signer_factory = None  # register() installs the host's (local or remote, per settings)


def signer():
    if _signer is not None:
        return _signer
    if _signer_factory is not None:
        return _signer_factory()
    return x402.Signer()


_STR = {"type": "string"}
_REQUEST_PROPS = {
    "url": {"type": "string", "description": "Full https URL of the x402 resource"},
    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "description": "HTTP method (default GET)"},
    "headers": {"type": "object", "additionalProperties": _STR, "description": "Extra request headers"},
    "json": {"description": "JSON request body (object or array); sets Content-Type: application/json"},
    "body": {"type": "string", "description": "Raw request body, when not JSON"},
}

X402_INSPECT_SCHEMA = {
    "name": "x402_inspect",
    "description": "Check what an x402 (HTTP 402) resource costs without paying: sends the request unpaid and reports "
                   "the price, network and whether this agent's wallet and limits would allow paying it. Free.",
    "parameters": {"type": "object", "properties": {**_REQUEST_PROPS}, "required": ["url"]},
}

X402_FETCH_SCHEMA = {
    "name": "x402_fetch",
    "description": "Make an HTTP request to an x402 resource and pay for it in USDC from this agent's wallet if the "
                   "server asks (HTTP 402). Pays at most once and never more than max_usd. Payments whose max_usd is "
                   f"above ${AUTO_APPROVE_USD:.2f} wait for the user's approval. Use x402_inspect first when the price is "
                   "unknown. Returns the response body (text) or a saved file path (binary), what was paid and the "
                   "on-chain settlement.",
    "parameters": {"type": "object", "properties": {
        **_REQUEST_PROPS,
        "max_usd": {"type": "number", "description": "Most you will pay for this one request, in USD. Required."},
        "network": {"type": "string", "description": "CAIP-2 network to pay on when several are offered, e.g. eip155:8453 (Base)"},
        "wallet": {"type": "string", "description": "Wallet name; omit for the default wallet"},
        "max_chars": {"type": "integer", "description": "Truncate a text body to this many characters (default 20000)"},
    }, "required": ["url", "max_usd"]},
}

X402_WALLETS_SCHEMA = {
    "name": "x402_wallets",
    "description": "This agent's x402 wallets: addresses, on-chain USDC balances, spending limits, spend in the last "
                   "24 hours and recent payments.",
    "parameters": {"type": "object", "properties": {
        "balances": {"type": "boolean", "description": "Query on-chain balances (default true; slower)"},
        "history": {"type": "integer", "description": "How many recent payments to list (default 5)"},
    }, "required": []},
}


def _request_parts(args: dict) -> tuple[str, str, dict, bytes | None]:
    url = str(args.get("url") or "").strip()
    u = urlparse(url)
    if u.scheme not in ("https", "http") or not u.netloc:
        raise x402.X402Error("url must be a full http(s) URL")
    if u.scheme == "http" and u.hostname not in ("localhost", "127.0.0.1") and not os.environ.get("X402_ALLOW_HTTP"):
        raise x402.X402Error("refusing to pay over plain http; use https")
    method = str(args.get("method") or "GET").upper()
    headers = {str(k): str(v) for k, v in (args.get("headers") or {}).items()}
    content = None
    if args.get("json") is not None:
        content = json.dumps(args["json"]).encode()
        headers.setdefault("Content-Type", "application/json")
    elif args.get("body") is not None:
        content = str(args["body"]).encode()
    return method, url, headers, content


def _summarize_required(required: dict) -> dict:
    res = required.get("resource")
    return {"resource": res, "error": required.get("error"),
            "offers": [{k: a.get(k) for k in ("scheme", "network", "amount", "asset", "payTo")}
                       for a in required.get("accepts") or []]}


def _body(r, max_chars: int) -> dict:
    ctype = r.headers.get("content-type", "")
    if not r.content:
        return {"body": ""}
    if any(ctype.startswith(t) for t in TEXT_TYPES) or not ctype:
        text = r.text
        if ctype.startswith("application/json"):
            try:
                text = json.dumps(r.json(), ensure_ascii=False)
            except ValueError:
                pass
        out = {"body": text[:max_chars]}
        if len(text) > max_chars:
            out["truncated"] = f"{len(text) - max_chars} more characters not shown"
        return out
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(urlparse(str(r.url)).path).name or "download")[:80]
    ext = {"audio/wav": ".wav", "audio/mpeg": ".mp3", "image/png": ".png", "image/jpeg": ".jpg",
           "application/pdf": ".pdf"}.get(ctype.split(";")[0], "")
    path = DOWNLOAD_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}{ext if not name.endswith(ext) else ''}"
    path.write_bytes(r.content)
    return {"saved_to": str(path), "bytes": len(r.content)}


def _headers(r) -> dict:
    return {k: v for k, v in r.headers.items() if k.lower() in KEEP_HEADERS or k.lower().startswith("x-")}


def handle_inspect(args: dict, **_) -> str:
    try:
        method, url, headers, content = _request_parts(args)
        res = x402.probe(method, url, headers, content)
        if res.required is None:
            return tool_result({"paid_resource": False, "status": res.response.status_code,
                                "note": "the server did not ask for payment", **_body(res.response, 2000)})
        quote = signer().quote(res.required)
        return tool_result({"paid_resource": True, "quote": quote, "server_offer": _summarize_required(res.required),
                            "auto_approve_under_usd": AUTO_APPROVE_USD})
    except x402.X402Error as e:
        return tool_error(str(e))
    except Exception as e:
        return tool_error(f"x402_inspect failed: {type(e).__name__}: {e}")


def handle_fetch(args: dict, **_) -> str:
    try:
        method, url, headers, content = _request_parts(args)
        max_usd = float(args["max_usd"])
        res = x402.fetch(method, url, max_usd, signer(), headers, content,
                         network=args.get("network") or None, wallet=args.get("wallet") or None)
        r = res.response
        out = {"status": r.status_code, "headers": _headers(r)}
        if res.signed:
            s = res.signed
            paid = {"usd": s["usd"], "network": s["network"], "chain": s["chain"], "wallet": s["wallet"],
                    "from": s["address"], "to": s["pay_to"], "testnet": s["testnet"]}
            st = res.settlement or {}
            tx = st.get("transaction")
            if tx:
                paid["transaction"] = tx
                paid["explorer"] = s["explorer_tx"] + tx
            if st and st.get("success") is False:
                paid["settlement_error"] = st.get("errorReason") or "settlement failed"
            out["paid"] = paid
        else:
            out["paid"] = None
        if res.notes:
            out["notes"] = res.notes
        out.update(_body(r, int(args.get("max_chars") or 20000)))
        return tool_result(out)
    except x402.X402Error as e:
        return tool_error(str(e))
    except (KeyError, ValueError) as e:
        return tool_error(f"bad arguments: {e}")
    except Exception as e:
        return tool_error(f"x402_fetch failed: {type(e).__name__}: {e}")


def handle_wallets(args: dict, **_) -> str:
    try:
        out = signer().wallets()
        if args.get("balances", True):
            out["balances"] = signer().balances()["balances"]
        n = int(args.get("history") if args.get("history") is not None else 5)
        if n > 0:
            out["recent_payments"] = signer().history(n)["history"]
        out["auto_approve_under_usd"] = AUTO_APPROVE_USD
        return tool_result(out)
    except x402.X402Error as e:
        return tool_error(str(e))


def check_available() -> bool:
    return True  # the signer is reached lazily; tools report a clear error if it is down


TIERS = (0.10, 0.25, 1.0, 5.0, 25.0, 100.0)


def pre_tool_call(tool_name: str = "", args: dict | None = None, **_) -> dict | None:
    """Payments with a ceiling above the auto-approve threshold wait for the user. The approval names the host
    and a price tier, so 'always' trusts that host up to that tier, never more; the signer's hard limits still apply."""
    if tool_name != "x402_fetch" or not isinstance(args, dict):
        return None
    try:
        max_usd = float(args.get("max_usd"))
    except (TypeError, ValueError):
        return {"action": "block", "message": "x402_fetch needs max_usd: the most to pay for this request, in USD"}
    if max_usd <= AUTO_APPROVE_USD:
        return None
    host = urlparse(str(args.get("url") or "")).hostname or "?"
    tier = next((t for t in TIERS if max_usd <= t + 1e-9), max_usd)
    method = str(args.get("method") or "GET").upper()
    return {"action": "approve", "rule_key": f"x402:{host}:{tier:g}",
            "message": f"pay up to ${max_usd:.2f} USDC to {host} for {method} {args.get('url')}"}
