# TelegramMail

TelegramMail 是一个基于 Telegram Bot 的邮件收发工具，让你可以直接在 Telegram 中收发邮件，无需切换到传统邮件客户端。本项目设计为个人使用，一个Bot只能由一个用户管理和使用。

## 功能

- [x] 添加多个邮箱
- [x] 接收邮件并转发到 Telegram
- [x] 删除邮件
- [x] 撰写新邮件
- [x] 撰写邮件正文支持 Markdown
- [x] 定时获取邮件
- [x] 手动刷新邮件
- [ ] 回复邮件
- [ ] 转发邮件
- [ ] 配置单个邮箱需要接收的文件夹
- [ ] 获取所有邮件
- [ ] 新邮件推送
- [ ] 为每个邮箱设置签名，支持 Markdown
- [ ] 添加邮箱后立即启动邮箱设置
- [ ] 通过 Telegram 配置邮箱设置
- [ ] 邮件信息中显示邮箱所在文件夹
- [ ] 使用 LLM 判断邮件标签
- [ ] 使用 LLM 写邮件

## 安装与部署

### 环境要求

- Python 3.9+
- Docker 和 Docker Compose (推荐)
- Telegram Bot Token

### 使用 Docker 部署

1. 克隆仓库:
   ```bash
   git clone https://github.com/dale0525/telegramail.git
   cd telegramail
   ```
2. 创建配置文件:
   ```bash
   cp .env.example .env
   ```
3. 获取你的Telegram Bot Token:
   通过 @BotFather 新建 Telegram 机器人，并获取 Token
4. 获取你的 Telegram ID:
   通过 @getuseridbot 获取你的 Telegram ID
5. 编辑 `.env` 文件，填入你的 Telegram Bot Token和Telegram ID:
   ```
   TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
   OWNER_CHAT_ID=your_telegram_id_here
   ```
6. 使用 Docker Compose 启动:
   ```bash
   docker-compose up -d
   ```

### 本地开发

1. 克隆仓库，配置好.env
2. (Optional) 使用 mise 配置开发环境:
   1. [安装 mise](https://mise.jdx.dev/getting-started.html)
   2. 使用 mise 配置本地开发环境:
   ```bash
   mise install
   ```
3. 安装依赖:
   ```bash
   pip install -r requirements.txt
   ```
4. 运行程序:
   ```bash
   # 使用mise:
   mise run dev
   # 或者不使用 mise:
   python run.py
   ```

## 使用方法

### Telegram Bot 命令

- `/help` - 显示帮助信息
- `/addaccount` - 添加邮箱账户
- `/accounts` - 列出已添加的账户
- `/compose` - 撰写新邮件
- `/check` - 手动检查新邮件

### 添加邮箱账户

1. 在 Telegram 中向你的 Bot 发送 `/addaccount` 命令
2. 按照提示输入邮箱地址、IMAP/SMTP 服务器信息和账户凭证
3. Bot 会验证连接并保存账户信息

### 接收邮件

TelegramMail 会定期检查新邮件，并将新邮件发送到 Telegram 对话中。每封邮件包含：
- 邮件主题、发件人和收件人信息
- 邮件正文预览
- 邮件全文截图
- 附件（如果有）

### 发送邮件

1. 发送 `/compose` 命令
2. 选择要使用的邮箱账户
3. 按照提示输入收件人、抄送和密送人、主题和正文
4. 发送附件（可选）
5. 确认发送

> **注意：** 由于Telegram客户端会自动渲染特定的格式标记，以下格式需要使用转义符号：
> - 加粗文本：使用 `\*\*文本\*\*` 而非 `**文本**`
> - 内联代码：使用 `` \`代码\` `` 而非 `` `代码` ``
> - 代码块：使用 `` \`\`\`\n 代码块 \n\`\`\` `` 而非 `` ```\n 代码块 \n``` ``

## 许可证
此项目使用 GPL 许可证 - 详见 [LICENSE](LICENSE) 文件