# 从 Telegramail v1 迁移到 v2

[English](./migration-v1-to-v2.md)

Telegramail v2 使用 Telegram Bot API 和 Mini App，取代了 v1 的个人 Telegram 客户端与 TDLib。迁移不会修改 v1 数据库，而是创建一份独立的 v2 数据库。

以下内容不会迁移：

- 原有 Telegram 话题和投递游标
- 已经丢失的附件文件；其元数据仍会保留

## 开始前

1. 停止并移除 v1 容器，但不要删除其数据卷。
2. 完整备份 v1 数据目录。
3. 将 v1 数据库放在 `./data/telegramail.db`。
4. 确认 `./data/telegramail-v2.db` 不存在。
5. 将复制后的数据目录交给容器用户 `10001:10001`：`sudo chown -R 10001:10001 data`。
6. 下载当前的 `docker-compose.yml` 和 `.env.example`，再按 v2 要求填写 `.env`。

## 执行迁移

在 `docker-compose.yml` 所在目录依次运行：

```bash
docker compose pull

# 确认能够读取源数据；此步骤不会写入 v2 数据库。
docker compose run --rm --no-deps telegramail python scripts/migrate_v2.py --dry-run

# 根据 v1 数据创建 v2 数据库。
docker compose run --rm --no-deps telegramail python scripts/migrate_v2.py

# 检查迁移后的数据库。
docker compose run --rm --no-deps telegramail python scripts/migrate_v2.py --check-only

docker compose up -d
```

启动后打开 `https://你的域名/health/ready`。在确认 v2 服务健康、邮箱账户与近期邮件均可正常查看前，不要删除 v1 备份。
