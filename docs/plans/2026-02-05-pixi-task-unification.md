# Pixi Task Unification Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make `pixi` the single entrypoint for common dev/verification commands (local + container), and update docs/CI accordingly.

**Architecture:** Keep `pixi.toml` as the source of truth for task commands. Update documentation and script hints to reference `pixi run <task>` instead of direct `python`/`pip` invocations. Ensure Docker build uses `pixi.toml` + `pixi.lock`, and CI rebuilds when those change.

**Tech Stack:** Pixi, Python 3.10, Debian-based Docker image, GitHub Actions.

### Task 1: Standardize Pixi tasks

**Files:**
- Modify: `pixi.toml`

**Step 1: Add convenience aliases (optional)**
- Add `tdlib-validate` / `tdlib-info` tasks (thin wrappers around `scripts/setup_tdlib.py` flags).

**Step 2: Run Pixi tasks locally**
- Run: `pixi run setup-tdlib --validate`
- Expected: `✓ TDLib setup is valid`
- Run: `pixi run test`
- Expected: `OK`

### Task 2: Update docs and script messages to use Pixi

**Files:**
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `scripts/setup_tdlib.py`

**Step 1: Replace direct commands with Pixi equivalents**
- Replace `pip install ...` / `python -m ...` / `python scripts/...` examples with:
  - `pixi install --locked`
  - `pixi run init`
  - `pixi run dev`
  - `pixi run i18n`

**Step 2: Update troubleshooting hints**
- In `scripts/setup_tdlib.py`, prefer printing `pixi run setup-tdlib` / `pixi run dev` / `pixi run setup-tdlib --validate`.

### Task 3: CI should rebuild on Pixi changes

**Files:**
- Modify: `.github/workflows/docker-publish.yml`

**Step 1: Remove `pixi.toml` / `pixi.lock` from `paths-ignore`**
- Verify that dependency changes trigger the Docker build.

### Task 4: Container verification (Lima/nerdctl)

**Files:**
- Verify: `Dockerfile`

**Step 1: Build image**
- Run: `limactl shell <instance> -- nerdctl build -t telegramail-pixi-test .`
- Expected: build completes successfully.

**Step 2: Validate TDLib inside container**
- Run: `limactl shell <instance> -- nerdctl run --rm telegramail-pixi-test pixi run setup-tdlib --validate`
- Expected: `✓ TDLib setup is valid`

