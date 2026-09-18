---
name: telegramail-development
description: "Develop, test, review, and deploy Telegramail v2 changes across the Python API/workers and React Mini App. Use for repository-scoped Telegramail work; do not use for unrelated generic projects."
---

# Telegramail Development

Use this skill for changes to Telegramail's backend, mail/LLM workers, SQLite v2 store, Telegram Bot API integration, or `web/` Mini App. Keep the requested outcome and acceptance checks explicit. Whether a change may be deployed without a separate confirmation is defined by the repository `AGENTS.md`, not here.

## Repository shape

- `app/`: Python 3.12 FastAPI API, Telegram integration, mail services/workers, SQLite repository, and LLM summary pipeline.
- `web/`: React/TypeScript Mini App. The Dockerfile builds it into `web/dist`; do not edit generated `web/dist` as source.
- `tests/`: Python `unittest` coverage, including v2 API/auth/database/worker/Telegram tests.
- `web/src/*.test.tsx`: Mini App Vitest coverage.
- `scripts/migrate_v2.py`: database initialization/upgrade/check entry point.
- Production operations are documented in [references/deployment.md](references/deployment.md); it contains no credentials and is the repository-local source for Telegramail's deployment shape.

## Working rules

1. Inspect `git status --short` before editing. Preserve unrelated dirty changes, including the repository's ongoing v1-to-v2 migration state; never reset, checkout, or clean broad paths.
2. Keep credentials out of source, logs, test fixtures, prompts, release archives, and chat. `.env` and `data/` are local/production state and must never enter a release package.
3. Prefer the smallest change that closes the acceptance gap. For auth, persistence, mail delivery, LLM summaries, and deletion flows, test the observable boundary rather than only internal helpers.
4. For UI changes, check both narrow mobile and desktop layouts, loading/empty/error states, keyboard or Telegram back/close behavior, and the real API contract. Reuse existing sanitization for email and LLM HTML; never render untrusted HTML directly.
5. LLM settings are user/database configuration, not deployment environment variables. Preserve the encrypted API-key storage convention and the `default_language` field across API, repository, worker prompt, and Mini App form.

## Local verification

Use the project toolchain. On a clean machine, install Pixi and frontend dependencies as documented by the repository:

```bash
pixi install
pixi run test
pixi run i18n
cd web
npm ci
npm test
npm run check
npm run build
```

For focused work, run the narrowest relevant Python test file and the related Vitest test, then run the full suites before handoff. Always run `git diff --check`; report any unavailable environment (for example, local Docker is not expected on the development Mac) instead of silently substituting another runtime.

## Production deployment

Production changes are externally consequential. Deploy only when the repository `AGENTS.md` authorizes it for the current change, or when the user explicitly asks for deployment. Before deploying, read [references/deployment.md](references/deployment.md). If the HomeNetwork repository is available, also read its `AGENTS.md`, `skills/homenetwork-ops/SKILL.md`, recovery reference, and canonical Unraid inventory before connecting remotely.

The deployment boundary — preservation of production state, the permitted migration command, and forbidden destructive operations — is defined by the repository `AGENTS.md`.

Follow [references/deployment.md](references/deployment.md) for the archive exclusion list, upload, replacement sequence, and acceptance commands.

After a deploy, verify the user-facing Mini App bundle and the requested workflow, not just container health. If Telegram authentication fails after a restart, distinguish an expired/invalid `initData` (401 before setup-code validation) from a missing database binding; a bound user should not need the one-time setup code.
