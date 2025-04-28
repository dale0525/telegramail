# TelegramMail

TelegramMail is a Telegram-based email tool that allows you to send and receive emails directly in Telegram without switching to traditional email clients. This project is designed for personal use, with each client managed and used by a single user.

## Comparison with Traditional Email Clients
### Advantages
- Lightweight
- Cross-platform support (via Telegram), one configuration works across all platforms
- Email information and attachments are stored in Telegram, enjoying Telegram's permanent cloud storage
- Even if you lose access to your email account (e.g., after leaving a company), you can still view historical emails
- Can implement AI-related features using LLM APIs

### Disadvantages
- Cannot handle emails with a large number of recipients, CC, or BCC
- Not convenient for composing emails, no WYSIWYG editor

## Features

- [x] Add multiple email accounts
- [x] Receive emails and forward to Telegram
- [x] Delete emails
- [ ] Compose new emails
- [x] Fetch emails on schedule
- [x] Manually refresh emails
- [ ] Reply to emails
- [ ] Forward emails
- [ ] Receive emails from folders other than INBOX
- [ ] Fetch all emails
- [ ] Set signature for each email account
- [ ] Display email folder information
- [x] Summarize emails using LLM
- [ ] Label emails using LLM
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
- (Optional) OpenAI-compatible LLM API for AI features such as email summarization

#### Deployment with Python

1. Clone the repository:
   ```bash
   git clone https://github.com/dale0525/telegramail.git
   cd telegramail
   ```
2. Create configuration file:
   ```bash
   cp .env.example .env
   ```
3. Edit the `.env` file and fill in your Telegram Bot Token and Telegram ID:
   ```
   TELEGRAM_API_ID=your_telegram_api_id_here
   TELEGRAM_API_HASH=your_telegram_api_hash_here
   TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
   ```
4. (Optional) Fill in the LLM base url and api key. `OPENAI_EMAIL_SUMMARIZE_MODELS` is a list of models used to summarize emails, separated by commas. If the first model fails to call, the next one will be used.
   ```
   ENABLE_LLM_SUMMARY=0    # set to 1 to enable email summary using llm
   OPENAI_BASE_URL=your_openai_base_url_here
   OPENAI_API_KEY=your_openai_key_here
   OPENAI_EMAIL_SUMMARIZE_MODELS=1st_model_to_summarize_email_content,2nd_model_to_summarize_email_content
   ```
5. Install dependencies:
   ```
   pip install -r requirements.txt

   # Or, if you have mise installed:
   # mise run pip
   ```
6. Start:
   ```
   python -m app.main

   # Or, if you have mise installed:
   # mise run dev
   ```

## Usage

### Telegram Bot Commands

- `/start` - First-time startup command, will guide users to log in to their Telegram account and add their first email account
- `/help` - Display help information
- `/accounts` - Manage added email accounts or add new ones
- `/check` - Manually check for new emails

### Adding Email Accounts

1. Use the `/accounts` command and select "Add new account"
2. Follow the prompts to select your email service provider, then enter your email address, password/App-specific password, and Alias (for display only). If you choose a custom email service, you'll need to additionally enter IMAP and SMTP server, port, and whether to use SSL
3. Telegramail will create a group in Telegram named after your Alias, and add a folder named Email
   ![20250428150635](https://imagehost.daletan.win/20250428150635.png)

### Receiving Emails

TelegramMail regularly checks for unread emails in the INBOX and sends new emails to the Telegram group named after your email account Alias. Each email is presented as a Forum Topic, including:
- Email subject, sender, and recipient information
- Email summary (if LLM is configured)
- Email body
  - If in HTML format, sent as an HTML file
  - If in plain text format, sent directly as plain text
- Attachments

You can change the frequency of regular checks by modifying `POLLING_INTERVAL` in the `.env` file. The default is 300 seconds.

### AI

If you've configured LLM-related parameters in `.env`, you can use AI-related features. Current features include:
- Using LLM to summarize email content. Prompt is located at [app/email_utils/llm.py](./app/email_utils/llm.py).

### Localization

Translation texts are located in the [app/i18n](./app/i18n/) folder. You can add translations for your target language and set `DEFAULT_LANGUAGE` to your language in the `.env` file.

## Known Issues
- Due to instability of LLM output, it may generate invalid JSON response, or HTML tags that cannot be parsed by Telegram. In such case email summary will not be sent. If you want to use this feature, try to use more intelligent models.

## License
This project is licensed under the GPL License - see the [LICENSE](LICENSE) file for details
