"""/x402 against the fake balance broker, with a real local signer and a fake Hermes host."""
import time

import pytest
from conftest import PAY_TO, fake_jwt

from x402.commands import Commands
from x402.host import JsonState
from x402.signers import LocalSigner, limits_from_settings, make_signer
from x402 import brokers as B


class FakeHost:
    interactive = False

    def __init__(self, tmp_path, broker_url, main=("", ""), signer_settings=None):
        self.tmp = tmp_path
        self._settings = {"brokers": {"fake": {"url": broker_url}}, "active_broker": "fake", **(signer_settings or {})}
        self._state = JsonState(tmp_path / "state.json")
        self.main = main
        self.sub = ("", "")
        self.placeholder = False
        self._signer = None

    def settings(self):
        return dict(self._settings)

    def set_setting(self, key, value):
        self._settings[key] = value

    @property
    def state(self):
        return self._state

    def signer(self):
        if self._signer is None:
            self._signer = make_signer(self._settings, self.tmp, lambda: B.all_payees(self._settings))
        return self._signer

    def main_model(self):
        return self.main

    def main_model_usable(self):
        return bool(self.main[0])

    def set_main_model(self, model):
        if self.main[0] and self.main[0] != "x402-broker":
            self.previous = self.main
        self.main = ("x402-broker", model)
        return f"Main model set to {model}"

    def restore_main_model(self):
        self.main, self.previous = self.previous, None
        return f"Main model restored to {self.main[1]}"

    def subagent_model(self):
        return self.sub

    def set_subagents(self, model):
        self.sub = ("x402-broker", model) if model else ("", "")
        return f"Sub-agents now run on {model}" if model else "Sub-agents now use the main model again."

    def ensure_placeholder(self):
        self.placeholder = True


@pytest.fixture
def host(tmp_path, broker):
    h = FakeHost(tmp_path, broker.url)
    h.signer().keystore.add_evm("hermes")
    return h


def run(host, line):
    return Commands(host).run(line)


def test_topup_with_no_model_sets_the_main_model(host, broker):
    out = run(host, "topup 5")
    assert "Topped up $5.00 on fake" in out and "Balance: $5.0000" in out, out
    assert "basescan.org/tx/0xefef" in out
    assert host.main == ("x402-broker", "gpt-oss-120b") and host.placeholder
    assert host.state.get("broker.fake.jwt") and broker.topups == [5.0]


def test_topup_with_a_model_set_leaves_it(tmp_path, broker):
    h = FakeHost(tmp_path, broker.url, main=("claude-subscription-directsdk-experimental", "sonnet"))
    h.signer().keystore.add_evm("hermes")
    out = run(h, "topup 1")
    assert "Main model stays sonnet" in out and h.main[1] == "sonnet"


def test_topup_uses_the_topup_allowance_only_for_the_broker(host, broker):
    # $5 is over the $1 standard limit but inside the $20 top-up limit, because payTo is the broker's address
    assert "Topped up $5.00" in run(host, "topup 5")
    assert "top-up limit" in run(host, "topup 25")
    assert "daily top-up limit" in run(host, "topup 16")          # 5 + 16 > 20
    lim = limits_from_settings({}, (PAY_TO,))
    assert lim.is_topup(PAY_TO.lower()) and not lim.is_topup("0x" + "11" * 20)


def test_topup_limit_does_not_apply_to_other_payees(host):
    s = host.signer()
    other = {"x402Version": 2, "resource": {}, "accepts": [{
        "scheme": "exact", "network": "eip155:8453", "amount": "5000000", "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "payTo": "0x" + "22" * 20, "maxTimeoutSeconds": 60, "extra": {"name": "USD Coin", "version": "2"}}]}
    from x402.x402 import X402Error
    with pytest.raises(X402Error, match="per-payment limit"):
        s.sign(other, max_usd=5)


def test_balance_models_use(host, broker):
    run(host, "topup 2")
    assert "fake balance: $2.0000" in run(host, "balance")
    models = run(host, "models")
    assert "✓ gpt-oss-120b" in models and "· qwen3-8b" in models and "bge-m3" not in models
    assert "at least 64,000" in run(host, "use main qwen3-8b")
    assert "Sub-agents now run on gpt-oss-120b" in run(host, "use subagents gpt-oss-120b")
    assert host.sub == ("x402-broker", "gpt-oss-120b")
    assert "not a chat model" in run(host, "use main nope")


def test_balance_without_topup_and_expired_token(host, broker):
    assert "no balance yet" in run(host, "balance")
    host.state.set("broker.fake.jwt", fake_jwt("0xabc", time.time() - 10))
    assert "token expired" in run(host, "balance")


def test_broker_management(host, broker):
    assert "* fake" in run(host, "broker list")
    assert "must be https" in run(host, f"broker add other {broker.url}")
    assert "cannot remove the last broker" in run(host, "broker remove fake")
    assert "no broker named" in run(host, "broker use nope")


def test_wallet_import_is_refused_in_chat(host):
    out = run(host, "wallet import hermes2")
    assert "never paste a private key" in out
    assert "hermes" in run(host, "wallet list")


def test_status_and_help(host, broker):
    s = run(host, "status")
    assert "signer: local" in s and "top-ups to brokers $20.0" in s and "main model: none" in s
    assert "topup <usd>" in run(host, "")
    assert "unknown subcommand" in run(host, "frobnicate")
    assert "not an amount" in run(host, "topup lots")


def test_remote_signer_is_selected_by_settings(tmp_path):
    s = make_signer({"signer": "remote", "signer_url": "http://127.0.0.1:9"}, tmp_path, lambda: ())
    assert not isinstance(s, LocalSigner) and s.url == "http://127.0.0.1:9"


def test_switch_back_to_the_previous_main_model_and_subagents_to_main(tmp_path, broker):
    h = FakeHost(tmp_path, broker.url, main=("claude-subscription-directsdk-experimental", "sonnet"))
    h.signer().keystore.add_evm("hermes")
    assert "Main model stays sonnet" in run(h, "topup 1")   # a set model is never replaced by a top-up
    run(h, "use main gpt-oss-120b")
    assert h.main == ("x402-broker", "gpt-oss-120b")
    assert "restored to sonnet" in run(h, "use main previous") and h.main[1] == "sonnet"
    run(h, "use subagents gpt-oss-120b")
    assert "main model again" in run(h, "use subagents main") and h.sub == ("", "")


def test_phrase_in_chat_is_refused_with_an_exposure_warning(host):
    out = run(host, "wallet import mine --phrase " + " ".join(["word"] * 12))
    assert "never paste" in out and "treat that wallet as exposed" in out
    assert "exposed" not in run(host, "wallet import mine --phrase")
