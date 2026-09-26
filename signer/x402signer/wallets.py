"""Wallet keystore: one file per wallet under $X402_DATA/wallets, mode 600, in a volume only the signer mounts.

Nothing in this module returns a key to a caller outside the process; the server exposes names and addresses."""
from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from eth_account import Account

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
EVM_PATH = "m/44'/60'/0'/0/{index}"  # the standard Ethereum account path (MetaMask, Rabby, Coinbase Wallet, Ledger Live)
PATH_RE = re.compile(r"^m(/[0-9]+'?)+$")


def derive_evm_key(phrase: str, index: int = 0, path: str | None = None, passphrase: str = "") -> tuple[str, str]:
    """(private key, path) for one account of a BIP-39 phrase. The phrase itself is never stored."""
    words = " ".join(phrase.lower().split())
    if len(words.split()) not in (12, 15, 18, 21, 24):
        raise ValueError(f"a secret phrase has 12, 15, 18, 21 or 24 words, not {len(words.split())}")
    path = path or EVM_PATH.format(index=int(index))
    if not PATH_RE.match(path):
        raise ValueError(f"not a derivation path: {path!r}")
    Account.enable_unaudited_hdwallet_features()
    try:
        acct = Account.from_mnemonic(words, passphrase=passphrase or "", account_path=path)
    except Exception as e:
        raise ValueError("that phrase is not a valid BIP-39 secret phrase (a word is misspelled or out of order)") from e
    return "0x" + acct.key.hex().removeprefix("0x"), path


@dataclass
class Wallet:
    name: str
    family: str      # "evm" (Solana later)
    address: str
    default: bool
    _key: str

    def public(self) -> dict:
        return {"name": self.name, "family": self.family, "address": self.address, "default": self.default}


class Keystore:
    def __init__(self, data_dir: str | os.PathLike):
        self.dir = Path(data_dir) / "wallets"

    def _ensure_dir(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)

    def _path(self, name: str) -> Path:
        if not NAME_RE.match(name):
            raise ValueError("wallet names are 1-32 chars of a-z, 0-9, '-' and '_', starting with a letter or digit")
        return self.dir / f"{name}.json"

    def list(self) -> list[Wallet]:
        if not self.dir.exists():
            return []
        out = []
        for p in sorted(self.dir.glob("*.json")):
            d = json.loads(p.read_text())
            out.append(Wallet(d["name"], d["family"], d["address"], bool(d.get("default")), d["key"]))
        return out

    def get(self, name: str | None, family: str) -> Wallet:
        """The named wallet, or the family's default (the only one, if there is exactly one)."""
        ws = [w for w in self.list() if w.family == family]
        if name:
            for w in ws:
                if w.name == name:
                    return w
            raise LookupError(f"no {family} wallet named {name!r}")
        defaults = [w for w in ws if w.default] or (ws if len(ws) == 1 else [])
        if not defaults:
            raise LookupError(f"no default {family} wallet; create one with `wallet new` or pick one with `wallet default`"
                              if ws else f"no {family} wallet yet; create one with `wallet new <name>`")
        return defaults[0]

    def add_evm_phrase(self, name: str, phrase: str, index: int = 0, path: str | None = None,
                       passphrase: str = "") -> Wallet:
        key, path = derive_evm_key(phrase, index, path, passphrase)
        return self.add_evm(name, key, source=f"secret phrase, {path}")

    def add_evm(self, name: str, key: str | None = None, source: str | None = None) -> Wallet:
        """Store an EVM key (generated when None). Refuses to overwrite: a wallet file may be the only copy of a key."""
        self._ensure_dir()
        p = self._path(name)
        if p.exists():
            raise FileExistsError(f"wallet {name!r} already exists")
        generated = not key
        key = key.strip() if key else "0x" + secrets.token_hex(32)
        if not key.startswith("0x"):
            key = "0x" + key
        acct = Account.from_key(key)  # validates the key
        if any(w.address.lower() == acct.address.lower() for w in self.list()):
            raise FileExistsError(f"{acct.address} is already in the keystore")
        first = not [w for w in self.list() if w.family == "evm"]
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"name": name, "family": "evm", "address": acct.address, "default": first, "key": key,
                       "source": source or ("generated" if generated else "imported key")}, f)
        return Wallet(name, "evm", acct.address, first, key)

    def set_default(self, name: str) -> Wallet:
        target = self.get(name, self._family_of(name))
        for w in self.list():
            if w.family == target.family:
                p = self._path(w.name)
                d = json.loads(p.read_text())
                d["default"] = w.name == name
                p.write_text(json.dumps(d))
                os.chmod(p, 0o600)
        return self.get(name, target.family)

    def _family_of(self, name: str) -> str:
        for w in self.list():
            if w.name == name:
                return w.family
        raise LookupError(f"no wallet named {name!r}")
