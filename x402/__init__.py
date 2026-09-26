"""x402 for Hermes: pay x402 (HTTP 402) resources and x402 inference brokers in USDC from this agent's own wallet.

- Tools: x402_inspect (free price check), x402_fetch (pay, at most max_usd; above X402_AUTO_APPROVE_USD it waits for
  the user's approval), x402_wallets.
- /x402 command (works with no model configured): topup a broker balance, pick broker models for the main agent or
  sub-agents, manage brokers and wallets. The companion x402-broker model provider runs models on that balance.
- Signer: built-in (keys under the Hermes home) by default, or a separate x402-signer container (settings.signer:
  remote). Both enforce per-payment and daily limits, with a separate top-up allowance for broker addresses."""
from __future__ import annotations

from . import tools as _t
from .commands import HELP, Commands
from .host import HermesHost

_TOOLS = (
    ("x402_inspect", _t.X402_INSPECT_SCHEMA, _t.handle_inspect, "🔎"),
    ("x402_fetch", _t.X402_FETCH_SCHEMA, _t.handle_fetch, "💸"),
    ("x402_wallets", _t.X402_WALLETS_SCHEMA, _t.handle_wallets, "👛"),
)


def _cli_setup(parser) -> None:
    parser.add_argument("args", nargs="*", help="x402 subcommand and arguments, e.g. `topup 5` or `wallet import me`")


def register(ctx) -> None:
    host = HermesHost(ctx)
    _t._signer_factory = host.signer
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(name=name, toolset="x402", schema=schema, handler=handler, check_fn=_t.check_available,
                          emoji=emoji)
    ctx.register_hook("pre_tool_call", _t.pre_tool_call)
    ctx.register_command("x402", lambda raw: Commands(host).run(raw),
                         description="x402: top up a broker balance, pick models, wallets (x402 help)",
                         args_hint="topup <usd> | balance | models | use main|subagents <model> | status")

    def _cli(args) -> None:
        print(Commands(HermesHost(ctx, interactive=True)).run(" ".join(args.args)))

    ctx.register_cli_command("x402", help="x402 payments: topup, balance, models, wallets", setup_fn=_cli_setup,
                             handler_fn=_cli, description=HELP)
