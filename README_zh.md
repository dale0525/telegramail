# TelegramMail

TelegramMail 是一个基于 Telegram 和 [aiotdlib](https://github.com/pylakey/aiotdlib) 的邮件收发工具，让你可以直接在 Telegram 中收发邮件，无需切换到传统邮件客户端。本项目设计为个人使用，一个客户端只能由一个用户管理和使用。

## 和传统邮件客户端的比较
### 优点
- 轻量
- 全平台支持（通过 telegram），一次配置全平台通用
- 邮件信息和附件存储在 telegram，享受 telegram 的永久云存储
- 即使邮箱账号丢失（比如离职后无法访问公司邮箱），依然可以查看历史邮件
- 可以使用大语言模型 API 实现 AI 相关功能

### 缺点
- 无法处理含有大量收件人、抄送、密送的情况
- 撰写邮件不方便，没有所见即所得的编辑器

## 功能

- [x] 添加多个邮箱
- [x] 接收邮件并转发到 Telegram
- [x] 删除邮件
- [ ] 撰写新邮件
- [x] 定时获取邮件
- [x] 手动刷新邮件
- [ ] 回复邮件
- [ ] 转发邮件
- [ ] 接收 INBOX 之外的邮件
- [ ] 获取所有邮件
- [ ] 为每个邮箱设置签名
- [ ] 邮件信息中显示邮箱所在文件夹
- [x] 使用 LLM 总结邮件
- [ ] 使用 LLM 判断邮件标签
- [ ] 使用 LLM 写邮件
- [ ] 搜索邮件
- [x] 从邮件中提取重要链接(通过 LLM)

## 安装与部署

### 提前准备

- Telegram Bot Token（通过 @BotFather 获取）
- Telegram App ID、Telegram App Hash（需要[申请](https://core.telegram.org/api/obtaining_api_id)）
> 为什么需要 Telegram App ID，而不是单纯使用 Telegram Bot?
> 1. Telegram Bot 无法删除超过 48 小时的消息
> 2. Telegram Bot 无法监听删除信息事件
- (可选) 兼容 OpenAI 的 LLM API，用于 AI 功能，如总结邮件内容

### 本地开发
*暂不支持 Windows，如果需要 Windows 支持，请自行编译 TDLib 库文件（或使用 WSL/Docker）*
1. [安装 mise](https://mise.jdx.dev/getting-started.html)
> 也可以不安装，直接使用 python3.10 和 pip

2. 克隆仓库:
   ```bash
   git clone https://github.com/dale0525/telegramail.git
   cd telegramail
   ```

3. 创建配置文件:
   ```bash
   cp .env.example .env
   ```

4. 编辑 `.env` 文件，填入你的 Telegram Bot Token和Telegram ID:
   ```
   TELEGRAM_API_ID=your_telegram_api_id_here
   TELEGRAM_API_HASH=your_telegram_api_hash_here
   TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here

   # 可选的 LLM 设置
   ENABLE_LLM_SUMMARY=0    # 改成 1 来开启 LLM 功能
   LLM_SUMMARY_THRESHOLD=200  # 如果邮件内容超过这个阈值，会使用 LLM 来总结邮件内容
   OPENAI_BASE_URL=your_openai_base_url_here
   OPENAI_API_KEY=your_openai_key_here
   OPENAI_EMAIL_SUMMARIZE_MODELS=第一个模型,第二个模型,...   # 如果第一个模型请求失败，会尝试使用第二个模型
   ```

5. 初始化开发环境：
   ```bash
   # 安装依赖、设置 TDLib 库文件
   mise run init
   # 或者
   pip install -r requirements.txt && python scripts/setup_tdlib.py
   ```

6. 启动应用：
   ```bash
   # 启动应用
   mise run dev
   # 或者
   python -m app.main
   ```

7. 检查 i18n 是否完善
   ```bash
   mise run i18n
   # 或者
   python scripts/check_i18n.py
   ```

#### TDLib 库文件管理

项目包含了跨平台的 TDLib 库文件自动化管理功能：

- **自动设置**：setup_tdlib.py脚本会自动检测你的平台并配置相应的 TDLib 库文件
- **独立库文件**：为 bot 和 user 客户端创建独立的库文件（aiotdlib 限制要求）
- **跨环境一致性**：在开发和生产/容器环境中都能一致工作

**平台支持**：
- ✅ **macOS**：从包含的 ARM64 库文件自动设置
- ✅ **Linux**：支持 AMD64 和 ARM64 架构的自动设置
- ⚠️ **Windows**：需要手动编译 TDLib 库（或使用 WSL/Docker）

### 生产环境部署（使用 Docker Compose）

使用 Docker Compose 和 Docker Hub 上的预构建镜像进行生产环境部署：

1. 创建部署目录:
   ```bash
   mkdir telegramail && cd telegramail
   ```

2. 下载 Docker Compose 配置和环境配置文件:
   ```bash
   curl -O https://raw.githubusercontent.com/dale0525/telegramail/main/docker-compose.yml
   curl -o .env https://raw.githubusercontent.com/dale0525/telegramail/main/.env.example
   ```

3. 修改`.env`文件，见[本地开发](#本地开发)中的说明

4. 创建数据目录并设置权限:
   ```bash
   mkdir data && chmod -R 755 data
   ```

5. 部署:
   ```bash
   docker-compose up -d
   ```

## 使用方法

### Telegram Bot 命令

- `/start` - 首次启动命令，会引导用户登录 telegram 账号、添加第一个邮箱账户
- `/help` - 显示帮助信息
- `/accounts` - 管理已添加的邮箱账户、添加新邮箱账户
- `/check` - 手动检查新邮件

### 添加邮箱账户

1. 使用`/accounts`命令，选择添加新账户
2. 按照提示选择邮箱服务供应商，然后输入邮箱地址、密码/App专用密码、Alias(纯显示用)。如果选择自定义邮箱服务，还需要额外输入 imap 和 smtp 的服务器、端口、是否使用 SSL。当前[预定义配置](./app/email_utils/common_providers.py)的邮箱服务供应商包括：
   - Gmail
   - Outlook/Hotmail
   - Yahoo
   - iCloud
   - Zoho Mail
   - AOL
   - GMX
   - QQ 邮箱
   - 网易邮箱
   - 腾讯企业邮箱
   - 阿里邮箱
   - Yandex
   - Linux DO
3. Telegramail 会在 Telegram 中创建一个以 Alias 命名的群组，并加入一个名为 Email 的文件夹
   ![20250428150635](https://imagehost.daletan.win/20250428150635.png)

### 接收邮件

TelegramMail 会定期检查 INBOX 中的未读邮件，并将新邮件发送到 Telegram 以邮箱账户 Alias 命名的群组中。每封邮件以一个 Forum Topic 呈现，包含：
- 邮件主题、发件人和收件人信息
- 邮件总结(如果进行了 LLM 相关配置)
- 邮件正文
  - 如果是 HTML 格式的，为一个 html 文件
  - 如果是纯文本格式的，则直接发送纯文本
- 附件
![20250428155652](https://imagehost.daletan.win/20250428155652.png)

你可以通过修改`.env`中的`POLLING_INTERVAL`来更改定期检查的频率。默认为 300 秒。

### 手动获取邮件
#### 手动获取所有邮件
使用`/check`命令，程序会检查所有已添加邮箱账户的未读邮件

#### 手动获取特定邮箱账户的邮件
使用`/accounts`命令，在出现的邮箱列表中，点击需要手动获取邮件的邮箱账户，然后点击"手动获取邮件"按钮即可

### 删除邮件

如果要删除邮件，直接删除 Telegram 中邮件对应的 Topic 即可。程序会每隔 3 分钟检查被删除的 Topic，并清理数据库并删除服务器上的对应邮件。

### AI

如果在`.env`中配置了 LLM 相关的参数，可以使用 AI 相关功能。目前功能有：
- 使用 AI 总结邮件内容。 Prompt 位于[app/email_utils/llm.py](./app/email_utils/llm.py)中。

### 本地化

翻译文本位于[app/i18n](./app/i18n/)文件夹中。你可以自己添加目标语种的翻译，并且在`.env`中将`DEFAULT_LANGUAGE`设置为自己的语言

## 已知问题
- 由于 LLM 输出的不稳定性，邮件总结可能包含错误的 JSON 格式，或者不能被 telegram 解析的 html tag，会导致发送总结失败。如果要使用该功能，请尽量配置靠谱的模型。

## 许可证
此项目使用 GPL 许可证 - 详见 [LICENSE](LICENSE) 文件