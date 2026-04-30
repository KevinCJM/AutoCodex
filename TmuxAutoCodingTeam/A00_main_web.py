from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Sequence


WEB_HOST = "127.0.0.1"
BACKEND_PORT = 8765
WEB_PORT = 5173


def repo_root() -> Path:
    return Path(__file__).resolve().parent


def web_package_dir() -> Path:
    return repo_root() / "packages" / "web"


def build_backend_command(*, backend_port: int = BACKEND_PORT) -> list[str]:
    return [sys.executable, str(repo_root() / "T11_web_backend.py"), "--port", str(int(backend_port))]


def build_web_command() -> list[str]:
    return ["bun", "run", "dev"]


def _missing_web_dependency_names() -> list[str]:
    package_dir = web_package_dir()
    package_json = package_dir / "package.json"
    if not package_json.exists():
        return []
    package_data = json.loads(package_json.read_text(encoding="utf-8"))
    dependency_names = sorted(
        {
            *package_data.get("dependencies", {}).keys(),
            *package_data.get("devDependencies", {}).keys(),
        }
    )
    return [
        name
        for name in dependency_names
        if not (package_dir / "node_modules" / Path(*name.split("/")) / "package.json").exists()
    ]


def ensure_web_dependencies_installed() -> None:
    package_dir = web_package_dir()
    package_json = package_dir / "package.json"
    if not package_json.exists():
        raise RuntimeError(f"缺少 Web package.json: {package_json}")

    missing = _missing_web_dependency_names()
    if not missing:
        return

    print(f"[web-main] installing Web dependencies: {', '.join(missing)}", flush=True)
    try:
        completed = subprocess.run(
            ["bun", "install", "--frozen-lockfile"],
            cwd=str(package_dir),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError("Web 依赖缺失且未找到 bun，请先安装 Bun") from error

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"Web 依赖安装失败: {detail or f'exit={completed.returncode}'}")

    remaining = _missing_web_dependency_names()
    if remaining:
        raise RuntimeError(f"Web 依赖安装后仍缺少: {', '.join(remaining)}")


def _start_process(name: str, command: Sequence[str], *, cwd: Path) -> subprocess.Popen:
    print(f"[web-main] starting {name}: {' '.join(command)}", flush=True)
    try:
        return subprocess.Popen(list(command), cwd=str(cwd))
    except FileNotFoundError as error:
        raise RuntimeError(f"启动 {name} 失败，缺少命令: {command[0]}") from error


def _wait_for_http(
    url: str,
    *,
    process: subprocess.Popen,
    name: str,
    timeout_sec: float = 20.0,
) -> None:
    deadline = time.monotonic() + timeout_sec
    last_error = ""
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"{name} 已退出: exit={return_code}")
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if 200 <= int(response.status) < 500:
                    return
        except Exception as error:  # noqa: BLE001
            last_error = str(error)
        time.sleep(0.25)
    raise RuntimeError(f"等待 {name} 启动超时: {url}; {last_error}")


def _terminate_processes(processes: Sequence[tuple[str, subprocess.Popen]]) -> None:
    for _, process in reversed(processes):
        if process.poll() is None:
            process.terminate()

    deadline = time.monotonic() + 5.0
    for name, process in reversed(processes):
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if process.poll() is None:
            print(f"[web-main] killing {name}", flush=True)
            process.kill()
            process.wait(timeout=2.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="一键启动 Tmux Web 控制台完整服务")
    parser.add_argument("--skip-install", action="store_true", help="跳过 Web 依赖检查/安装")
    parser.add_argument("--backend-port", type=int, default=BACKEND_PORT, help="WebBackend 端口，默认 8765")
    parser.add_argument("--web-port", type=int, default=WEB_PORT, help="Web 前端端口，当前必须为 5173")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    backend_port = int(args.backend_port)
    web_port = int(args.web_port)

    if backend_port != BACKEND_PORT:
        raise RuntimeError("packages/web/vite.config.ts 当前代理固定到 127.0.0.1:8765，请使用 --backend-port 8765")
    if web_port != WEB_PORT:
        raise RuntimeError("packages/web 当前固定使用 Vite 端口 5173，请使用 --web-port 5173")

    if not args.skip_install:
        ensure_web_dependencies_installed()

    processes: list[tuple[str, subprocess.Popen]] = []
    try:
        backend = _start_process("web-backend", build_backend_command(backend_port=backend_port), cwd=repo_root())
        processes.append(("web-backend", backend))
        _wait_for_http(f"http://{WEB_HOST}:{backend_port}/healthz", process=backend, name="web-backend")

        web = _start_process("web-frontend", build_web_command(), cwd=web_package_dir())
        processes.append(("web-frontend", web))
        _wait_for_http(f"http://{WEB_HOST}:{web_port}", process=web, name="web-frontend")

        print(f"[web-main] ready: http://{WEB_HOST}:{web_port}", flush=True)
        print("[web-main] press Ctrl+C to stop", flush=True)
        while True:
            for name, process in processes:
                return_code = process.poll()
                if return_code is not None:
                    print(f"[web-main] {name} exited: {return_code}", flush=True)
                    return int(return_code or 0)
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 130
    finally:
        _terminate_processes(processes)
        print("[web-main] shutdown complete", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
