# Telegramail 项目规范

## 适用范围

- `app/`、`scripts/`、`tests/` 和 `web/src/` 是 Telegramail v2 的主要源码与测试目录。
- `web/dist/` 是构建产物，不直接编辑。
- `.env`、`data/`（含数据库与各类附件）和生产 Compose volumes 属于运行时状态，不得进入发布包或被常规修复覆盖。

## 修复与发布

- 对应用、Worker、Telegram Bot API 集成或 Mini App 的常规 bug 修复，完成相关测试、全量测试和构建检查后，直接按 `.codex/skills/telegramail-development/references/deployment.md` 部署生产，不再等待额外的部署确认。
- 发布前必须执行 `git diff --check`；归档排除清单、上传方式与 SHA-256 校验按 `.codex/skills/telegramail-development/references/deployment.md` 执行。
- 生产部署必须保留远端 `.env`、整个 `data/` 目录及无关 Compose 状态；已有数据库只运行 `scripts/migrate_v2.py --upgrade`，不得运行 `--init` 或 `docker compose down -v`。
- 部署完成后必须确认 `telegramail` 为 `running healthy`、重启次数为 0、首页和 `/health/ready` 返回 200，且近期日志无 error/exception/traceback/fatal 标记。
- 验收应覆盖本次修改的用户流程；涉及删除邮件等破坏性用户数据操作时，用自动化测试或无破坏性检查验证，未经用户明确授权不得在生产数据上制造测试记录。

## 需要停下确认的情况

- 删除或重置生产数据库、`data/` 下的持久化数据、Compose volume 或其他持久化数据。
- 写入、迁移或替换凭据、令牌、私钥或生产 `.env`。
- 修改路由器、DNS、网关、Docker 网络、Cloudflare/Ingress 或其他基础设施。
- 测试失败、构建失败、远端预检失败，或验收结果无法证明本次修复已经生效。

