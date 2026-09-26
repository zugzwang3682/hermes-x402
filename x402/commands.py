"""/x402: top up a broker balance, pick models, manage brokers and wallets. Runs without a model configured.

In Slack: `!x402 topup 5` or `/hermes x402 topup 5`. In the CLI: `/x402 topup 5`, or `hermes x402 ...` from a shell
(the only place a wallet key may be imported: keys never travel through chat)."""
from __future__ import annotations

import shlex
import time

try:
    from . import brokers as B
    from . import x402
except ImportError:
    from x402 import brokers as B
    from x402 import x402

HELP = """x402 — pay-as-you-go models on an x402 broker, funded from this agent's USDC wallet.

  topup <usd>                  add balance on the active broker (pays from the wallet); sets the main model only if none is set
  balance                      broker balance and on-chain wallet USDC
  models                       models that can run an agent here (≥64K context), with prices
  use main <model>             run the main agent on a broker model   (use main previous: undo the last switch)
  use subagents <model>        run delegated sub-agents on a broker model   (use subagents main: back to the main model)
  broker list | use <name> | add <name> <url> | remove <name>
  wallet list | new <name> | default <name>
  wallet import <name> [--phrase [--index N]]     shell only: hermes x402 wallet import <name> --phrase
  status                       signer, limits, brokers, token expiry"""


class Host:
    """What the command needs from Hermes. HermesHost implements it; tests use a fake."""

    def settings(self) -> dict: ...
    def set_setting(self, key: str, value) -> None: ...
    @property
    def state(self): ...
    def signer(self): ...
    def main_model(self) -> tuple[str, str]: ...
    def main_model_usable(self) -> bool: ...
    def set_main_model(self, model: str) -> str: ...
    def restore_main_model(self) -> str: ...
    def subagent_model(self) -> tuple[str, str]: ...
    def set_subagents(self, model: str) -> str: ...
    def ensure_placeholder(self) -> None: ...
    interactive: bool = False  # True only for `hermes x402` in a shell


def _usd(s: str) -> float:
    try:
        v = float(s.lstrip("$"))
    except ValueError:
        raise ValueError(f"not an amount: {s!r}") from None
    if not 0 < v < 1e6:
        raise ValueError("the amount must be a positive number of dollars")
    return round(v, 2)


def _money(v) -> str:
    return f"${float(v):,.4f}" if v is not None else "?"


def _explorer(signed: dict | None, settlement: dict | None) -> str:
    tx = (settlement or {}).get("transaction")
    if signed and tx:
        return f" — {signed.get('explorer_tx', '')}{tx}"
    return ""


