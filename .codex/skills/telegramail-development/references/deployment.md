# Telegramail production deployment checklist

This is the Telegramail-specific production runbook, and the single source of truth for the release archive's exclusion list, upload procedure, replacement sequence, and acceptance commands. It is intentionally tracked without credentials. The current target is reached through the local `ssh unraid` alias (Unraid root at the HomeNetwork canonical inventory); never put its password, tokens, or private keys in commands, files, logs, or chat.

Current production shape:

- Remote app directory: `/mnt/user/appdata/telegramail`
- Application container: `telegramail`
- Tunnel container: `telegramail-cloudflared`
- Database: `/mnt/user/appdata/telegramail/data/telegramail-v2.db`
- Runtime data directory: `/mnt/user/appdata/telegramail/data/` — preserve it whole. The application creates its subdirectories on demand, so a healthy install may have any subset of them and a fresh install has none.
- Public Mini App: `https://telegramail.logiconsole.com`

Before any remote operation, read the HomeNetwork repository's current operating instructions and canonical inventory if that repository is present. If the `unraid` SSH alias or canonical host identity is unavailable, stop rather than guessing credentials or host details.

## Preflight

From the repository root, confirm the requested change is the only intended scope, run the local checks, and create an archive from the current worktree while excluding:

```text
.git  .env  .local  .codex  data  .pixi  .vscode
web/node_modules  web/dist  tmp  temp  .pytest_cache
__pycache__  *.pyc  *.pyo  .DS_Store
```

Record the archive SHA-256. Upload it with authenticated `scp` to `unraid:/tmp/telegramail-release.tar.gz` and verify the remote SHA-256 matches. Do not output or download the production `.env`.

## Existing Unraid install

All remote commands below run from the app directory. On Unraid, verify before replacement:

```bash
cd /mnt/user/appdata/telegramail
test -f .env
test -s data/telegramail-v2.db
test -f docker-compose.yml
docker inspect telegramail \
  --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} restart={{.RestartCount}}'
```

Only after these checks pass, replace the source directories/files from the archive while preserving `.env`, `data/`, Compose volumes, and unrelated files:

```bash
rm -rf -- app scripts tests web
tar -xzf /tmp/telegramail-release.tar.gz
```

This deletion is limited to source directories and is authorized only as part of the requested deployment. Never use `docker compose down -v`, remove `data/`, or run `migrate_v2.py --init` against this database. The existing production container normally continues serving while source directories are replaced.

Then build and migrate from the replaced sources:

```bash
docker compose config >/dev/null
docker compose build telegramail
docker compose run --rm --no-deps telegramail \
  python scripts/migrate_v2.py --upgrade
docker compose up -d
```

Wait up to 60 seconds for exactly `running healthy`; stop and report the failing phase if it becomes unhealthy. Do not initialize or delete the existing database.

## Acceptance

```bash
docker inspect telegramail \
  --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} restart={{.RestartCount}}'
curl -fsS -o /dev/null -w 'root=%{http_code}\n' https://telegramail.logiconsole.com/
curl -fsS -o /dev/null -w 'ready=%{http_code}\n' https://telegramail.logiconsole.com/health/ready
docker logs --since 5m telegramail 2>&1 \
  | grep -Eic '(^|[^[:alpha:]])(error|exception|traceback|fatal)([^[:alpha:]]|$)'
```

The release is not complete until the requested Mini App/Bot workflow is checked: authentication, inbox, email detail, LLM settings, and the specific changed behavior on both narrow and desktop layouts when applicable. Remove only the temporary transfer archive and generated caches after successful verification; never remove production data or volumes. If the deploy fails, preserve the data directory and report the exact failed phase.
