from __future__ import annotations

import http.client
import io
import json
import queue
import signal
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from canopy_core.bridge import web_backend as web_backend_module
from T11_tui_backend import BridgeCore, PendingPromptState
from T11_web_backend import WebBackendServer, main as web_backend_main


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

    def test_web_backend_exposes_pending_prompt_snapshot(self):
        server, thread = self._start_server()
        try:
            server._pending_prompt = PendingPromptState(  # noqa: SLF001
                prompt_id='prompt_1',
                prompt_type='select',
                payload={
                    'title': '选择模型',
                    'options': [{'value': 'gemini', 'label': 'Gemini'}],
                    'default_value': 'gemini',
                },
            )
            payload = self._get_json(server, '/api/prompt')
        finally:
            self._stop_server(server, thread)

        self.assertTrue(payload['ok'])
        self.assertTrue(payload['payload']['pending'])
        self.assertEqual(payload['payload']['prompt_id'], 'prompt_1')
        self.assertEqual(payload['payload']['prompt_type'], 'select')
        self.assertEqual(payload['payload']['payload']['default_value'], 'gemini')

    def test_web_backend_exposes_read_only_agent_catalog(self):
        def model(model_id: str, efforts: tuple[str, ...] = ('high',), default_effort: str = 'high') -> SimpleNamespace:
            return SimpleNamespace(
                model_id=model_id,
                display_name=model_id,
                source_kind='test',
                confidence='high',
                synthetic=False,
                reasoning=SimpleNamespace(
                    normalized_reasoning_levels=efforts,
                    default_normalized_effort=default_effort,
                ),
            )

        inventories = {
            'codex': SimpleNamespace(vendor_id='codex', installed=True, scan_status='ok', source_kind='test', confidence='high', default_model='gpt-5.4', models=[model('gpt-5.4', ('low', 'medium', 'high'), 'high')]),
            'claude': SimpleNamespace(vendor_id='claude', installed=True, scan_status='ok', source_kind='test', confidence='high', default_model='sonnet', models=[model('sonnet')]),
            'gemini': SimpleNamespace(vendor_id='gemini', installed=True, scan_status='ok', source_kind='test', confidence='high', default_model='auto', models=[model('auto'), model('flash', ('low', 'medium', 'high'), 'high')]),
            'opencode': SimpleNamespace(vendor_id='opencode', installed=True, scan_status='ok', source_kind='test', confidence='high', default_model='opencode/big-pickle', models=[model('opencode/big-pickle', ('low', 'medium', 'high', 'xhigh', 'max'), 'high')]),
        }
        fake_snapshot = SimpleNamespace(
            generated_at='2026-04-27T00:00:00+08:00',
            vendor=lambda vendor_id: inventories[vendor_id],
        )
        server, thread = self._start_server()
        try:
            with patch('canopy_core.bridge.web_backend.get_catalog_snapshot', return_value=fake_snapshot):
                payload = self._get_json(server, '/api/agent-catalog')
        finally:
            self._stop_server(server, thread)

        self.assertTrue(payload['ok'])
        catalog = payload['payload']
        self.assertEqual(catalog['schema_version'], '1.0')
        vendors = {item['vendor_id']: item for item in catalog['vendors']}
        self.assertEqual(tuple(vendors), ('codex', 'claude', 'gemini', 'opencode'))
        self.assertIn('default_model', vendors['gemini'])
        self.assertIsInstance(vendors['gemini']['models'], list)
        flash = next(item for item in vendors['gemini']['models'] if item['model_id'] == 'flash')
        self.assertIn('high', flash['efforts'])
        self.assertIn(flash['default_effort'], flash['efforts'])
        self.assertNotIn('commands', catalog)

    def test_web_backend_lists_existing_requirements(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / '需求A_原始需求.md').write_text('正文A\n', encoding='utf-8')
            (root / '空需求_原始需求.md').write_text('', encoding='utf-8')
            server, thread = self._start_server()
            try:
                payload = self._get_json(
                    server,
                    '/api/requirements?project_dir=' + urllib.parse.quote(str(root)),
                )
            finally:
                self._stop_server(server, thread)

        self.assertTrue(payload['ok'])
        result = payload['payload']
        self.assertEqual(result['schema_version'], '1.0')
        self.assertEqual(result['project_dir'], str(root.resolve()))
        self.assertEqual(result['requirements'], [{'name': '需求A', 'path': str((root / '需求A_原始需求.md').resolve())}])

    def test_web_backend_requirement_list_uses_tui_directory_error(self):
        server, thread = self._start_server()
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self._get_json(
                    server,
                    '/api/requirements?project_dir=' + urllib.parse.quote('/definitely/missing/project'),
                )
            error_payload = json.loads(raised.exception.read().decode('utf-8'))
        finally:
            self._stop_server(server, thread)

        self.assertEqual(raised.exception.code, 400)
        self.assertFalse(error_payload['ok'])
        self.assertIn('目录无效:', error_payload['error'])

    def test_web_backend_file_preview_allows_only_snapshot_exposed_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            preview_path = Path(tmpdir) / 'preview.md'
            preview_path.write_text('hello web preview\n', encoding='utf-8')
            hidden_path = Path(tmpdir) / 'hidden.md'
            hidden_path.write_text('hidden\n', encoding='utf-8')
            server, thread = self._start_server()
            try:
                server._pending_prompt = PendingPromptState(  # noqa: SLF001
                    prompt_id='prompt_1',
                    prompt_type='select',
                    payload={'preview_path': str(preview_path)},
                )
                preview = self._get_json(
                    server,
                    '/api/file-preview?path=' + urllib.parse.quote(str(preview_path)),
                )
                with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                    self._get_json(
                        server,
                        '/api/file-preview?path=' + urllib.parse.quote(str(hidden_path)),
                    )
            finally:
                self._stop_server(server, thread)

        self.assertTrue(preview['ok'])
        self.assertEqual(preview['payload']['path'], str(preview_path.resolve()))
        self.assertEqual(preview['payload']['text'], 'hello web preview\n')
        self.assertEqual(unauthorized.exception.code, 403)

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

    def test_bridge_core_writes_payload_agent_config_to_temp_file(self):
        argv = BridgeCore._argv_with_payload_agent_config(  # noqa: SLF001
            ['--project-dir', '/tmp/project'],
            {
                'agent_config': {
                    'main': {'vendor': 'codex', 'model': 'gpt-5.4', 'effort': 'high'},
                    'stages': {'development': {'main': {'vendor': 'gemini', 'model': 'flash', 'effort': 'medium'}}},
                }
            },
        )

        self.assertEqual(argv[-2], '--agent-config')
        config_path = Path(argv[-1])
        self.assertTrue(config_path.exists())
        payload = json.loads(config_path.read_text(encoding='utf-8'))
        self.assertEqual(payload['stages']['development']['main']['vendor'], 'gemini')

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

    def test_web_backend_suppresses_client_disconnect_tracebacks(self):
        server, thread = self._start_server()
        try:
            with patch.object(web_backend_module.ThreadingHTTPServer, 'handle_error', side_effect=AssertionError('disconnect should be quiet')):
                try:
                    raise ConnectionResetError('client closed connection')
                except ConnectionResetError:
                    server._httpd.handle_error(object(), ('127.0.0.1', 12345))  # noqa: SLF001
        finally:
            self._stop_server(server, thread)

    def test_bridge_core_rejects_second_adapter_on_same_instance(self):
        core = BridgeCore()
        core.attach_adapter('web')
        with self.assertRaisesRegex(RuntimeError, 'adapter'):
            core.attach_adapter('tui')

    def test_web_backend_main_prints_startup_banner(self):
        class FakeServer:
            def __init__(self, *, port: int) -> None:
                self.host = '127.0.0.1'
                self.port = int(port)
                self.shutdown_calls: list[bool] = []

            def serve_forever(self) -> int:
                return 0

            def shutdown(self, *, cleanup_tmux: bool) -> list[str]:
                self.shutdown_calls.append(bool(cleanup_tmux))
                return []

        buffer = io.StringIO()
        with (
            patch('canopy_core.bridge.web_backend.WebBackendServer', FakeServer),
            patch('canopy_core.bridge.web_backend.signal.getsignal', return_value=signal.SIG_DFL),
            patch('canopy_core.bridge.web_backend.signal.signal'),
            redirect_stdout(buffer),
        ):
            status = web_backend_main(['--port', '8765'])

        output = buffer.getvalue()
        self.assertEqual(status, 0)
        self.assertIn('[web-backend] listening on http://127.0.0.1:8765', output)
        self.assertIn('[web-backend] healthz: http://127.0.0.1:8765/healthz', output)
        self.assertIn('[web-backend] sse: http://127.0.0.1:8765/api/events', output)
        self.assertIn('[web-backend] press Ctrl+C to stop', output)
        self.assertIn('[web-backend] shutdown complete', output)


if __name__ == '__main__':
    unittest.main()
