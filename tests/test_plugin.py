"""The plugin end to end against a real signer (HTTP) and a fake x402 server that verifies signatures."""
import json
import urllib.request

import pytest


def call(handler, **args):
    return json.loads(handler(args))


def test_pays_and_returns_body_and_settlement(plugin, fake_server):
    out = call(plugin.handle_fetch, url=fake_server.url + "/paid", method="POST", json={"q": 1}, max_usd=0.10)
    assert out["status"] == 200, out
    assert json.loads(out["body"]) == {"ok": True, "echo": {"q": 1}}
    assert out["paid"]["usd"] == pytest.approx(0.10) and out["paid"]["chain"] == "Base"
    assert out["paid"]["explorer"].startswith("https://basescan.org/tx/0xabab")
    assert out["headers"]["x-charged-usd"] == "0.1"
    assert len(fake_server.payments) == 1 and fake_server.payments[0]["body"] == b'{"q": 1}'


def test_free_resource_pays_nothing(plugin, fake_server):
    out = call(plugin.handle_fetch, url=fake_server.url + "/free", max_usd=1)
    assert out["status"] == 200 and out["paid"] is None


def test_price_above_ceiling_is_not_paid(plugin, fake_server, signer_env):
    out = call(plugin.handle_fetch, url=fake_server.url + "/paid", max_usd=0.05)
    assert "ceiling" in out["error"]
    assert fake_server.payments == []
    wallets = json.loads(urllib.request.urlopen(signer_env[0] + "/wallets").read())
    assert wallets["spent_24h_usd"] == 0


def test_daily_limit_holds_across_calls(plugin, fake_server):
    for _ in range(3):
        assert call(plugin.handle_fetch, url=fake_server.url + "/paid", max_usd=0.10)["status"] == 200
    out = call(plugin.handle_fetch, url=fake_server.url + "/paid", max_usd=0.10)
    assert "daily limit" in out["error"] and len(fake_server.payments) == 3


def test_inspect_quotes_without_paying(plugin, fake_server):
    out = call(plugin.handle_inspect, url=fake_server.url + "/paid", method="POST")
    assert out["paid_resource"] and out["quote"]["payable"] and out["quote"]["usd"] == pytest.approx(0.10)
    assert out["quote"]["wallet"] == "hermes" and fake_server.payments == []
    fake_server.units = 2_000_000
    out = call(plugin.handle_inspect, url=fake_server.url + "/paid")
    assert out["quote"]["payable"] is False and "per-payment" in out["quote"]["reason"]


def test_binary_body_is_saved(plugin, fake_server, tmp_path):
    out = call(plugin.handle_fetch, url=fake_server.url + "/bin", max_usd=0.10)
    assert out["bytes"] > 0 and out["saved_to"].endswith(".wav")
    assert open(out["saved_to"], "rb").read().startswith(b"RIFF")


def test_v1_server_is_explained(plugin, fake_server):
    out = call(plugin.handle_fetch, url=fake_server.url + "/v1", max_usd=0.10)
    assert "v1" in out["error"]


def test_plain_http_refused_off_localhost(plugin):
    out = call(plugin.handle_fetch, url="http://example.com/x", max_usd=0.01)
    assert "https" in out["error"]


def test_wallets_tool(plugin, fake_server):
    call(plugin.handle_fetch, url=fake_server.url + "/paid", max_usd=0.10)
    out = call(plugin.handle_wallets, balances=False, history=5)
    assert out["wallets"][0]["name"] == "hermes" and "key" not in out["wallets"][0]
    assert out["spent_24h_usd"] == pytest.approx(0.10) and len(out["recent_payments"]) == 1


def test_signer_down_is_a_clear_error(plugin, fake_server, monkeypatch):
    from x402 import x402
    monkeypatch.setattr(plugin, "_signer", x402.Signer("http://127.0.0.1:9"))
    out = call(plugin.handle_fetch, url=fake_server.url + "/paid", max_usd=0.10)
    assert "cannot reach the x402 signer" in out["error"] and fake_server.payments == []


def test_approval_hook(plugin):
    h = plugin.pre_tool_call
    assert h("x402_fetch", {"url": "https://a.test/x", "max_usd": 0.01}) is None
    d = h("x402_fetch", {"url": "https://a.test/x", "max_usd": 0.5, "method": "post"})
    assert d["action"] == "approve" and d["rule_key"] == "x402:a.test:1" and "$0.50" in d["message"]
    assert h("x402_fetch", {"url": "https://a.test/x"})["action"] == "block"
    assert h("terminal", {"command": "ls"}) is None
