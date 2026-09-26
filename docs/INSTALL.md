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

_(filled in during the verified install)_

## 3. A wallet

_(filled in during the verified install)_

## 4. From Slack

_(filled in during the verified install)_
