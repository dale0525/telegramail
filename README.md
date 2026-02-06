# TelegramMail

[中文文档](./README_zh.md)

TelegramMail is a Telegram-based email tool built on top of [aiotdlib](https://github.com/pylakey/aiotdlib) that allows you to send and receive emails directly in Telegram without switching to traditional email clients. This project is designed for personal use, with each client managed and used by a single user.

## Comparison with Traditional Email Clients
### Advantages
- Lightweight
- Cross-platform support (via Telegram), one configuration works across all platforms
- Email information and attachments are stored in Telegram, enjoying Telegram's permanent cloud storage
- Even if you lose access to your email account (e.g., after leaving a company), you can still view historical emails
- Can implement AI-related features using LLM APIs

### Disadvantages
- If there are many recipients (especially large BCC lists), it may send multiple emails in batches
- No WYSIWYG editor for composing (currently authored in Markdown)

## Features

- [x] Add multiple email accounts
- [x] Receive emails and forward to Telegram
- [x] Delete emails
- [x] Compose new emails
- [x] Fetch emails on schedule
- [x] Manually refresh emails
- [x] Reply to emails
- [x] Forward emails
- [x] Receive emails from folders other than INBOX
- [ ] Fetch all emails
- [ ] Set signature for each email account
- [x] Display email folder information
- [x] Summarize emails using LLM
- [x] Label emails using LLM
- [ ] Write emails using LLM
- [ ] Search emails
- [x] Extract important links from emails (via LLM)

## Installation and Deployment

### Prerequisites

- Telegram Bot Token (obtained via @BotFather)
- Telegram App ID, Telegram App Hash (need to [apply](https://core.telegram.org/api/obtaining_api_id))
> Why do we need Telegram App ID instead of just using Telegram Bot?
> 1. Telegram Bot cannot delete messages older than 48 hours
> 2. Telegram Bot cannot listen to message deletion events
- (Optional) OpenAI-compatible LLM API for AI features such as email summarization and label classification

### Local Development
**Windows is not yet supported for local development. If you need Windows support, please compile TDLib library files yourself (or use WSL/Docker).**

1. [Install pixi](https://pixi.sh/)

1. Clone the repository:
   ```bash
   git clone https://github.com/dale0525/telegramail.git
   cd telegramail
   ```

2. Create configuration file:
   ```bash
   cp .env.example .env
   ```

3. Edit the `.env` file and fill in your configuration:
   ```
   TELEGRAM_API_ID=your_telegram_api_id_here
   TELEGRAM_API_HASH=your_telegram_api_hash_here
   TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here

   # Optional TDLib settings
   TELEGRAM_CHAT_EVENT_LOG_TIMEOUT=30  # increase if you see TimeoutError when scanning deleted topics

   # Optional LLM settings
   ENABLE_LLM_SUMMARY=0    # set to 1 to enable LLM analysis (summary + labels + links)
   LLM_SUMMARY_THRESHOLD=200  # if email content is longer than this threshold, use LLM analysis
   OPENAI_BASE_URL=your_openai_base_url_here
   OPENAI_API_KEY=your_openai_key_here
   OPENAI_EMAIL_SUMMARIZE_MODELS=first_model_to_summarize_email_content,second_model_to_summarize_email_content,...   # if the first model fails, try the second one
   ```

4. Init development environment:
   ```bash
   # Install dependencies and setup TDLib libraries
   pixi install --locked
   pixi run init
   ```

5. Start the application:
   ```bash
   # Start development server
   pixi run dev
   ```

6. Check i18n:
   ```bash
   pixi run i18n
   ```

#### (Optional) Local Container Runtime (macOS: Lima + Docker, driven by pixi)

If you want a lightweight Docker setup on macOS (without Docker Desktop), you can run Docker Engine inside a Lima VM and keep all VM state under this repo’s `.pixi/` directory.

1. Install Lima (the only system prerequisite):
   ```bash
   brew install lima
   ```

2. Initialize the project-scoped Docker engine (data dir: `.pixi/lima`):
   ```bash
   pixi run container-init
   ```

3. Use Docker / Compose via pixi (does not touch `~/.lima` or `~/.docker`):
   ```bash
   pixi run docker -- version
   pixi run docker -- ps

   pixi run compose -- version
   pixi run compose -- up -d
   pixi run compose -- down
   ```

Optional environment variables (tune resources / instance name):
- `TELEGRAMAIL_LIMA_INSTANCE` (default `telegramail-docker`)
- `TELEGRAMAIL_LIMA_CPUS` (default `2`)
- `TELEGRAMAIL_LIMA_MEMORY` (GiB, default `2`)
- `TELEGRAMAIL_LIMA_DISK` (GiB, default `20`)

#### TDLib Management

The project includes automated TDLib library management for cross-platform development:

- **Automatic Setup**: The setup_tdlib.py script automatically detects your platform and configures the appropriate TDLib libraries
- **Separate Libraries**: Creates separate library files for bot and user clients (required by aiotdlib limitation)
- **Cross-Environment**: Works consistently in both development and production/container environments

> On Linux, the TDLib shared library also depends on runtime libraries such as the C++ runtime, OpenSSL, zlib (installed via pixi/conda), and LLVM libunwind (`libunwind.so.1`), which needs to come from your system packages (Debian/Ubuntu: `libunwind-14`, or any package that provides `libunwind.so.1`).

**Platform Support**:
- ✅ **macOS**: Automatic setup from included ARM64 library
- ✅ **Linux**: Automatic setup for both AMD64 and ARM64
- ⚠️ **Windows**: Manual TDLib compilation required (or use WSL/Docker)

### Production Deployment

Use Docker Compose for production deployment with pre-built images from Docker Hub:

1. Create deployment directory:
   ```bash
   mkdir telegramail && cd telegramail
   ```

2. Download Docker Compose configuration:
   ```bash
   curl -O https://raw.githubusercontent.com/dale0525/telegramail/main/docker-compose.yml
   curl -o .env https://raw.githubusercontent.com/dale0525/telegramail/main/.env.example
   ```

3. Edit the `.env` file. See [Local Development](#Local Development) for details.

4. Create data directory and set permissions:
   ```bash
   mkdir data && chmod -R 755 data
   ```

5. Deploy:
   ```bash
   docker-compose up -d
   ```

> If you want to build the image from source instead: `docker build -t telegramail .`. The project's `Dockerfile` installs dependencies using `pixi.toml` + `pixi.lock` to keep dev/prod dependencies aligned.

## Usage

### Telegram Bot Commands

- `/start` - First-time startup command, will guide users to log in to their Telegram account and add their first email account
- `/help` - Display help information
- `/accounts` - Manage added email accounts or add new ones
- `/check` - Manually check for new emails
- `/compose` - Compose a new email (creates a Draft topic)

### Adding Email Accounts

1. Use the `/accounts` command and select "Add new account"
2. Follow the prompts to select your email service provider, then enter your email address, password/App-specific password, and Alias (for display only). If you choose a custom email service, you'll need to additionally enter IMAP and SMTP server, port, and whether to use SSL. Currently, [pre-defined configs](./app/email_utils/common_providers.py) include:
   - Gmail
   - Outlook/Hotmail
   - Yahoo
   - iCloud
   - Zoho Mail
   - AOL
   - GMX
   - QQ Mail
   - Netease
   - Tencent Exmail
   - Alimail
   - Yandex
   - Linux DO
3. Telegramail will create a group in Telegram named after your Alias, and add a folder named Email
   ![20250428150635](https://imagehost.daletan.win/20250428150635.png)

### Receiving Emails

TelegramMail regularly checks for unread emails in the INBOX (default) and sends new emails to the Telegram group named after your email account Alias. Each email is presented as a Forum Topic, including:
- Email subject, sender, and recipient information
- Email summary (if LLM is configured)
- Email body
  - If in HTML format, sent as an HTML file
  - If in plain text format, sent directly as plain text
- Attachments
![20250428160226](https://imagehost.daletan.win/20250428160226.png)

You can change the frequency of regular checks by modifying `POLLING_INTERVAL` in the `.env` file. The default is 300 seconds.

To monitor additional IMAP folders, set `TELEGRAMAIL_IMAP_MONITORED_MAILBOXES` (comma-separated), e.g. `INBOX,Archive,Spam`.
You can also set per-account overrides via `/accounts` → select an account → **IMAP Folders** (includes “Detect folders” + an interactive picker).

### Manually Fetching Emails
#### Manually Fetch All Emails
Use the `/check` command to manually check for new emails for all added email accounts

#### Manually Fetch Specific Email Account
Use the `/accounts` command, then click on the email account you want to manually fetch emails for, and then click the "Manual Fetch Email" button

### Delete Emails

If you want to delete an email, just delete the corresponding Topic in Telegram. Telegramail will search for deleted topics every 3 minutes, clean the database and delete related emails from the mail server.

### Compose / Reply / Forward (Draft)

TelegramMail uses Draft topics for composing, replying, and forwarding:

1. **Compose new email**
   - Send `/compose` inside an account group (in any topic, e.g. “General”). In group chats Telegram may auto-complete it as `/compose@YourBot` — both work. It creates a new Draft topic and pins a Draft card message (Send/Cancel).
2. **Reply / Forward**
   - Each email thread has an “Actions” message with Reply / Forward buttons. Click to create a Draft in the same thread.
3. **Edit inside Draft topic**
   - `/from`: open a From-identity selector (for alias scenarios)
   - `/from b@example.com`: set From identity directly
   - `/to ...`, `/cc ...`, `/bcc ...`: set recipients (multiple addresses are comma-separated, e.g. `/to a@example.com, b@example.com`; same for `/cc` and `/bcc`)
   - `/subject ...`: set subject
   - Body: send normal text messages; they are appended to the email body (Markdown supported)
   - Attachments: send files/photos/audio in the Draft topic; they will be attached to the email; use `/attachments` to manage/remove attachments
4. **Send**
   - Click Send on the Draft card to send; Cancel to discard

#### Body formatting (Markdown → HTML)

Draft body is authored in Markdown. When sending, TelegramMail includes:
- `text/plain`: original Markdown
- `text/html`: rendered HTML from Markdown

### AI

If you've configured LLM-related parameters in `.env`, you can use AI-related features. Current features include:
- Using LLM to summarize email content (summary, priority, action items, deadline, and key contacts).
- Using LLM to classify email labels (`category`) with built-in classes: `task`, `meeting`, `financial`, `travel`, `newsletter`, `system`, `social`, `other`.
- Using LLM to extract important links from emails (for example, unsubscribe links).
- Use `/label` inside Telegram to filter recent emails by label, then locate/reply/forward directly.
- Prompt is located at [app/email_utils/llm.py](./app/email_utils/llm.py).

### Localization

Translation texts are located in the [app/i18n](./app/i18n/) folder. You can add translations for your target language and set `DEFAULT_LANGUAGE` to your language in the `.env` file.

## Known Issues
- Due to instability of LLM output, AI analysis (summary/labels/links) may contain invalid JSON or HTML tags that cannot be parsed by Telegram, which can cause message sending failure. If you want to use this feature, try more reliable models.

## License
This project is licensed under the GPL License - see the [LICENSE](LICENSE) file for details
