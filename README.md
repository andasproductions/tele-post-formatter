# tele-post-formatter

A Telegram bot that formats text for Twitter, Bluesky, and Instagram. Detects names with spaCy, looks up handles via Bluesky API and Google (Serper), and splits long posts correctly per platform.

## Setup

### 1. Create a Telegram bot
Talk to [@BotFather](https://t.me/BotFather), create a bot, copy the token.

### 2. Get your Telegram user ID

Talk to [@userinfobot](https://t.me/userinfobot) to get your numeric user ID.

### 3. Get a Serper API key
Sign up at [serper.dev](https://serper.dev) and copy your API key.

### 4. (Optional) Get an Anthropic API key
Required only if you enable `auto_emoji`. Sign up at [console.anthropic.com](https://console.anthropic.com).

### 5. Local development

```bash
cp .env.example .env
# fill in TELEGRAM_BOT_TOKEN, ADMIN_USER_ID, SERPER_API_KEY, and optionally ANTHROPIC_API_KEY

uv sync
uv run python -m spacy download en_core_web_md
uv run --env-file .env python bot.py
```

`config.json` will be created automatically in the project root on first run.

### 6. Deploy to Railway

1. Push this repo to GitHub and create a new Railway project from the repo
2. Add a **Volume** in Railway, mounted at `/data`
3. Set these service variables in Railway:
   - `TELEGRAM_BOT_TOKEN`
   - `ADMIN_USER_ID`
   - `SERPER_API_KEY`
   - `ANTHROPIC_API_KEY` (optional, for auto-emoji)
   - `DATA_DIR` → `/data`
4. Deploy.

Railway will build the Docker image, install dependencies, and download the spaCy model at build time. `config.json` will be created at `/data/config.json` on first run and persist across redeploys.

## Usage

- **Send any text** → bot detects names, lets you select which to look up, finds handles, asks you to confirm, then sends formatted output
- **/start** → run the setup wizard (platforms, prefixes, suffixes, ignored names)
- **/config** → same as /start, for updating settings
- **/users** → (admin only) manage which Telegram users can access the bot
- **/cancel** → abort current operation

Editable via `/start` or `/config` in Telegram.

- **enabled**: toggle a platform on/off
- **allowed_users**: Telegram user IDs permitted to use the bot (admin access only)
- **auto_paragraph**: reflow text into paragraphs before formatting
- **auto_emoji**: prepend an apt emoji to each paragraph using Claude (requires `ANTHROPIC_API_KEY`)

## Character limits

| Platform  | Limit | Split format |
|-----------|-------|--------------|
| Twitter   | 280   | `…\n\nn/total` on non-final chunks |
| Bluesky   | 300   | `…\n\nn/total` on non-final chunks |
| Instagram | —     | no split |

Splits happen **after** handle substitutions so character counts are accurate.
