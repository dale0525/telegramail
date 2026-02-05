#!/usr/bin/env python3
"""
Project-local container runtime helper.

Goals:
- Use Lima (macOS) with Docker Engine inside the VM (template:docker)
- Store all Lima state under this repo's `.pixi/` directory to minimize system pollution
- Provide a stable, pixi-managed CLI surface for `docker`/`docker-compose`
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _pixi_dir(root: Path) -> Path:
    return root / ".pixi"


def _lima_home(root: Path) -> Path:
    return _pixi_dir(root) / "lima"


def _docker_config(root: Path) -> Path:
    return _pixi_dir(root) / "docker"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _ensure_dirs(*dirs: Path) -> None:
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def _run(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=True,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=capture_output,
    )


def _strip_task_runner_sentinel(args: list[str]) -> list[str]:
    # `pixi run <task> -- <args>` inserts a literal `--` into the executed command.
    # For pass-through wrappers, strip it to avoid confusing downstream CLIs.
    if args and args[0] == "--":
        return args[1:]
    return args


def _limactl_env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["LIMA_HOME"] = str(_lima_home(root))
    return env


def _require_limactl() -> None:
    if shutil.which("limactl") is None:
        raise RuntimeError(
            "limactl not found. Please install Lima first (macOS: `brew install lima`)."
        )


def _require_docker_cli() -> None:
    if shutil.which("docker") is None:
        raise RuntimeError(
            "`docker` not found in PATH. Ensure you ran `pixi install` successfully."
        )


def _require_docker_compose() -> list[str]:
    if shutil.which("docker-compose") is not None:
        return ["docker-compose"]
    if shutil.which("docker") is not None:
        # docker compose plugin (if present)
        return ["docker", "compose"]
    raise RuntimeError(
        "`docker-compose` (or `docker compose`) not found in PATH. Ensure you ran `pixi install`."
    )


def _instance_name() -> str:
    return os.environ.get("TELEGRAMAIL_LIMA_INSTANCE", "telegramail-docker")


def _instance_resources() -> tuple[str, str, str]:
    cpus = os.environ.get("TELEGRAMAIL_LIMA_CPUS", "2")
    memory = os.environ.get("TELEGRAMAIL_LIMA_MEMORY", "2")  # GiB
    disk = os.environ.get("TELEGRAMAIL_LIMA_DISK", "20")  # GiB
    return cpus, memory, disk


def _lima_instance_exists(root: Path, name: str) -> bool:
    env = _limactl_env(root)
    try:
        cp = _run(["limactl", "list", "--quiet"], env=env, capture_output=True)
    except subprocess.CalledProcessError:
        return False
    names = {line.strip() for line in (cp.stdout or "").splitlines() if line.strip()}
    return name in names


def _lima_start_docker_vm(root: Path, name: str) -> None:
    _require_limactl()

    env = _limactl_env(root)
    _ensure_dirs(_lima_home(root))

    if _lima_instance_exists(root, name):
        _run(["limactl", "start", name], env=env)
        return

    cpus, memory, disk = _instance_resources()
    _run(
        [
            "limactl",
            "start",
            "--name",
            name,
            "--cpus",
            str(cpus),
            "--memory",
            str(memory),
            "--disk",
            str(disk),
            "template:docker",
        ],
        env=env,
    )


def _lima_stop_vm(root: Path, name: str) -> None:
    _require_limactl()
    env = _limactl_env(root)
    if not _lima_instance_exists(root, name):
        print(f"ℹ️  Lima instance not found: {name}")
        return
    _run(["limactl", "stop", name], env=env)


def _lima_delete_vm(root: Path, name: str) -> None:
    _require_limactl()
    env = _limactl_env(root)
    if not _lima_instance_exists(root, name):
        print(f"ℹ️  Lima instance not found: {name}")
        return
    _run(["limactl", "delete", "--force", name], env=env)


def _lima_docker_host(root: Path, name: str) -> str:
    env = _limactl_env(root)
    cp = _run(
        ["limactl", "list", name, "--format", "unix://{{.Dir}}/sock/docker.sock"],
        env=env,
        capture_output=True,
    )
    host = (cp.stdout or "").strip()
    if not host:
        raise RuntimeError("Failed to resolve DOCKER_HOST from limactl output.")
    return host


def _docker_env_for_project(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("DOCKER_CONFIG", str(_docker_config(root)))
    _ensure_dirs(Path(env["DOCKER_CONFIG"]))
    return env


def cmd_init(_: argparse.Namespace) -> int:
    root = _project_root()
    name = _instance_name()

    if not _is_macos():
        print("ℹ️  container-init is only needed on macOS (Lima).")
        return 0

    _lima_start_docker_vm(root, name)
    host = _lima_docker_host(root, name)
    print("✅ Lima Docker VM is running")
    print(f"Instance: {name}")
    print(f"DOCKER_HOST: {host}")
    return 0


def cmd_stop(_: argparse.Namespace) -> int:
    root = _project_root()
    name = _instance_name()

    if not _is_macos():
        print("ℹ️  container-stop is only needed on macOS (Lima).")
        return 0

    _lima_stop_vm(root, name)
    print("✅ Lima VM stopped")
    return 0


def cmd_delete(_: argparse.Namespace) -> int:
    root = _project_root()
    name = _instance_name()

    if not _is_macos():
        print("ℹ️  container-delete is only needed on macOS (Lima).")
        return 0

    _lima_delete_vm(root, name)
    print("✅ Lima VM deleted")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    root = _project_root()
    name = _instance_name()

    if not _is_macos():
        print("ℹ️  container-status is only needed on macOS (Lima).")
        return 0

    _require_limactl()
    env = _limactl_env(root)
    _run(["limactl", "list", "--all-fields", name], env=env)
    return 0


def cmd_docker(args: argparse.Namespace) -> int:
    root = _project_root()
    _require_docker_cli()

    env = _docker_env_for_project(root)

    if _is_macos():
        name = _instance_name()
        _lima_start_docker_vm(root, name)
        env["DOCKER_HOST"] = _lima_docker_host(root, name)

    docker_args = _strip_task_runner_sentinel(args.docker_args or [])
    argv = ["docker"] + docker_args
    _run(argv, env=env, cwd=root)
    return 0


def cmd_compose(args: argparse.Namespace) -> int:
    root = _project_root()

    env = _docker_env_for_project(root)

    if _is_macos():
        name = _instance_name()
        _lima_start_docker_vm(root, name)
        env["DOCKER_HOST"] = _lima_docker_host(root, name)

    compose_args = _strip_task_runner_sentinel(args.compose_args or [])
    compose_cmd = _require_docker_compose()
    argv = compose_cmd + compose_args
    _run(argv, env=env, cwd=root)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Project-local container runtime (Lima + Docker) helper"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_init = subparsers.add_parser(
        "init", help="Create/start the project-local Lima Docker VM (macOS only)"
    )
    p_init.set_defaults(func=cmd_init)

    p_stop = subparsers.add_parser("stop", help="Stop the Lima VM (macOS only)")
    p_stop.set_defaults(func=cmd_stop)

    p_delete = subparsers.add_parser(
        "delete", help="Delete the Lima VM (macOS only)"
    )
    p_delete.set_defaults(func=cmd_delete)

    p_status = subparsers.add_parser(
        "status", help="Show Lima VM status (macOS only)"
    )
    p_status.set_defaults(func=cmd_status)

    p_docker = subparsers.add_parser(
        "docker",
        help="Run docker against the project-local engine (macOS) or system engine (other OS)",
    )
    p_docker.add_argument(
        "docker_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed through to docker",
    )
    p_docker.set_defaults(func=cmd_docker)

    p_compose = subparsers.add_parser(
        "compose",
        help="Run docker-compose (or docker compose) against the project-local engine (macOS)",
    )
    p_compose.add_argument(
        "compose_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed through to docker-compose / docker compose",
    )
    p_compose.set_defaults(func=cmd_compose)

    args = parser.parse_args()
    try:
        return int(args.func(args))
    except RuntimeError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as e:
        return int(e.returncode or 1)


if __name__ == "__main__":
    raise SystemExit(main())
