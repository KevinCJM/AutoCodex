from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import A00_main_web


class _FakeProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def poll(self):  # noqa: ANN201
        if self.killed:
            return -9
        if self.terminated:
            return -15
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None):  # noqa: ANN001, ANN201
        return self.poll()


class A00MainWebTests(unittest.TestCase):
    def test_build_commands_use_expected_entrypoints(self):
        backend_command = A00_main_web.build_backend_command()

        self.assertEqual(backend_command[0], sys.executable)
        self.assertEqual(Path(backend_command[1]).name, "T11_web_backend.py")
        self.assertEqual(backend_command[2:], ["--port", "8765"])
        self.assertEqual(A00_main_web.build_web_command(), ["bun", "run", "dev"])

    def test_main_starts_backend_then_frontend_and_cleans_up_on_keyboard_interrupt(self):
        started: list[tuple[list[str], str]] = []
        waits: list[str] = []
        processes = [_FakeProcess(), _FakeProcess()]

        def fake_popen(command, cwd=None):  # noqa: ANN001
            started.append((list(command), str(cwd)))
            return processes[len(started) - 1]

        def fake_wait_for_http(url, **kwargs):  # noqa: ANN001
            waits.append(url)

        with patch("A00_main_web.ensure_web_dependencies_installed") as ensure_deps, patch(
            "A00_main_web.subprocess.Popen",
            side_effect=fake_popen,
        ), patch("A00_main_web._wait_for_http", side_effect=fake_wait_for_http), patch(
            "A00_main_web.time.sleep",
            side_effect=KeyboardInterrupt,
        ):
            exit_code = A00_main_web.main([])

        self.assertEqual(exit_code, 130)
        ensure_deps.assert_called_once_with()
        self.assertEqual(Path(started[0][0][1]).name, "T11_web_backend.py")
        self.assertEqual(started[0][0][2:], ["--port", "8765"])
        self.assertEqual(started[0][1], str(A00_main_web.repo_root()))
        self.assertEqual(started[1][0], ["bun", "run", "dev"])
        self.assertEqual(started[1][1], str(A00_main_web.web_package_dir()))
        self.assertEqual(
            waits,
            [
                "http://127.0.0.1:8765/healthz",
                "http://127.0.0.1:5173",
            ],
        )
        self.assertTrue(all(process.terminated for process in processes))

    def test_main_rejects_non_default_backend_port_until_vite_proxy_is_configurable(self):
        with self.assertRaisesRegex(RuntimeError, "代理固定"):
            A00_main_web.main(["--backend-port", "9999", "--skip-install"])

    def test_main_rejects_non_default_web_port_until_vite_port_is_configurable(self):
        with self.assertRaisesRegex(RuntimeError, "端口 5173"):
            A00_main_web.main(["--web-port", "3000", "--skip-install"])


if __name__ == "__main__":
    unittest.main()