class Commands:
    def __init__(self, host: Host):
        self.host = host

    # -- entry -----------------------------------------------------------------------------------------------------
    def run(self, raw: str) -> str:
        try:
            argv = shlex.split(raw or "")
        except ValueError as e:
            return f"x402: {e}"
        if not argv or argv[0] in ("help", "-h", "--help"):
            return HELP
        cmd, rest = argv[0], argv[1:]
        fn = getattr(self, "cmd_" + cmd.replace("-", "_"), None)
        if fn is None:
            return f"x402: unknown subcommand {cmd!r}\n\n{HELP}"
        try:
            return fn(rest)
        except (ValueError, LookupError, x402.X402Error) as e:
            return f"x402 {cmd}: {e}"
        except Exception as e:  # never take the gateway down
            return f"x402 {cmd} failed: {type(e).__name__}: {e}"

    # -- helpers ---------------------------------------------------------------------------------------------------
    def _active(self) -> tuple[str, str, dict]:
        s = self.host.settings()
        name = B.active_name(s)
        return name, B.url_of(s, name), s

    def _store(self) -> B.Store:
        return B.Store(self.host.state)

    # -- subcommands -----------------------------------------------------------------------------------------------
    def cmd_topup(self, rest: list[str]) -> str:
        if len(rest) != 1:
            return "usage: x402 topup <usd>   e.g. x402 topup 5"
        usd = _usd(rest[0])
        name, url, s = self._active()
        doc = B.validate(url)
        topup_url = f"{url}/v1/topup?usd={usd:.2f}"
        res = x402.fetch("POST", topup_url, max_usd=usd, signer=self.host.signer())
        r = res.response
        if r.status_code != 200:
            try:
                err = r.json()
                err = err.get("detail", err)
                err = err.get("error", err) if isinstance(err, dict) else err
            except ValueError:
                err = r.text[:300]
            notes = f" ({'; '.join(res.notes)})" if res.notes else ""
            return f"x402 topup: {name} answered HTTP {r.status_code}: {err}{notes}"
        body = r.json()
        self._store().put(name, jwt=body["jwt"], wallet=body.get("wallet"), balance_usd=body.get("balance_usd"))
        self.host.ensure_placeholder()
        paid = res.signed or {}
        lines = [f"Topped up ${usd:.2f} on {name} from {paid.get('wallet', '?')} "
                 f"({paid.get('chain', paid.get('network', '?'))}){_explorer(res.signed, res.settlement)}",
                 f"Balance: {_money(body.get('balance_usd'))}"]
        if not self.host.main_model_usable():
            model = s.get("default_model") or B.DEFAULT_MODEL
            lines.append(self.host.set_main_model(model))
        else:
            prov, model = self.host.main_model()
            lines.append(f"Main model stays {model} ({prov}). To run on this balance: x402 use main <model>")
        return "\n".join(lines)

    def _live_balance(self, name: str, url: str) -> str:
        """The broker's own figure (the stored one can lag streamed calls). Returns a display line."""
        st = self._store().get(name)
        if not st["jwt"]:
            return f"{name}: no balance yet — run x402 topup <usd>"
        import httpx
        try:
            r = httpx.get(url + "/v1/balance", headers={"Authorization": f"Bearer {st['jwt']}"}, timeout=15)
        except httpx.HTTPError as e:
            return f"{name} balance: {_money(st['balance_usd'])} (last known; broker unreachable: {e})"
        if r.status_code == 200:
            bal = r.json().get("balance_usd")
            self._store().put(name, balance_usd=bal)
            return f"{name} balance: {_money(bal)}"
        if r.status_code == 401:
            return f"{name}: token expired — run x402 topup <usd> (any amount ≥ $0.10 renews it)"
        return f"{name}: balance check failed (HTTP {r.status_code})"

    def cmd_balance(self, rest: list[str]) -> str:
        name, url, _ = self._active()
        lines = [self._live_balance(name, url)]
        try:
            for b in self.host.signer().balances().get("balances", []):
                lines.append(f"wallet {b['wallet']} ({b['address'][:6]}…{b['address'][-4:]}): "
                             f"{_money(b['balance'])} {b['asset']} on {b['chain']}")
        except x402.X402Error as e:
            lines.append(f"wallet: {e}")
        return "\n".join(lines)

    def cmd_models(self, rest: list[str]) -> str:
        name, url, _ = self._active()
        main = self.host.main_model()
        sub = self.host.subagent_model()
        rows = []
        for m in B.models(url):
            ctx = int(m.get("max_context") or 0)
            ok = ctx >= B.MIN_AGENT_CONTEXT and m.get("available", True)
            tags = [t for t, hit in (("main", main == ("x402-broker", m["id"])),
                                     ("subagents", sub == ("x402-broker", m["id"]))) if hit]
            rows.append(f"{'✓' if ok else '·'} {m['id']:<20} {ctx // 1024:>5}K  "
                        f"${m.get('price_in_per_mtok')}/M in, ${m.get('price_out_per_mtok')}/M out"
                        f"{'  tools' if m.get('tools') else ''}{'  ← ' + ', '.join(tags) if tags else ''}")
        return f"{name} models (✓ = can run a Hermes agent, ≥64K context):\n" + "\n".join(rows)

    def cmd_use(self, rest: list[str]) -> str:
        if len(rest) != 2 or rest[0] not in ("main", "subagents"):
            return "usage: x402 use main <model> | x402 use subagents <model>"
        role, model = rest
        if role == "main" and model == "previous":
            return self.host.restore_main_model()
        if role == "subagents" and model in ("main", "inherit"):
            return self.host.set_subagents(None)
        _, url, _ = self._active()
        row = B.model_row(url, model)
        if not row:
            raise LookupError(f"{model!r} is not a chat model on this broker; see x402 models")
        if int(row.get("max_context") or 0) < B.MIN_AGENT_CONTEXT:
            raise ValueError(f"{model} has {int(row.get('max_context') or 0):,} tokens of context; "
                             f"Hermes needs at least {B.MIN_AGENT_CONTEXT:,} to run an agent on it")
        self.host.ensure_placeholder()
        return self.host.set_main_model(model) if role == "main" else self.host.set_subagents(model)

    def cmd_broker(self, rest: list[str]) -> str:
        s = self.host.settings()
        bs = B.brokers(s)
        sub = rest[0] if rest else "list"
        if sub == "list":
            active = B.active_name(s)
            out = []
            for n, b in bs.items():
                st = self._store().get(n)
                exp = B.jwt_expiry(st["jwt"]) if st["jwt"] else None
                bal = f"balance {_money(st['balance_usd'])}" if st["jwt"] else "no balance"
                if exp and exp < time.time():
                    bal += " (token expired)"
                out.append(f"{'*' if n == active else ' '} {n:<12} {b['url']}  {bal}")
            return "\n".join(out)
        if sub == "use" and len(rest) == 2:
            if rest[1] not in bs:
                raise LookupError(f"no broker named {rest[1]!r}")
            self.host.set_setting("active_broker", rest[1])
            return f"active broker is now {rest[1]} ({bs[rest[1]]['url']})"
        if sub == "add" and len(rest) == 3:
            n, u = rest[1], rest[2].rstrip("/")
            if not u.startswith("https://"):
                raise ValueError("a broker URL must be https://")
            doc = B.validate(u)
            bs[n] = {"url": u}
            self.host.set_setting("brokers", bs)
            return f"added broker {n} ({doc.get('broker', {}).get('id', '?')}) at {u}. Switch to it: x402 broker use {n}"
        if sub == "remove" and len(rest) == 2:
            if rest[1] not in bs:
                raise LookupError(f"no broker named {rest[1]!r}")
            if len(bs) == 1:
                raise ValueError("cannot remove the last broker")
            del bs[rest[1]]
            self.host.set_setting("brokers", bs)
            if B.active_name(s) == rest[1]:
                self.host.set_setting("active_broker", next(iter(bs)))
            return f"removed broker {rest[1]}"
        return "usage: x402 broker list | use <name> | add <name> <url> | remove <name>"

    def cmd_wallet(self, rest: list[str]) -> str:
        signer = self.host.signer()
        sub = rest[0] if rest else "list"
        if sub == "list":
            w = signer.wallets()
            lines = [f"{x['name']:<12} {x['address']}{'  (default)' if x['default'] else ''}" for x in w["wallets"]]
            return "\n".join(lines) or "no wallets yet — create one: x402 wallet new <name>"
        ks = getattr(signer, "keystore", None)
        if ks is None:
            return ("wallets live in the signer container here; manage them there: "
                    "docker compose run --rm x402-signer wallet list|new|import|default")
        if sub == "new" and len(rest) == 2:
            w = ks.add_evm(rest[1])
            return (f"created {w.name}: {w.address}{' (default)' if w.default else ''}\n"
                    "Fund it with USDC on Base. The key is stored under the Hermes home: back it up, keep the balance small.")
        if sub == "default" and len(rest) == 2:
            w = ks.set_default(rest[1])
            return f"default wallet is now {w.name} ({w.address})"
        if sub == "import":
            if not self.host.interactive:
                leaked = len([w for w in rest[2:] if not w.startswith("-")]) >= 11 or any(
                    len(w) >= 64 and w.lower().removeprefix("0x").isalnum() for w in rest[2:])
                warn = ("⚠️ That message looks like it contains a secret phrase or key. It is now in this chat's "
                        "history: treat that wallet as exposed and move its funds to a new one.\n\n") if leaked else ""
                return warn + ("never paste a private key or secret phrase into chat. Import from a shell on this machine:\n"
                        "  hermes x402 wallet import <name>                (private key, hidden prompt)\n"
                        "  hermes x402 wallet import <name> --phrase       (secret phrase, hidden prompt; --index N for account N)")
            return self._import(ks, rest[1:])
        return "usage: x402 wallet list | new <name> | default <name> | import <name> [--phrase] (shell only)"

    def _import(self, ks, rest: list[str]) -> str:
        try:
            from .x402signer.cli import run_import
        except ImportError:
            from x402.x402signer.cli import run_import
        return run_import(ks, rest)

    def cmd_status(self, rest: list[str]) -> str:
        name, url, s = self._active()
        signer = self.host.signer()
        mode = s.get("signer") or "local"
        lines = [f"signer: {mode}" + (" (keys under the Hermes home — keep balances small)" if mode == "local" else
                                      f" ({getattr(signer, 'url', '')})")]
        try:
            w = signer.wallets()
            lim = w.get("limits", {})
            lines.append(f"limits: ${lim.get('per_payment_usd')}/payment, ${lim.get('per_day_usd')}/24h; top-ups to "
                         f"brokers ${lim.get('topup_per_payment_usd')}/payment, ${lim.get('topup_per_day_usd')}/24h; "
                         f"networks {', '.join(lim.get('networks', []))}")
            lines.append(f"wallets: {', '.join(x['name'] for x in w['wallets']) or 'none — x402 wallet new <name>'}")
        except x402.X402Error as e:
            lines.append(f"signer unreachable: {e}")
        st = self._store().get(name)
        exp = B.jwt_expiry(st["jwt"]) if st["jwt"] else None
        lines.append(f"broker: {name} {url}; {self._live_balance(name, url).split(': ', 1)[-1]}"
                     + (f", token valid {max(0, int((exp - time.time()) // 86400))} more days" if exp else ""))
        prov, model = self.host.main_model()
        sp, sm = self.host.subagent_model()
        lines.append(f"main model: {model or 'none'} ({prov or 'no provider'})"
                     f"{'' if self.host.main_model_usable() else ' — not usable'}")
        lines.append(f"sub-agents: {sm or 'same as main'}{f' ({sp})' if sp else ''}")
        return "\n".join(lines)
