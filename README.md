# Telegramail

[简体中文](./README_zh.md)

Telegramail puts your email in Telegram. One self-hosted service can watch several IMAP/SMTP accounts, send new mail to your bot, and give you a Mini App for reading, writing, replying, and forwarding.

![A Telegramail email and its generated summary inside Telegram](./docs/images/telegramail-inbox.png)

## Is Telegramail for you?

Telegramail is built for one person who already uses Telegram, manages multiple email accounts, and is comfortable running Docker on a server.

It is not a team inbox or a multi-user mail server. Telegram keeps a useful second copy of synchronized messages, but it does not replace backups of your local `data/` directory.

## Why Telegramail?

- **One lightweight inbox in Telegram.** New mail arrives as Telegram messages; the Mini App handles longer threads, attachments, composing, replies, forwards, and account settings.
- **Self-hosted and under your control.** Mail credentials are encrypted with your master key. Credentials and indexed mail data stay in a local SQLite database on your server.
- **A second copy in Telegram.** Telegramail sends synchronized messages and supported attachments to Telegram. If a source mailbox later becomes unavailable, copies already delivered to Telegram can still be viewed there. Items beyond Telegram's delivery limits remain in local storage.

Telegramail uses the Telegram Bot API and Mini Apps only. It does not need your personal Telegram phone number, API ID, API hash, or TDLib session.

## What it supports

- Multiple Gmail, QQ Mail, Outlook/Microsoft 365, iCloud, and custom IMAP/SMTP accounts
- UID-based polling with a configurable interval and durable per-mailbox cursors
- Compose, reply, forward, To/Cc/Bcc, Markdown, signatures, and attachments
- Optional summaries, categories, and important-link extraction through an OpenAI-compatible provider
- Encrypted account credentials and local SQLite storage

## Quick start with the pre-built image

You need Docker Compose, a bot created with [@BotFather](https://t.me/BotFather), and a public HTTPS hostname. Create a named Cloudflare Tunnel, route that hostname to `http://telegramail:8080`, and keep its connector token.

```bash
mkdir telegramail && cd telegramail
curl -fsSLO https://raw.githubusercontent.com/dale0525/telegramail/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/dale0525/telegramail/main/.env.example -o .env
mkdir -p data
sudo chown 10001:10001 data
```

Edit `.env` and set the bot token, public URL, Cloudflare Tunnel token, setup code, session secrets, and master key. The comments in the file explain how to generate each secret.

```bash
docker compose pull
docker compose run --rm --no-deps telegramail python scripts/migrate_v2.py --init
docker compose up -d
```

Open `https://your-host/health/ready`; a ready instance returns HTTP 200. Then send `/start` to your bot, open the Mini App, enter `SETUP_CODE` once to claim the administrator account, and add your mail accounts from Settings.

GitHub Actions publishes `ghcr.io/dale0525/telegramail`. `latest` follows `main`; a Git tag such as `v2.1.0` publishes image tag `2.1.0`. Set `TELEGRAMAIL_IMAGE_TAG=2.1.0` in `.env` to pin that version.

The commands above assume the GHCR package is public. GitHub creates it as private on the first publication, so the project owner must change its visibility to Public once before anonymous pulls will work.

To update an existing v2 installation:

```bash
sudo chown -R 10001:10001 data
docker compose pull
docker compose run --rm --no-deps telegramail python scripts/migrate_v2.py --upgrade
docker compose up -d
```

Upgrading from the TDLib-based v1 release? Read the [v1 to v2 migration guide](./docs/migration-v1-to-v2.md).

## License

[GPL-3.0](./LICENSE)
