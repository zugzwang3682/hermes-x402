# Installing hermes-x402

A walkthrough from nothing to a Hermes agent that runs on an x402 broker, funded from its own USDC wallet, driven
from Slack. Every command here was run end to end on Hermes Agent v0.21.5 in the official Docker image.

## 1. Hermes

Use the official image and data layout ([Hermes Docker docs](https://hermes-agent.nousresearch.com/docs/user-guide/docker)).
Hermes keeps everything (config, `.env`, sessions, plugins and the plugin's wallet) in one data directory, mounted at
`/opt/data`. Already running Hermes, natively or in Docker? Skip to step 2.

`compose.yaml`:

```yaml
services:
  hermes:
    image: nousresearch/hermes-agent:latest
    container_name: hermes
    restart: unless-stopped
    command: gateway run
    shm_size: 1g
    volumes:
      - ~/.hermes:/opt/data
```

```bash
mkdir -p ~/.hermes
docker compose up -d          # first start writes config.yaml and .env into ~/.hermes
```

**No model is needed.** The x402 plugin tops up and picks a model from chat. `hermes setup`, the interactive wizard,
asks for a model provider; skip that step, or unset what it wrote:

```bash
docker exec -u hermes hermes hermes config unset model.default
docker exec -u hermes hermes hermes config unset model.provider
docker exec -u hermes hermes hermes config unset model.base_url
```

Run Hermes commands **as the `hermes` user** (`docker exec -u hermes …`), so files it writes stay owned by it.

### Slack

Create a Slack app from the manifest Hermes generates (Socket Mode, so no public URL is needed):

```bash
docker exec -u hermes hermes hermes slack manifest --agent-view --write   # writes ~/.hermes/slack-manifest.json
```

In Slack's app settings, create the app from that manifest, install it to the workspace, then put three values in
`~/.hermes/.env` (mode 600):

```
SLACK_BOT_TOKEN=xoxb-…          # OAuth & Permissions → Bot User OAuth Token
SLACK_APP_TOKEN=xapp-…          # Basic Information → App-Level Tokens (scope connections:write)
SLACK_ALLOWED_USERS=U0…         # your member ID: profile → ⋯ → Copy member ID
```

```bash
docker compose restart
docker compose logs hermes | grep -i slack     # expect: ✓ slack connected
```

Only one Hermes may connect with a given app token: Slack splits events between connections.

## 2. The plugins

Two plugins from this repo, installed with Hermes's own installer. It clones the subdirectory and installs each one's
Python dependencies from its `pyproject.toml` (`eth-account` for the wallet, `openai` for the provider):

```bash
docker exec -u hermes hermes hermes plugins install zugzwang3682/hermes-x402/x402 --enable
docker exec -u hermes hermes hermes plugins install zugzwang3682/hermes-x402/x402-broker --enable
```

Hermes warns that the source isn't from its catalog; that's expected for a plugin installed straight from GitHub. The
gateway picks both up without a restart. Check:

```bash
docker exec -u hermes hermes hermes x402 status
```

```
signer: local (keys under the Hermes home — keep balances small)
limits: $1.0/payment, $5.0/24h; top-ups to brokers $20.0/payment, $20.0/24h; networks eip155:8453
wallets: none — x402 wallet new <name>
broker: autumn8 https://x402.autumn8.net; balance none
main model: none (no provider) — not usable
```

Update later with `hermes plugins update x402` and `hermes plugins update x402-broker`.

## 3. A wallet

The wallet holds USDC on Base; it needs no ETH (the broker's facilitator pays gas). Create or import it **from a shell**:
`/x402` refuses keys and secret phrases in chat.

```bash
# a new wallet (prints only the address; then send it USDC on Base)
docker exec -u hermes hermes hermes x402 wallet new hermes

# or import an existing one: a secret phrase (hidden prompt; --index N for account N+1 in MetaMask/Rabby)
docker exec -it -u hermes hermes hermes x402 wallet import hermes --phrase

# or a private key (hidden prompt), or from a file holding `0x…` or `NAME=0x…`
docker exec -it -u hermes hermes hermes x402 wallet import hermes
docker exec -i  -u hermes hermes hermes x402 wallet import hermes --stdin < wallet.key
```

With the built-in signer the key is stored under `~/.hermes/plugin-data/x402/signer/wallets/` (mode 600). The agent's
terminal can read files under the Hermes home, so keep the balance small, or run the key-holding container instead
(`signer/`, then `hermes config set plugins.entries.x402.settings.signer remote`).

## 4. From Slack

_(filled in during the verified install)_
