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

You need:

- Docker Compose
- A Telegram bot created with [@BotFather](https://t.me/BotFather)
- A Cloudflare account and a domain already added to Cloudflare

### 1. Create a Cloudflare Tunnel and get its token

1. In the Cloudflare dashboard, open **Networking → Tunnels** and select **Create Tunnel**.
2. Enter a name such as `telegramail`, then create the tunnel.
3. Select **Docker** as the environment. Cloudflare displays a command containing `--token eyJ...`; copy only the `eyJ...` value after `--token`. Do not run the full command. This value is your `CLOUDFLARE_TUNNEL_TOKEN`.

For an existing tunnel, open its **Overview** page and select **Add a replica** to display the installation command again. Anyone who has this token can run your tunnel, so protect it like a password. See Cloudflare's official [Tunnel setup guide](https://developers.cloudflare.com/tunnel/setup/) and [token documentation](https://developers.cloudflare.com/tunnel/advanced/tunnel-tokens/).

### 2. Download the Compose configuration

```bash
mkdir telegramail && cd telegramail
curl -fsSLO https://raw.githubusercontent.com/dale0525/telegramail/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/dale0525/telegramail/main/.env.example -o .env
mkdir -p data
sudo chown 10001:10001 data
```

### 3. Configure `.env`

Open `.env` and make sure these three values describe the same deployment:

```dotenv
TELEGRAM_BOT_TOKEN=token_from_BotFather
WEB_BASE_URL=https://mail.example.com
CLOUDFLARE_TUNNEL_TOKEN=eyJ...
```

`WEB_BASE_URL` is the full HTTPS origin you will publish through Cloudflare. Do not include a path or trailing `/`. Follow the comments in `.env` to set `SETUP_CODE`, `SESSION_SECRET`, `TELEGRAM_WEBHOOK_SECRET`, and `MASTER_KEY`; never commit the real `.env` file.

### 4. Initialize and start

```bash
docker compose pull
docker compose run --rm --no-deps telegramail python scripts/migrate_v2.py --init
docker compose up -d
```

### 5. Route the hostname to Telegramail

1. Return to Cloudflare **Networking → Tunnels** and wait for the new tunnel to show **Healthy**.
2. Open the tunnel. Under **Routes**, select **Add route → Published application**.
3. Set **Hostname** to the same hostname used by `WEB_BASE_URL`, such as `mail.example.com`.
4. For **Service URL**, select HTTP and enter `http://telegramail:8080`, then save.

Do not use `localhost:8080` here. `cloudflared` and Telegramail run in separate containers; the Compose service name `telegramail` resolves to the application container. Cloudflare creates the tunnel route for the hostname when you save the published application.

### 6. Verify and sign in

```bash
docker compose ps
curl -fsS https://mail.example.com/health/ready
```

Both containers should be running, and the second command should return JSON containing `"status":"ok"`. Send `/start` to the bot, select **Telegramail** from the chat menu to open the Mini App, enter `SETUP_CODE` once to claim the administrator account, and then add your mail accounts.

GitHub Actions publishes `ghcr.io/dale0525/telegramail`. `latest` follows `main`; a Git tag such as `v2.1.0` publishes image tag `2.1.0`. Set `TELEGRAMAIL_IMAGE_TAG=2.1.0` in `.env` to pin that version.

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
