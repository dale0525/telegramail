# Migrate from Telegramail v1 to v2

[简体中文](./migration-v1-to-v2.zh.md)

Telegramail v2 replaces the personal Telegram client and TDLib integration with the Telegram Bot API and Mini App. The migration keeps the v1 database unchanged and creates a separate v2 database.

Not migrated:

- Existing Telegram topics and delivery cursors
- Attachment files that are already missing; their metadata is retained

## Before you start

1. Stop and remove the v1 container without deleting its volumes.
2. Back up the complete v1 data directory.
3. Put the v1 database at `./data/telegramail.db`.
4. Confirm that `./data/telegramail-v2.db` does not exist.
5. Give container user `10001:10001` ownership of the copied data directory: `sudo chown -R 10001:10001 data`.
6. Download the current `docker-compose.yml` and `.env.example`, then configure `.env` for v2.

## Migrate

Run each command from the directory containing `docker-compose.yml`:

```bash
docker compose pull

# Verify that the source data can be read; this does not write the v2 database.
docker compose run --rm --no-deps telegramail python scripts/migrate_v2.py --dry-run

# Create the v2 database from the v1 data.
docker compose run --rm --no-deps telegramail python scripts/migrate_v2.py

# Check the migrated database.
docker compose run --rm --no-deps telegramail python scripts/migrate_v2.py --check-only

docker compose up -d
```

Open `https://your-host/health/ready` after startup. Do not remove the v1 backup until the v2 service is healthy and your accounts and recent mail are visible.
