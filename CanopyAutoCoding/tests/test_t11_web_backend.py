from __future__ import annotations

import http.client
import json
import queue
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from T11_tui_backend import BridgeCore
from T11_web_backend import WebBackendServer


class WebBackendTests(unittest.TestCase):
    def _start_server(self) -> tuple[WebBackendServer, threading.Thread]:
        server = WebBackendServer(port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _stop_server(self, server: WebBackendServer, thread: threading.Thread) -> None:
        server.shutdown(cleanup_tmux=False)
        thread.join(timeout=2.0)

    @staticmethod
    def _get_json(server: WebBackendServer, path: str) -> dict[str, object]:
        with urllib.request.urlopen(f'http://127.0.0.1:{server.port}{path}', timeout=5) as response:
            return json.loads(response.read().decode('utf-8'))

    @staticmethod
    def _post_json(server: WebBackendServer, path: str, payload: dict[str, object]) -> dict[str, object]:
        request = urllib.request.Request(
            f'http://127.0.0.1:{server.port}{path}',
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode('utf-8'))

    def test_web_backend_binds_localhost_and_serves_bootstrap_and_snapshots(self):
        server, thread = self._start_server()
        try:
            health = self._get_json(server, '/healthz')
            bootstrap = self._get_json(server, '/api/bootstrap')
            snapshots = self._get_json(server, '/api/snapshots')
        finally:
            self._stop_server(server, thread)

        self.assertEqual(server.host, '127.0.0.1')
        self.assertTrue(health['ok'])
        self.assertEqual(health['adapter'], 'web')
        self.assertIn('routes', bootstrap['payload'])
        self.assertIn('snapshots', bootstrap['payload'])
        self.assertIn('overall-review', bootstrap['payload']['routes'])
        self.assertIn('stages', snapshots['payload'])
        self.assertIn('development', snapshots['payload']['stages'])
        self.assertIn('overall-review', snapshots['payload']['stages'])

    def test_web_backend_prompt_response_roundtrip(self):
        server, thread = self._start_server()
        try:
            prompt_queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
            server._prompt_broker._pending['prompt_1'] = prompt_queue  # noqa: SLF001
            payload = self._post_json(server, '/api/prompt-response', {'prompt_id': 'prompt_1', 'value': 'ok'})
        finally:
            self._stop_server(server, thread)

        self.assertTrue(payload['ok'])
        self.assertEqual(payload['payload']['accepted'], True)
        self.assertEqual(prompt_queue.get_nowait()['value'], 'ok')

    def test_web_backend_request_returns_immediate_ack_for_background_stage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            server, thread = self._start_server()
            try:
                with patch('canopy_core.bridge.backend.run_detailed_design_stage', return_value={'project_dir': str(project_dir), 'requirement_name': '需求A', 'passed': True}):
                    payload = self._post_json(
                        server,
                        '/api/request',
                        {
                            'action': 'stage.a05.start',
                            'payload': {'argv': ['--project-dir', str(project_dir), '--requirement-name', '需求A']},
                        },
                    )
                    for worker in list(server._workers.values()):  # noqa: SLF001
                        worker.join(timeout=2.0)
            finally:
                self._stop_server(server, thread)

        self.assertTrue(payload['ok'])
        self.assertEqual(payload['payload'], {'accepted': True, 'deferred': True})

    def test_web_backend_sse_streams_core_events(self):
        server, thread = self._start_server()
        conn = http.client.HTTPConnection('127.0.0.1', server.port, timeout=5)
        try:
            conn.request('GET', '/api/events')
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.fp.readline().decode('utf-8'), ': connected\n')
            self.assertEqual(response.fp.readline().decode('utf-8'), '\n')

            server.emit_event('log.append', {'text': 'hello\n'})

            event_line = response.fp.readline().decode('utf-8').strip()
            data_line = response.fp.readline().decode('utf-8').strip()
            blank_line = response.fp.readline().decode('utf-8').strip()
        finally:
            conn.close()
            self._stop_server(server, thread)

        self.assertEqual(event_line, 'event: log.append')
        self.assertEqual(blank_line, '')
        event_payload = json.loads(data_line.removeprefix('data: '))
        self.assertEqual(event_payload['type'], 'log.append')
        self.assertEqual(event_payload['payload']['text'], 'hello\n')

    def test_bridge_core_rejects_second_adapter_on_same_instance(self):
        core = BridgeCore()
        core.attach_adapter('web')
        with self.assertRaisesRegex(RuntimeError, 'adapter'):
            core.attach_adapter('tui')


if __name__ == '__main__':
    unittest.main()
