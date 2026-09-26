import time

import pytest
from conftest import BASE_USDC, requirement, verify
from x402signer.policy import Limits, PolicyError
from x402signer.server import Signer
from x402signer.wallets import Keystore


def make(tmp_path, per_payment=1.0, per_day=0.35, networks=("eip155:8453",)):
    Keystore(tmp_path).add_evm("hermes")
    return Signer(str(tmp_path), Limits(per_payment, per_day, networks))


def required(*offers):
    return {"x402Version": 2, "resource": {"url": "https://example.test/x"}, "accepts": list(offers)}


def test_signature_is_a_valid_eip3009_authorization(tmp_path):
    s = make(tmp_path)
    out = s.sign({"required": required(requirement(100000))})
    assert verify(out["payment"]) is None
    assert out["usd"] == pytest.approx(0.10)
    assert out["address"] == s.keystore.get(None, "evm").address
    assert out["payment"]["accepted"]["network"] == "eip155:8453"


def test_per_payment_limit(tmp_path):
    s = make(tmp_path, per_payment=0.05)
    with pytest.raises(PolicyError, match="per-payment limit"):
        s.sign({"required": required(requirement(100000))})
    assert s.ledger.spent_usd(0) == 0


def test_daily_limit_counts_every_signature(tmp_path):
    s = make(tmp_path, per_day=0.25)
    s.sign({"required": required(requirement(100000))})
    s.sign({"required": required(requirement(100000))})
    with pytest.raises(PolicyError, match="daily limit"):
        s.sign({"required": required(requirement(100000))})
    assert s.ledger.spent_usd(time.time() - 60) == pytest.approx(0.20)


def test_callers_ceiling(tmp_path):
    s = make(tmp_path)
    with pytest.raises(PolicyError, match="ceiling"):
        s.sign({"required": required(requirement(100000)), "max_usd": 0.05})


def test_disabled_network_and_unknown_asset_are_refused(tmp_path):
    s = make(tmp_path)
    with pytest.raises(PolicyError, match="nothing payable"):
        s.sign({"required": required(requirement(1000, network="eip155:1",
                                                  asset="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"))})
    with pytest.raises(PolicyError, match="nothing payable"):
        s.sign({"required": required(requirement(1000, asset="0x000000000000000000000000000000000000dEaD"))})


def test_picks_the_payable_offer_and_the_requested_network(tmp_path):
    s = make(tmp_path, networks=("eip155:8453", "eip155:1"))
    eth = requirement(2000, network="eip155:1", asset="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
    base = requirement(1000)
    sol = {"scheme": "exact", "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "amount": "1000",
           "asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "payTo": "x"}
    assert s.sign({"required": required(sol, base, eth)})["network"] == "eip155:8453"
    out = s.sign({"required": required(sol, base, eth), "network": "eip155:1"})
    assert out["network"] == "eip155:1" and verify(out["payment"]) is None


def test_v1_is_refused(tmp_path):
    s = make(tmp_path)
    with pytest.raises(PolicyError, match="x402Version 1"):
        s.sign({"required": {"x402Version": 1, "accepts": [requirement()]}})


def test_quote_does_not_sign_or_spend(tmp_path):
    s = make(tmp_path, per_payment=0.05)
    q = s.quote({"required": required(requirement(100000))})
    assert q["payable"] is False and "per-payment" in q["reason"] and q["usd"] == pytest.approx(0.10)
    assert s.quote({"required": required(requirement(10000))})["payable"] is True
    assert s.ledger.spent_usd(0) == 0


def test_keystore(tmp_path):
    ks = Keystore(tmp_path)
    a = ks.add_evm("one")
    assert a.default and oct((tmp_path / "wallets" / "one.json").stat().st_mode)[-3:] == "600"
    key = "0x" + "11" * 32
    b = ks.add_evm("two", key)
    assert not b.default and ks.get(None, "evm").name == "one"
    with pytest.raises(FileExistsError):
        ks.add_evm("two")
    with pytest.raises(FileExistsError):
        ks.add_evm("three", key)  # same address under another name
    ks.set_default("two")
    assert ks.get(None, "evm").name == "two"
    assert all("key" not in w.public() for w in ks.list())
    with pytest.raises(ValueError):
        ks.add_evm("../escape")


def test_no_wallet(tmp_path):
    s = Signer(str(tmp_path), Limits(1, 1, ("eip155:8453",)))
    with pytest.raises(LookupError, match="no evm wallet"):
        s.sign({"required": required(requirement(1000))})


def test_asset_constant_matches_base_usdc():
    from x402signer.networks import find_asset
    assert find_asset("eip155:8453", BASE_USDC.lower()).eip712_name == "USD Coin"


def test_import_parses_buyer_key_files():
    from x402signer.cli import key_from_text
    assert key_from_text("# x402 buyer wallet 0xabc\nAGENT_WALLET_KEY=0x11\n") == "0x11"
    assert key_from_text("\n0x22\n") == "0x22"
    assert key_from_text("# only a comment\n") == ""


def test_cli_import_from_stdin(tmp_path, monkeypatch, capsys):
    import io
    from x402signer import cli
    monkeypatch.setenv("X402_DATA", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO("# w\nAGENT_WALLET_KEY=0x" + "22" * 32 + "\n"))
    assert cli.main(["wallet", "import", "hermes", "--stdin"]) == 0
    out = capsys.readouterr().out
    assert "imported hermes: 0x" in out and "22" * 32 not in out


HARDHAT = "test test test test test test test test test test test junk"  # the public Hardhat/Anvil test phrase


def test_phrase_import_derives_the_standard_accounts(tmp_path):
    ks = Keystore(tmp_path)
    assert ks.add_evm_phrase("a", HARDHAT).address == "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
    assert ks.add_evm_phrase("b", "  " + HARDHAT.upper() + "\n", index=1).address == \
        "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"  # whitespace and case are normalised
    c = ks.add_evm_phrase("c", HARDHAT, passphrase="extra")
    assert c.address not in ("0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",)
    stored = (tmp_path / "wallets" / "a.json").read_text()
    assert "junk" not in stored and "secret phrase, m/44'/60'/0'/0/0" in stored  # phrase never stored


def test_phrase_import_rejects_bad_input(tmp_path):
    ks = Keystore(tmp_path)
    with pytest.raises(ValueError, match="not a valid BIP-39"):
        ks.add_evm_phrase("x", "test " * 12)
    with pytest.raises(ValueError, match="12, 15, 18, 21 or 24 words"):
        ks.add_evm_phrase("x", "test test test")
    with pytest.raises(ValueError, match="not a derivation path"):
        ks.add_evm_phrase("x", HARDHAT, path="m/44'/60'/x")


def test_run_import_prompts_and_stdin(tmp_path):
    from x402signer.cli import run_import
    ks = Keystore(tmp_path)
    answers = iter([HARDHAT, ""])
    out = run_import(ks, ["p", "--phrase", "--passphrase"], prompt=lambda _: next(answers))
    assert "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266" in out and "junk" not in out
    out = run_import(ks, ["q", "--phrase", "--index", "2", "--stdin"], read_stdin=lambda: HARDHAT + "\n")
    assert "m/44'/60'/0'/0/2" in out
    with pytest.raises(ValueError, match="unknown option"):
        run_import(ks, ["r", "--seed"])
