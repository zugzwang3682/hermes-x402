"""The x402-broker provider's client in balance mode: a real OpenAI client against the fake broker."""
import importlib.util
import time

import openai
import pytest
from conftest import ROOT, fake_jwt

spec = importlib.util.spec_from_file_location("x402_broker_transport", ROOT / "x402-broker" / "transport.py")
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


@pytest.fixture
def funded(broker):
    w = "0x" + "aa" * 20
    broker.balances[w] = 0.05
    tok = fake_jwt(w, time.time() + 86400)
    broker.tokens[tok] = (w, time.time() + 86400)
    return tok


def client(broker, jwt, seen=None):
    return transport.make_client(lambda: ("fake", broker.url, jwt),
                                 on_balance=(lambda n, b: seen.append((n, b))) if seen is not None else None,
                                 api_key="x402-balance", base_url="https://stale.example/v1", max_retries=2,
                                 unknown_future_key=1)


def test_metered_call_sends_the_jwt_to_the_active_broker(broker, funded):
    seen = []
    r = client(broker, funded, seen).chat.completions.create(model="gpt-oss-120b",
                                                              messages=[{"role": "user", "content": "ping"}])
    assert r.choices[0].message.content == "pong"
    assert broker.chats == [{"model": "gpt-oss-120b", "auth": f"Bearer {funded}"}]  # not the placeholder, not stale.example
    assert seen and seen[-1][0] == "fake" and seen[-1][1] == pytest.approx(0.04)


def test_insufficient_balance_says_how_to_top_up_and_never_pays(broker, funded):
    broker.charge = 1.0
    with pytest.raises(openai.APIStatusError) as e:
        client(broker, funded).chat.completions.create(model="gpt-oss-120b", messages=[{"role": "user", "content": "x"}])
    assert e.value.status_code == 402 and "x402 topup" in str(e.value) and "$0.0500" in str(e.value)
    assert broker.topups == []


def test_expired_token_and_no_token(broker):
    old = fake_jwt("0xabc", time.time() - 5)
    with pytest.raises(openai.APIStatusError) as e:
        client(broker, old).chat.completions.create(model="gpt-oss-120b", messages=[{"role": "user", "content": "x"}])
    assert e.value.status_code == 401 and "expired" in str(e.value)
    with pytest.raises(openai.APIStatusError) as e:
        client(broker, None).chat.completions.create(model="gpt-oss-120b", messages=[{"role": "user", "content": "x"}])
    assert e.value.status_code == 402 and "no x402 balance" in str(e.value)
    assert broker.chats == []


def test_vendored_signer_core_matches_the_container_signer():
    a, b = ROOT / "signer" / "x402signer", ROOT / "x402" / "x402signer"
    for f in sorted(p.name for p in a.glob("*.py")):
        assert (a / f).read_text() == (b / f).read_text(), f"x402/x402signer/{f} drifted: cp -R signer/x402signer x402/"
