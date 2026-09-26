# hermes-x402

Pay-as-you-go models and x402 payments for [Hermes Agent](https://github.com/NousResearch/hermes-agent), funded
in USDC from the agent's own wallet. Free and MIT-licensed. Preconfigured for the
[Autumn Eight broker](https://x402.autumn8.net); add or swap in any x402 broker.

```
!x402 topup 5            # Slack (or /hermes x402 topup 5; /x402 topup 5 in the CLI)
Topped up $5.00 on autumn8 from hermes (Base) — https://basescan.org/tx/0x…
Balance: $5.0000
Main model set to gpt-oss-120b on the x402 broker.
```

`/x402` works **with no model configured**, so a fresh Hermes can fund itself and start running on a broker model
from a single chat command.

## What's in it

| Directory | Install | What it does |
|---|---|---|
| `x402/` | `hermes plugins install <repo>/x402` | `/x402` command, `x402_inspect` / `x402_fetch` / `x402_wallets` tools, approval hook, built-in signer |
| `x402-broker/` | `hermes plugins install <repo>/x402-broker` | Model provider: Hermes runs on broker models, billed per token to the balance |
| `signer/` | optional container | Holds the keys in a separate container so the agent can never read them |

### `/x402`

| | |
|---|---|
| `topup <usd>` | Pay the active broker from the wallet (x402), store the balance token. If no usable main model is set, set it to `default_model` (gpt-oss-120b) |
| `balance` | Broker balance and on-chain wallet USDC |
| `models` | Broker models, marking those with ≥64K context (Hermes's minimum for an agent) |
| `use main <model>` / `use subagents <model>` | Run the main agent or delegated sub-agents on a broker model |
| `broker list \| use <name> \| add <name> <https url> \| remove <name>` | Manage brokers; `add` checks the broker's `/.well-known/broker.json` |
| `wallet list \| new <name> \| default <name>` | Built-in signer wallets |
| `wallet import <name> [--phrase [--index N \| --path P] [--passphrase]]` | Shell only (`hermes x402 wallet import …`; container: `wallet import …`). A private key, or one account derived from a 12-24 word secret phrase at `m/44'/60'/0'/0/N` (MetaMask/Rabby account N+1). Only the derived key is stored, never the phrase. Refused in chat, with a warning if a phrase or key was pasted there |
| `status` | Signer, limits, broker, token expiry, which models are in use |

When the balance runs out, turns fail with "x402 balance … is too low — run `x402 topup <usd>`". Nothing tops up on
its own. A top-up of any amount (≥ $0.10) also renews the 30-day balance token.

### Money and keys

- **Signer, built-in (default):** keys in `<HERMES_HOME>/plugin-data/x402/signer`, mode 600. Easy, but the agent's
  terminal can read files under the Hermes home, so **keep the balance small**.
- **Signer, container:** set `plugins.entries.x402.settings.signer: remote` and `signer_url`. Run `signer/` as a
  container with no published ports; the keys never enter the Hermes process.
- **Hard limits** are enforced by the signer, whoever asks:
  - $1 per payment and $5 per 24h to anyone;
  - **top-ups** to a configured broker's receiving address, $20 per payment and $20 per 24h. That money becomes
    your own balance, so it gets a separate allowance.

  The signer checks the payee itself, so nothing can reach the top-up allowance by paying anyone else.
- `x402_fetch` payments whose ceiling is above `X402_AUTO_APPROVE_USD` (default $0.05) wait for the user's approval in
  Slack or the CLI.
- **Coverage:** known USDC contracts on Base and Ethereum (and their testnets) only. A wallet needs USDC and no ETH:
  the facilitator pays the gas.

### Settings (`plugins.entries.x402.settings` in config.yaml)

```yaml
brokers:
  autumn8: {url: https://x402.autumn8.net}    # the default; add your own with /x402 broker add
active_broker: autumn8
default_model: gpt-oss-120b
signer: local                                 # or remote + signer_url
limits: {per_payment_usd: 1, per_day_usd: 5, topup_per_payment_usd: 20, topup_per_day_usd: 20, networks: [eip155:8453]}
```

The container signer takes the same limits from `X402_MAX_PER_PAYMENT_USD`, `X402_MAX_PER_DAY_USD`, `X402_NETWORKS`,
`X402_TOPUP_PAYEES` (broker receiving addresses), `X402_MAX_TOPUP_USD` and `X402_MAX_TOPUP_PER_DAY_USD`.

### Notes

- **Placeholder key.** Hermes wants an API key for the provider before it builds a client. `/x402` writes the
  placeholder `X402_BROKER_API_KEY=x402-balance` to the profile's `.env`, and the provider sends the balance token
  instead.
- **Balance mode only.** The provider runs on the balance, not per call. Broker per-call terms cap context at 8-32K,
  which is under Hermes's 64K minimum; per-call payments remain available to the model through `x402_fetch`.
- **Session overrides.** A chat that ran `/model` keeps its own model until `/new`.

## Develop

```bash
uv run --python 3.12 --with 'eth-account>=0.13,<0.14' --with httpx --with openai --with pytest pytest -q tests
```

The tests drive the command, both signers and the provider against a fake broker with the real balance flow (x402
top-up → token → metered chat). Nothing touches a chain. `x402/x402signer` is a copy of `signer/x402signer`, and a
test fails if they drift.
