"""Operator commands, run by a person inside the signer container:

  wallet new <name>        generate a key; prints only the address
  wallet import <name>     paste an existing EVM private key (hidden input)
  wallet import <name> --stdin
                           read the key from stdin: a bare 0x... key or a KEY=0x... line; # comments are skipped,
                           e.g. `... run --rm -T x402-signer wallet import hermes --stdin < wallet.key`
  wallet import <name> --phrase [--index N | --path m/44'/60'/0'/0/N] [--passphrase] [--stdin]
                           derive one account from a 12-24 word secret phrase (hidden prompt, or stdin). Only that
                           account's key is stored; the phrase is never saved or printed. Default path m/44'/60'/0'/0/0,
                           the first account in MetaMask, Rabby and most wallets; --index 1 is the second.
                           --passphrase asks for the optional BIP-39 passphrase ("25th word").
  wallet list              names and addresses
  wallet default <name>    make this the wallet used when none is named
  serve                    run the HTTP API (the container's default command)
"""
from __future__ import annotations

import getpass
import os
import sys

from .policy import Limits
from .server import main as serve_main
from .wallets import Keystore


def key_from_text(text: str) -> str:
    """The first non-comment line, minus any NAME= prefix (the format gpu-lessor's buyer.py writes)."""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line.split("=", 1)[1].strip() if "=" in line else line
    return ""


def run_import(ks, rest: list[str], read_stdin=None, prompt=None) -> str:
    """`wallet import <name> [--stdin] [--phrase [--index N | --path P] [--passphrase]]` -> a one-line result.
    Shared by the container CLI and the Hermes plugin's shell command. Raises ValueError on bad input."""
    read_stdin = read_stdin or sys.stdin.read
    prompt = prompt or getpass.getpass
    if not rest or rest[0].startswith("-"):
        raise ValueError("usage: wallet import <name> [--stdin] [--phrase [--index N | --path P] [--passphrase]]")
    name, flags = rest[0], rest[1:]
    opts = {"stdin": False, "phrase": False, "passphrase": False, "index": 0, "path": None}
    i = 0
    while i < len(flags):
        f = flags[i]
        if f in ("--stdin", "--phrase", "--passphrase"):
            opts[f[2:]] = True
        elif f in ("--index", "--path") and i + 1 < len(flags):
            opts[f[2:]] = int(flags[i + 1]) if f == "--index" else flags[i + 1]
            i += 1
        else:
            raise ValueError(f"unknown option {f!r}")
        i += 1
    if not opts["phrase"]:
        key = key_from_text(read_stdin()) if opts["stdin"] else prompt("EVM private key (0x..., hidden): ").strip()
        if not key:
            raise ValueError("no key entered")
        w = ks.add_evm(name, key)
        return f"imported {w.name}: {w.address}{' (default)' if w.default else ''}"
    phrase = read_stdin() if opts["stdin"] else prompt("Secret phrase, 12-24 words (hidden): ")
    passphrase = prompt("BIP-39 passphrase (hidden; Enter if none): ") if opts["passphrase"] else ""
    w = ks.add_evm_phrase(name, phrase, index=opts["index"], path=opts["path"], passphrase=passphrase)
    path = opts["path"] or f"m/44'/60'/0'/0/{opts['index']}"
    return (f"imported {w.name}: {w.address}{' (default)' if w.default else ''} from the secret phrase at {path}\n"
            "Check that this address matches the account in your wallet app. Only this account's key was stored.")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] == "serve":
        serve_main()
        return 0
    if argv[0] != "wallet" or len(argv) < 2:
        print(__doc__)
        return 2
    ks = Keystore(os.environ.get("X402_DATA", "/data"))
    cmd, rest = argv[1], argv[2:]
    try:
        if cmd == "list":
            ws = ks.list()
            if not ws:
                print("no wallets yet; create one with: wallet new <name>")
            for w in ws:
                print(f"{w.name:20} {w.family:6} {w.address}{'  (default)' if w.default else ''}")
            lim = Limits.from_env()
            print(f"\nlimits: ${lim.per_payment_usd:.2f} per payment, ${lim.per_day_usd:.2f} per 24h, "
                  f"networks {', '.join(lim.networks)}")
            return 0
        if cmd == "import":
            print(run_import(ks, rest))
            print("The key lives only in this container's volume.")
            return 0
        if cmd == "new" and len(rest) == 1:
            w = ks.add_evm(rest[0])
            print(f"created {w.name}: {w.address}{' (default)' if w.default else ''}")
            print("The key lives only in this container's volume. Back it up before funding it: "
                  "whoever holds it holds the funds.")
            return 0
        if cmd == "default" and len(rest) == 1:
            w = ks.set_default(rest[0])
            print(f"default {w.family} wallet is now {w.name} ({w.address})")
            return 0
    except (ValueError, LookupError, FileExistsError) as e:
        print(f"error: {e}")
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
