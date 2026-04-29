from __future__ import annotations

import argparse
import contextlib
import json
import queue
import signal
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlparse

from canopy_core.bridge.backend import BridgeCore
from canopy_core.runtime.vendor_catalog import VENDOR_ORDER, get_catalog_snapshot, get_default_model_for_vendor
from T12_requirements_common import build_output_path, list_existing_requirements, resolve_existing_directory


class _EventStreamHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[dict[str, Any] | None]] = set()

    def subscribe(self) -> queue.Queue[dict[str, Any] | None]:
        subscriber: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=128)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any] | None]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, message: Mapping[str, Any]) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            item = dict(message)
            try:
                subscriber.put_nowait(item)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass
                try:
                    subscriber.put_nowait(item)
                except queue.Full:
                    continue

    def close(self) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
            self._subscribers.clear()
        for subscriber in subscribers:
            with contextlib.suppress(queue.Full):
                subscriber.put_nowait(None)


class _BridgeWebHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], *, backend: 'WebBackendServer') -> None:
        self.backend = backend
        super().__init__(server_address, handler_class)

    def handle_error(self, request: Any, client_address: tuple[str, int]) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class WebBackendServer(BridgeCore):
    def __init__(self, *, host: str = '127.0.0.1', port: int = 8765) -> None:
        if str(host).strip() != '127.0.0.1':
            raise ValueError('Web backend 仅允许绑定 127.0.0.1')
        super().__init__()
        self.attach_adapter('web')
        self._event_hub = _EventStreamHub()
        self.subscribe_events(self._event_hub.publish)
        self._httpd = _BridgeWebHttpServer((host, int(port)), self._build_handler_class(), backend=self)

    @property
    def host(self) -> str:
        return str(self._httpd.server_address[0])

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def build_agent_catalog(self) -> dict[str, Any]:
        snapshot = get_catalog_snapshot()
        vendors: list[dict[str, Any]] = []
        for vendor_id in VENDOR_ORDER:
            inventory = snapshot.vendor(vendor_id)
            models = []
            for model in inventory.models:
                reasoning = model.reasoning
                efforts = tuple(reasoning.normalized_reasoning_levels or ("high",))
                default_effort = str(reasoning.default_normalized_effort or "").strip()
                if default_effort not in efforts:
                    default_effort = "high" if "high" in efforts else efforts[0]
                models.append(
                    {
                        "model_id": model.model_id,
                        "display_name": model.display_name or model.model_id,
                        "source_kind": model.source_kind,
                        "confidence": model.confidence,
                        "synthetic": model.synthetic,
                        "efforts": list(efforts),
                        "default_effort": default_effort,
                    }
                )
            vendors.append(
                {
                    "vendor_id": vendor_id,
                    "installed": inventory.installed,
                    "scan_status": inventory.scan_status,
                    "source_kind": inventory.source_kind,
                    "confidence": inventory.confidence,
                    "default_model": get_default_model_for_vendor(vendor_id, catalog=snapshot),
                    "models": models,
                }
            )
        return {
            "schema_version": "1.0",
            "generated_at": snapshot.generated_at,
            "vendors": vendors,
        }

    def build_requirements_list(self, project_dir: str) -> dict[str, Any]:
        project_dir_text = str(project_dir or "").strip()
        if not project_dir_text:
            raise ValueError("目录无效: 缺少 project_dir")
        try:
            project_root = resolve_existing_directory(project_dir_text)
            requirements = [
                {
                    "name": name,
                    "path": str(build_output_path(project_root, name).resolve()),
                }
                for name in list_existing_requirements(project_root)
            ]
        except Exception as error:  # noqa: BLE001
            raise ValueError(f"目录无效: {error}") from error
        return {
            "schema_version": "1.0",
            "project_dir": str(Path(project_root).resolve()),
            "requirements": requirements,
        }

    def _build_handler_class(self) -> type[BaseHTTPRequestHandler]:
        backend = self

        class Handler(BaseHTTPRequestHandler):
            server: _BridgeWebHttpServer

            def log_message(self, format: str, *args: object) -> None:  # noqa: A003
                _ = format
                _ = args
                return None

            def _read_json_body(self) -> dict[str, Any]:
                raw_length = self.headers.get('Content-Length', '0')
                try:
                    length = max(int(raw_length), 0)
                except ValueError as error:
                    raise ValueError('无效的 Content-Length') from error
                raw = self.rfile.read(length) if length > 0 else b''
                if not raw:
                    return {}
                payload = json.loads(raw.decode('utf-8'))
                if not isinstance(payload, dict):
                    raise ValueError('请求体必须是 JSON 对象')
                return payload

            def _write_json(self, status: int, payload: Mapping[str, Any]) -> None:
                data = json.dumps(dict(payload), ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _write_error(self, status: int, message: str) -> None:
                self._write_json(status, {'ok': False, 'error': str(message).strip()})

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                try:
                    if parsed.path == '/healthz':
                        self._write_json(HTTPStatus.OK, {'ok': True, 'adapter': 'web', 'host': backend.host, 'port': backend.port})
                        return
                    if parsed.path == '/api/bootstrap':
                        self._write_json(HTTPStatus.OK, {'ok': True, 'payload': backend.bootstrap()})
                        return
                    if parsed.path == '/api/snapshots':
                        self._write_json(HTTPStatus.OK, {'ok': True, 'payload': backend.build_snapshots()})
                        return
                    if parsed.path == '/api/prompt':
                        self._write_json(HTTPStatus.OK, {'ok': True, 'payload': backend.build_prompt_snapshot()})
                        return
                    if parsed.path == '/api/agent-catalog':
                        self._write_json(HTTPStatus.OK, {'ok': True, 'payload': backend.build_agent_catalog()})
                        return
                    if parsed.path == '/api/requirements':
                        query = parse_qs(parsed.query)
                        project_dir_value = str((query.get('project_dir') or [''])[0]).strip()
                        self._write_json(HTTPStatus.OK, {'ok': True, 'payload': backend.build_requirements_list(project_dir_value)})
                        return
                    if parsed.path == '/api/file-preview':
                        query = parse_qs(parsed.query)
                        path_value = str((query.get('path') or [''])[0]).strip()
                        max_bytes_value = str((query.get('max_bytes') or [''])[0]).strip()
                        if not path_value:
                            raise ValueError('缺少 path')
                        max_bytes = int(max_bytes_value) if max_bytes_value else 256 * 1024
                        self._write_json(HTTPStatus.OK, {'ok': True, 'payload': backend.build_file_preview(path_value, max_bytes=max_bytes)})
                        return
                    if parsed.path == '/api/events':
                        self._serve_sse()
                        return
                    self._write_error(HTTPStatus.NOT_FOUND, f'未知路径: {parsed.path}')
                except PermissionError as error:
                    self._write_error(HTTPStatus.FORBIDDEN, str(error))
                except FileNotFoundError as error:
                    self._write_error(HTTPStatus.NOT_FOUND, str(error))
                except ValueError as error:
                    self._write_error(HTTPStatus.BAD_REQUEST, str(error))
                except Exception as error:  # noqa: BLE001
                    self._write_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                try:
                    payload = self._read_json_body()
                    if parsed.path == '/api/request':
                        action = str(payload.get('action', '')).strip()
                        if not action:
                            raise ValueError('缺少 action')
                        result = backend.handle_action(action, payload.get('payload', {}))
                        self._write_json(HTTPStatus.OK, {'ok': True, 'payload': result})
                        return
                    if parsed.path == '/api/prompt-response':
                        prompt_id = str(payload.get('prompt_id', '')).strip()
                        result = backend.resolve_prompt(prompt_id, payload)
                        self._write_json(HTTPStatus.OK, {'ok': True, 'payload': result})
                        return
                    self._write_error(HTTPStatus.NOT_FOUND, f'未知路径: {parsed.path}')
                except ValueError as error:
                    self._write_error(HTTPStatus.BAD_REQUEST, str(error))
                except Exception as error:  # noqa: BLE001
                    self._write_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

            def _serve_sse(self) -> None:
                subscriber = backend._event_hub.subscribe()  # noqa: SLF001
                self.send_response(HTTPStatus.OK)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'keep-alive')
                self.end_headers()
                try:
                    self.wfile.write(b': connected\n\n')
                    self.wfile.flush()
                    while True:
                        try:
                            message = subscriber.get(timeout=15.0)
                        except queue.Empty:
                            self.wfile.write(b': ping\n\n')
                            self.wfile.flush()
                            continue
                        if message is None:
                            break
                        event_type = str(message.get('type', 'message')).strip() or 'message'
                        data = json.dumps(message, ensure_ascii=False)
                        chunk = f'event: {event_type}\ndata: {data}\n\n'.encode('utf-8')
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                finally:
                    backend._event_hub.unsubscribe(subscriber)  # noqa: SLF001

        return Handler

    def serve_forever(self) -> int:
        self._httpd.serve_forever(poll_interval=0.2)
        return 0

    def shutdown(self, *, cleanup_tmux: bool) -> list[str]:
        self._event_hub.close()
        self._httpd.shutdown()
        self._httpd.server_close()
        return super().shutdown(cleanup_tmux=cleanup_tmux)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Web adapter backend')
    parser.add_argument('--port', type=int, default=8765, help='监听端口，默认 8765；传 0 表示自动分配')
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = WebBackendServer(port=int(args.port or 0))
    base_url = f'http://{server.host}:{server.port}'

    def _handle_signal(signum: int, _frame: Any) -> None:
        print(f'[web-backend] signal={signum}, shutting down', flush=True)
        server.shutdown(cleanup_tmux=True)
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + int(signum))

    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    try:
        print(f'[web-backend] listening on {base_url}', flush=True)
        print(f'[web-backend] healthz: {base_url}/healthz', flush=True)
        print(f'[web-backend] sse: {base_url}/api/events', flush=True)
        print('[web-backend] press Ctrl+C to stop', flush=True)
        return server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        print('[web-backend] shutdown complete', flush=True)
        server.shutdown(cleanup_tmux=True)


if __name__ == '__main__':
    raise SystemExit(main())
