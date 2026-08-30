# Telegramail

[English](./README.md)

Telegramail 把邮箱放进 Telegram。一个自托管服务可以同时连接多个 IMAP/SMTP 邮箱，通过 Bot 推送新邮件，并用 Mini App 完成阅读、写信、回复和转发。

![Telegram 中的 Telegramail 邮件与自动摘要](./docs/images/telegramail-inbox.png)

## Telegramail 适合你吗？

Telegramail 面向个人用户：你经常使用 Telegram，需要管理多个邮箱，并且愿意在自己的服务器上运行 Docker。

它不是团队共享邮箱，也不是多人邮件服务。Telegram 中同步出的副本可以托底，但不能替代对本地 `data/` 目录的备份。

## 为什么选择 Telegramail？

- **在 Telegram 里轻量管理多个邮箱。** 新邮件直接进入 Telegram；长邮件、附件、写信、回复、转发和账户设置则交给 Mini App。
- **自托管，数据由自己掌控。** 邮箱凭据使用你的主密钥加密，凭据和邮件索引数据保存在服务器本地的 SQLite 数据库中。
- **在 Telegram 中多一份副本。** Telegramail 会把已同步的邮件和支持的附件发送到 Telegram。即使源邮箱以后失效，已经送达 Telegram 的副本仍可在那里回看；超出 Telegram 投递限制的内容则只保留在本地。

Telegramail 只使用 Telegram Bot API 和 Mini App，不需要你的个人 Telegram 手机号、API ID、API Hash 或 TDLib 会话。

## 支持的功能

- 同时连接 Gmail、QQ 邮箱、Outlook/Microsoft 365、iCloud 和自定义 IMAP/SMTP 邮箱
- 按可配置间隔检查新邮件，并用每个邮箱独立的 UID 游标避免重复收取
- 写信、回复、转发、抄送/密送、Markdown、签名和附件
- 通过 OpenAI-compatible 服务生成可选的摘要、分类和重要链接
- 加密保存邮箱凭据，邮件数据存入本地 SQLite

## 使用构建好的镜像快速启动

你需要 Docker Compose、通过 [@BotFather](https://t.me/BotFather) 创建的 Bot，以及一个可公开访问的 HTTPS 域名。先创建 Cloudflare named Tunnel，将域名路由到 `http://telegramail:8080`，并保存连接令牌。

```bash
mkdir telegramail && cd telegramail
curl -fsSLO https://raw.githubusercontent.com/dale0525/telegramail/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/dale0525/telegramail/main/.env.example -o .env
mkdir -p data
sudo chown 10001:10001 data
```

编辑 `.env`，填写 Bot Token、公开网址、Cloudflare Tunnel Token、首次设置码、会话密钥和主密钥。文件内的注释说明了各项密钥的生成方式。

```bash
docker compose pull
docker compose run --rm --no-deps telegramail python scripts/migrate_v2.py --init
docker compose up -d
```

打开 `https://你的域名/health/ready`，服务就绪时会返回 HTTP 200。然后向 Bot 发送 `/start`，打开 Mini App，首次输入 `SETUP_CODE` 绑定管理员，再从“设置”中添加邮箱。

GitHub Actions 会将镜像发布到 `ghcr.io/dale0525/telegramail`。`latest` 跟随 `main`；`v2.1.0` 这样的 Git 标签会生成镜像标签 `2.1.0`。如需固定该版本，可在 `.env` 中设置 `TELEGRAMAIL_IMAGE_TAG=2.1.0`。

以上命令默认 GHCR 镜像已经公开。GitHub 首次发布时会将镜像包设为私有，因此项目维护者需要在第一次发布后将其可见性改为 Public，匿名拉取才会生效。

更新已有的 v2 服务：

```bash
sudo chown -R 10001:10001 data
docker compose pull
docker compose run --rm --no-deps telegramail python scripts/migrate_v2.py --upgrade
docker compose up -d
```

从使用 TDLib 的 v1 版本升级？请阅读 [v1 到 v2 迁移指南](./docs/migration-v1-to-v2.zh.md)。

## 许可证

[GPL-3.0](./LICENSE)
