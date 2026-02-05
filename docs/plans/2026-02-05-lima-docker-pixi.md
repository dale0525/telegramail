# Lima Docker (Pixi-Managed) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** On macOS, run Docker via Lima with all VM state stored under the repo's `.pixi/` directory, while exposing a pixi-managed `docker`/`compose` task interface for the team.

**Architecture:** Add a small Python wrapper (`scripts/container_runtime.py`) that (1) sets `LIMA_HOME` to `.pixi/lima`, (2) starts a `template:docker` Lima instance, (3) resolves `DOCKER_HOST` to the forwarded socket, and (4) executes `docker` / `docker-compose` with `DOCKER_CONFIG` under `.pixi/docker`.

**Tech Stack:** pixi, Python, Lima, Docker Engine in VM (Lima `template:docker`)

---

### Task 1: Pixi dependencies and tasks

**Files:**
- Modify: `pixi.toml`

**Steps:**
1. Add `docker-cli` and `docker-compose` to `[dependencies]`.
2. Add tasks:
   - `container-init`, `container-stop`, `container-delete`, `container-status`
   - `docker` (passes args through)
   - `compose`, `compose-up`, `compose-down`

---

### Task 2: Project-local runtime wrapper

**Files:**
- Create: `scripts/container_runtime.py`

**Steps:**
1. Implement macOS behavior:
   - `LIMA_HOME=$PWD/.pixi/lima`
   - `limactl start --name telegramail-docker template:docker` (with small CPU/memory/disk defaults)
   - `DOCKER_HOST=unix://<instance-dir>/sock/docker.sock`
2. Implement pass-through:
   - `docker ...`
   - `docker-compose ...` (or `docker compose ...` if plugin is available)
3. Ensure `DOCKER_CONFIG=$PWD/.pixi/docker` to avoid touching `~/.docker`.

---

### Task 3: Create and validate `.pixi`-scoped Docker engine (macOS)

**Steps:**
1. Run: `pixi install --locked`
2. Run: `pixi run container-init`
3. Run: `pixi run docker -- version`
4. Run: `pixi run docker -- info`

Expected: Docker CLI connects successfully using the Lima-provided socket under `.pixi/lima/.../sock/docker.sock`.

---

### Task 4: Clean up system-level Lima disk usage (one-time)

**Steps:**
1. Stop and delete instances in the default home (`~/.lima`):
   - `limactl stop <name>`
   - `limactl delete --force <name>`
2. Remove directories:
   - `~/.lima`
   - `~/Library/Caches/lima`

Expected: Only `.pixi/lima` holds VM disk/images for this repository.

