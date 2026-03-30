# tele-post-formatter

A Telegram bot that formats text for Twitter, Bluesky, and Instagram. Detects names with spaCy, looks up handles via Bluesky API and Google (Serper), and splits long posts correctly per platform.

## Setup

### 1. Create a Telegram bot
Talk to [@BotFather](https://t.me/BotFather), create a bot, copy the token.

### 2. Get your Telegram user ID

Talk to [@userinfobot](https://t.me/userinfobot) to get your numeric user ID.

### 3. Get a Serper API key
Sign up at [serper.dev](https://serper.dev) and copy your API key.

### 4. Local development

```bash
cp .env.example .env
# fill in TELEGRAM_BOT_TOKEN, ADMIN_USER_ID, and SERPER_API_KEY in .env

uv sync
uv run python -m spacy download en_core_web_sm
uv run --env-file .env python bot.py
```

`config.json` will be created automatically in the project root on first run.

### 5. Deploy to Railway

1. Push this repo to GitHub and create a new Railway project from the repo
2. Add a **Volume** in Railway, mounted at `/data`
3. Set these service variables in Railway:
   - `TELEGRAM_BOT_TOKEN`
   - `ADMIN_USER_ID`
   - `SERPER_API_KEY`
   - `DATA_DIR` → `/data`
4. Deploy.

Railway will build the Docker image, install dependencies, and download the spaCy model at build time. `config.json` will be created at `/data/config.json` on first run and persist across redeploys.

## Usage

- **Send any text** → bot detects names, looks up handles, asks you to confirm, then sends formatted output
- **/config** → edit prefixes, suffixes, and ignored names per platform
- **/cancel** → abort current operation

## Config (`config.json`)

```json
{
  "twitter":   { "prefix": "", "suffix": "" },
  "bluesky":   { "prefix": "", "suffix": "" },
  "instagram": { "prefix": "", "suffix": "" },
  "ignored_names": ["OpenAI", "White House"]
}
```

Editable via `/config` in Telegram — no need to touch the file directly.

## Character limits

| Platform  | Limit | Split format |
|-----------|-------|--------------|
| Twitter   | 280   | `…\n\nn/total` on non-final chunks |
| Bluesky   | 300   | same |
| Instagram | —     | no split |

Splits happen **after** handle substitutions so character counts are accurate.
