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
- 如果收件人很多（尤其大量 BCC），需要分批发送多封邮件
- 撰写邮件没有所见即所得编辑器（当前以 Markdown 撰写）

## 功能

- [x] 添加多个邮箱
- [x] 接收邮件并转发到 Telegram
- [x] 删除邮件
- [x] 撰写新邮件
- [x] 定时获取邮件
- [x] 手动刷新邮件
- [x] 回复邮件
- [x] 转发邮件
- [x] 接收 INBOX 之外的邮件
- [ ] 获取所有邮件
- [x] 为每个邮箱设置签名
- [x] 邮件信息中显示邮箱所在文件夹
- [x] 使用 LLM 总结邮件
- [x] 使用 LLM 判断邮件标签
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
- (可选) 兼容 OpenAI 的 LLM API，用于 AI 功能，如总结邮件和判断邮件标签

### 本地开发
*暂不支持 Windows，如果需要 Windows 支持，请自行编译 TDLib 库文件（或使用 WSL/Docker）*
1. [安装 pixi](https://pixi.sh/)

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

   # 可选的 TDLib 设置
   TELEGRAM_CHAT_EVENT_LOG_TIMEOUT=30  # 如果遇到超时（TimeoutError），可以适当调大

   # 可选的 LLM 设置
   ENABLE_LLM_SUMMARY=0    # 改成 1 来开启 LLM 分析（总结 + 标签 + 链接）
   LLM_SUMMARY_THRESHOLD=200  # 如果邮件内容超过这个阈值，会使用 LLM 进行邮件分析
   OPENAI_BASE_URL=your_openai_base_url_here
   OPENAI_API_KEY=your_openai_key_here
   OPENAI_EMAIL_SUMMARIZE_MODELS=第一个模型,第二个模型,...   # 如果第一个模型请求失败，会尝试使用第二个模型
   ```

5. 初始化开发环境：
   ```bash
   # 安装依赖、设置 TDLib 库文件
   pixi install --locked
   pixi run init
   ```

6. 启动应用：
   ```bash
   # 启动应用
   pixi run dev
   ```

7. 检查 i18n 是否完善
   ```bash
   pixi run i18n
   ```

#### （可选）本地容器运行时（macOS：Lima + Docker，全部通过 pixi 驱动）

如果你希望在 macOS 上使用更轻量、占用更可控的 Docker 方案（避免 Docker Desktop），可以使用 Lima 在虚拟机里运行 Docker Engine，并把所有 VM 数据放到项目的 `.pixi/` 目录中。

1. 安装 Lima（系统唯一前置）：
   ```bash
   brew install lima
   ```

2. 初始化项目专用的 Docker 引擎（数据目录：`.pixi/lima`）：
   ```bash
   pixi run container-init
   ```

3. 通过 pixi 使用 Docker / Compose（不会污染 `~/.lima`、`~/.docker`）：
   ```bash
   pixi run docker -- version
   pixi run docker -- ps

   pixi run compose -- version
   pixi run compose -- up -d
   pixi run compose -- down
   ```

可选环境变量（调整资源/实例名）：
- `TELEGRAMAIL_LIMA_INSTANCE`（默认 `telegramail-docker`）
- `TELEGRAMAIL_LIMA_CPUS`（默认 `2`）
- `TELEGRAMAIL_LIMA_MEMORY`（GiB，默认 `2`）
- `TELEGRAMAIL_LIMA_DISK`（GiB，默认 `20`）

#### TDLib 库文件管理

项目包含了跨平台的 TDLib 库文件自动化管理功能：

- **自动设置**：setup_tdlib.py脚本会自动检测你的平台并配置相应的 TDLib 库文件
- **独立库文件**：为 bot 和 user 客户端创建独立的库文件（aiotdlib 限制要求）
- **跨环境一致性**：在开发和生产/容器环境中都能一致工作

> Linux 下 TDLib 动态库还依赖 C++ 运行时、OpenSSL、zlib（通过 pixi/conda 安装），以及 LLVM libunwind（`libunwind.so.1`）。其中 `libunwind.so.1` 需要系统包提供（Debian/Ubuntu: `libunwind-14`，或任何能提供 `libunwind.so.1` 的版本）。

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

> 如果你希望从源码自行构建镜像：`docker build -t telegramail .`。项目的 `Dockerfile` 会使用 `pixi.toml` + `pixi.lock` 来安装依赖，从而保证开发/生产依赖一致。

## 使用方法

### Telegram Bot 命令

- `/start` - 首次启动命令，会引导用户登录 telegram 账号、添加第一个邮箱账户
- `/help` - 显示帮助信息
- `/accounts` - 管理已添加的邮箱账户、添加新邮箱账户
- `/check` - 手动检查新邮件
- `/compose` - 撰写新邮件（创建一个 Draft 话题）

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

TelegramMail 会将新邮件发送到 Telegram 以邮箱账户 Alias 命名的群组中。每封邮件以一个 Forum Topic 呈现，包含：
- 邮件主题、发件人和收件人信息
- 邮件总结(如果进行了 LLM 相关配置)
- 邮件正文
  - 如果是 HTML 格式的，为一个 html 文件
  - 如果是纯文本格式的，则直接发送纯文本
- 附件
![20250428155652](https://imagehost.daletan.win/20250428155652.png)

默认模式为 `MAIL_RECEIVE_MODE=hybrid`（推荐）：优先使用 IMAP IDLE 做近实时监听，同时保留 `POLLING_INTERVAL` 作为兜底轮询。

- `hybrid`：近实时（通常数秒到几十秒）+ 兜底轮询
- `idle`：仅使用 IMAP IDLE（低延迟，但无轮询兜底）
- `polling`：仅定时轮询（延迟约为 `0 ~ POLLING_INTERVAL`）

常用参数：
- `POLLING_INTERVAL`：轮询间隔（秒），默认 `300`
- `IMAP_IDLE_TIMEOUT_SECONDS`：单次 IDLE 等待超时（秒），默认 `1740`
- `IMAP_IDLE_FALLBACK_POLL_SECONDS`：服务器不支持 IDLE 时的短轮询间隔（秒），默认 `30`
- `IMAP_IDLE_RECONNECT_BACKOFF_SECONDS`：IDLE 连接失败后的重连初始退避（秒，指数退避），默认 `5`

如果要监听额外的 IMAP 文件夹，可以设置 `TELEGRAMAIL_IMAP_MONITORED_MAILBOXES`（逗号分隔），例如：`INBOX,Archive,Spam`。
你也可以通过 `/accounts` → 选择账户 → **IMAP 文件夹** 来为单个账户配置覆盖（支持“探测文件夹”+ 点击选择）。
你也可以通过 `/accounts` → 选择账户 → **签名** 来为单个账户设置多个发信签名（签名内容使用 Markdown，发送时自动渲染为 HTML）。

### 手动获取邮件
#### 手动获取所有邮件
使用`/check`命令，程序会检查所有已添加邮箱账户的未读邮件

#### 手动获取特定邮箱账户的邮件
使用`/accounts`命令，在出现的邮箱列表中，点击需要手动获取邮件的邮箱账户，然后点击"手动获取邮件"按钮即可

### 删除邮件

如果要删除邮件，直接删除 Telegram 中邮件对应的 Topic 即可。程序会每隔 3 分钟检查被删除的 Topic，并清理数据库并删除服务器上的对应邮件。

### 撰写 / 回复 / 转发邮件（Draft）

TelegramMail 使用 Draft 话题来完成撰写、回复和转发：

1. **撰写新邮件**
   - 在该邮箱账号群组的任意 Topic（例如 General）里发送 `/compose`。在群组里 Telegram 可能会自动补全成 `/compose@你的机器人` —— 两种写法都支持。它会创建一个新的 Draft Topic，并固定一条 Draft 卡片消息（带 Send/Cancel 按钮）。
   - 然后会进入交互式引导（和新增邮箱账号类似），依次填写：`To`（必填）、`Cc`（可 `/skip`）、`Bcc`（可 `/skip`）、`Subject`（可 `/skip`）、`Body`（可 `/skip`）。引导完成后会自动回填 Draft 卡片。
2. **回复 / 转发**
   - 在邮件 Topic 内，会有一条“Actions”消息，包含 Reply / Forward 按钮；点击后会在同一 thread 内创建 Draft。
3. **在 Draft Topic 中编辑邮件**
   - `/from`：弹出发件人身份列表（用于 alias 场景），点击即可切换
   - `/from b@example.com`：直接切换到指定发件人身份
   - `/to ...`、`/cc ...`、`/bcc ...`：设置收件人。可直接输入邮箱（多个地址用英文逗号`,`分隔，例如：`/to a@example.com, b@example.com`；`/cc`、`/bcc` 同理）；也可输入关键词（或不带参数）从该邮箱账号往来邮件联系人中筛选，并以“多选 + 保存”方式批量选择
   - `/subject ...`：设置主题
   - `/signature`：弹出签名选择（可选某个签名、默认签名或不使用签名）
   - `/signature none`：本次发送不使用签名；`/signature default`：使用默认签名
   - 正文：直接发送普通文本消息，会追加到邮件正文（支持 Markdown）
   - 附件：直接在 Draft Topic 里发送文件/图片/音频等，会作为邮件附件；用 `/attachments` 管理/移除附件
4. **发送**
   - 点击 Draft 卡片上的 Send 按钮发送邮件；Cancel 会取消草稿
   - 若本草稿未显式指定签名，默认使用账户默认签名；也可在草稿中切换为其他签名或不使用签名
   - 签名选择会持久化：系统会记住该邮箱“上次成功发送时使用的签名策略”（具体签名 / 默认签名 / 不使用签名），下次新建草稿会自动沿用

#### From 身份（alias）说明

部分邮件服务商存在 alias 投递：你实际收到了 `a@example.com` 的邮件，但投递头（Delivered-To 等）显示是发给 `b@example.com`（`b` 是 `a` 的 alias）。

TelegramMail 会基于 Delivered-To 等投递头自动匹配并默认选择更合适的 From 身份（如 `b@example.com`），并允许你在 Draft 中用 `/from` 手动切换。

#### 正文格式（Markdown → HTML）

Draft 正文以 Markdown 撰写。发送时会同时生成：
- `text/plain`：原 Markdown 文本
- `text/html`：Markdown 渲染后的 HTML

### AI

如果在`.env`中配置了 LLM 相关的参数，可以使用 AI 相关功能。目前功能有：
- 使用 AI 总结邮件内容（摘要、优先级、待办、截止时间、关键联系人）。
- 使用 AI 判断邮件标签（`category`），当前分类：`task`、`meeting`、`financial`、`travel`、`newsletter`、`system`、`social`、`other`。
- 使用 AI 从邮件中提取重要链接（例如退订链接）。
- 可使用 `/label` 在 Telegram 内按标签筛选最近邮件，并进行定位/回复/转发。
- 以上 Prompt 位于 [app/email_utils/llm.py](./app/email_utils/llm.py)。

### 本地化

翻译文本位于[app/i18n](./app/i18n/)文件夹中。你可以自己添加目标语种的翻译，并且在`.env`中将`DEFAULT_LANGUAGE`设置为自己的语言

## 已知问题
- 由于 LLM 输出的不稳定性，邮件 AI 分析（总结/标签/链接）可能包含错误的 JSON 格式，或者不能被 Telegram 解析的 HTML tag，导致发送失败。如果要使用该功能，请尽量配置靠谱的模型。

## 许可证
此项目使用 GPL 许可证 - 详见 [LICENSE](LICENSE) 文件
