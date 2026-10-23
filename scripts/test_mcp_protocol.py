"""Real newline stdio round trips; HTTP fixtures never access the real desktop."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import mcp_server as mcp
from smart_desktop import OrganizerModel, ControlService

ROOT = Path(__file__).resolve().parent.parent


class MCPProtocolTests(unittest.TestCase):
    def run_stdio(self, messages, port, *args):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/mcp_server.py'), *args],
            input=''.join(json.dumps(msg, ensure_ascii=False) + '\n' for msg in messages),
            text=True, encoding='utf-8', capture_output=True, timeout=20,
            env=dict(os.environ, DESKFLOW_API_PORT=str(port)))
        self.assertEqual(0, result.returncode, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()], result

    def test_stdio_discovery_call_error_utf8_and_readonly_geometry(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append((self.path, payload))
                self.send_response(400 if payload.get('query') == 'error' else 200)
                self.end_headers()
                self.wfile.write(json.dumps({'items': [{'name': '测试 文件.txt'}], 'detail': 'fixture failure'}).encode())
            def do_GET(self):
                seen.append((self.path, None))
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({'config': {'ui': {'panel_positions': {'work': [30, 40]}}, 'item_metadata': {'secret': 'excluded'}}}).encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        requests = [
            ('initialize', {'protocolVersion': '2025-06-18'}), ('tools/list', {}),
            ('resources/list', {}), ('resources/read', {'uri': mcp.GUIDE_URI}),
            ('prompts/list', {}), ('prompts/get', {'name': 'organize-desktop'}),
            ('tools/call', {'name': 'get_desktop_catalog', 'arguments': {'query': '中文 测试'}}),
            ('tools/call', {'name': 'get_desktop_arrangement'}),
            ('tools/call', {'name': 'get_desktop_catalog', 'arguments': {'query': 'error'}}),
            ('tools/call', {'name': 'get_desktop_catalog', 'arguments': {'limit': True}}),
            ('tools/call', {'name': 'invalid'}), ('ping', {}),
        ]
        messages = [{'jsonrpc': '2.0', 'id': i, 'method': method, 'params': params} for i, (method, params) in enumerate(requests)]
        messages.insert(1, {'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        messages.insert(2, {'jsonrpc': '2.0', 'method': 'unknown-notification'})
        try:
            replies, result = self.run_stdio(messages, server.server_port)
        finally:
            server.shutdown(); server.server_close(); thread.join()
        self.assertEqual(list(range(len(requests))), [r['id'] for r in replies])
        self.assertEqual('2025-06-18', replies[0]['result']['protocolVersion'])
        self.assertGreaterEqual(len(replies[1]['result']['tools']), 18)
        self.assertIn('manual', replies[3]['result']['contents'][0]['text'])
        self.assertIn('messages', replies[5]['result'])
        self.assertIn('测试 文件.txt', replies[6]['result']['content'][0]['text'])
        geometry = json.loads(replies[7]['result']['content'][0]['text'])
        self.assertEqual({'work': [30, 40]}, geometry['panel_positions'])
        self.assertNotIn('secret', json.dumps(geometry))
        self.assertTrue(replies[8]['result']['isError'])
        self.assertEqual(-32602, replies[9]['error']['code'])
        self.assertEqual(-32602, replies[10]['error']['code'])
        self.assertEqual([('/catalog', {'query': '中文 测试'}), ('/layout', None), ('/catalog', {'query': 'error'})], seen)
        self.assertEqual('', result.stderr)

    def test_invalid_rpc_and_schema_are_never_executed(self):
        for line, code in [('bad', -32700), ('[]', -32600), ('null', -32600),
                           ('{"jsonrpc":"2.0","method":"ping","id":true}', -32600),
                           ('{"jsonrpc":"2.0","method":"missing","id":1}', -32601)]:
            self.assertEqual(code, mcp.process_line(line)['error']['code'])
        tool = mcp.TOOL_INDEX['organize_desktop_items']
        for invalid in [None, [], {'operations': []}, {'operations': [{'name': 'x', 'category': 4}]},
                        {'operations': [{'name': 'x'}], 'allow_manual': 'true'},
                        {'operations': [{'name': 'x', 'unknown': True}]}]:
            with self.assertRaises(mcp.RPCError):
                mcp.validate(invalid, tool['inputSchema'])

    def test_print_config_source_and_frozen_does_not_start_watcher(self):
        output = io.StringIO()
        with patch.object(mcp, 'watch_update') as watcher, patch('sys.stdout', output):
            self.assertEqual(0, mcp.main(['--print-config']))
        watcher.assert_not_called()
        config = json.loads(output.getvalue())['mcpServers']['deskflow']
        self.assertEqual(sys.executable, config['command'])
        self.assertTrue(Path(config['args'][0]).is_absolute())
        import app_runtime
        with patch.object(app_runtime, 'FROZEN', True), patch.object(app_runtime, 'APP_ROOT', Path('G:/中文 安装/Deskflow')):
            output = io.StringIO()
            with patch('sys.stdout', output):
                mcp.main(['--print-config'])
            config = json.loads(output.getvalue())['mcpServers']['deskflow']
        self.assertEqual(str(Path('G:/中文 安装/Deskflow/DeskflowMCP.exe')), config['command'])
        self.assertEqual([], config['args'])

    def test_opt_in_logs_never_pollute_stdio(self):
        import app_runtime
        import logging
        temporary_root = ROOT / 'build/d14/tmp'
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
            output = io.StringIO()
            messages = '\n'.join(json.dumps({'jsonrpc': '2.0', 'id': i, 'method': method, 'params': params}) for i, (method, params) in enumerate([
                ('tools/call', {'name': 'get_desktop_catalog'}), ('ping', {})])) + '\n'
            with patch.object(app_runtime, 'APP_ROOT', Path(directory)), patch('sys.stdout', output), patch('sys.stdin', io.StringIO(messages)), patch.object(mcp, 'watch_update'), patch.object(mcp, 'request', side_effect=RuntimeError('fixture failure')):
                mcp.main(['--log-level', 'debug'])
            replies = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([0, 1], [reply['id'] for reply in replies])
            self.assertTrue(replies[0]['result']['isError'])
            log = next(Path(directory).glob('*.log')).read_text(encoding='utf-8')
            self.assertIn('Traceback', log)
            self.assertIn('MCP tool failed', log)
            self.assertIn('elapsed_ms=', log)
            self.assertRegex(log, r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \d+ error')
            from diagnostics import configure_logging
            configure_logging(None)


class ManualProtectionTests(unittest.TestCase):
    def setUp(self):
        class Store:
            def __init__(self):
                self.saved = 0
                self.lock = threading.RLock()
                self.data = {'overrides': {'manual.txt': 'work'}, 'custom_blocks': [], 'ui': {},
                             'item_metadata': {}, 'tip_summaries': {}, 'pending_inbox': {}}
            def save(self):
                self.saved += 1
        self.store = Store()
        self.model = OrganizerModel(self.store)
        self.model.scan = lambda: {'work': [{'name': 'manual.txt', 'manual': True}], 'archive': [{'name': 'auto.txt', 'manual': False}]}

    def test_manual_change_rejects_entire_batch_and_reset(self):
        baseline = copy.deepcopy(self.store.data)
        for change in [{'category': 'archive'}, {'reset_category': True}]:
            with self.assertRaisesRegex(ValueError, 'manual classification protected'):
                self.model.organize_items([{'name': 'auto.txt', 'summary': 'new'}, {'name': 'manual.txt', **change}])
            self.assertEqual(baseline, self.store.data)
            self.assertEqual(0, self.store.saved)

    def test_metadata_same_category_allowed_dry_run_no_write_and_authorized(self):
        baseline = copy.deepcopy(self.store.data)
        self.model.organize_items([{'name': 'manual.txt', 'category': 'work', 'summary': 'purpose'}], dry_run=True)
        self.assertEqual(baseline, self.store.data)
        self.assertEqual(0, self.store.saved)
        self.model.organize_items([{'name': 'manual.txt', 'category': 'work', 'summary': 'purpose'}])
        self.assertEqual('purpose', self.store.data['item_metadata']['manual.txt']['summary'])
        self.model.organize_items([{'name': 'manual.txt', 'category': 'archive'}], allow_manual=True)
        self.assertEqual('archive', self.store.data['overrides']['manual.txt'])

    def test_model_and_http_require_real_boolean(self):
        for kwargs in [{'dry_run': 'false'}, {'allow_manual': 'true'}]:
            with self.assertRaisesRegex(ValueError, 'booleans'):
                self.model.organize_items([{'name': 'manual.txt', 'category': 'archive'}], **kwargs)
        # Invoke the actual registered route without running an overlay or opening a port.
        import queue
        control = ControlService(self.model, queue.Queue())
        endpoint = next(route.endpoint for route in control.app.routes if route.path == '/organize-batch')
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as error:
            endpoint({'operations': [{'name': 'manual.txt', 'category': 'archive'}], 'allow_manual': 'true'})
        self.assertEqual(400, error.exception.status_code)
        self.assertEqual(0, self.store.saved)

    def test_actual_new_desktop_file_dry_run_and_invalid_batch_write_nothing(self):
        temporary_root = ROOT / 'build/d14/tmp'
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
            desktop = Path(directory)
            (desktop / 'new.txt').write_text('fresh document', encoding='utf-8')
            self.store.data.update({'desktop': str(desktop), 'seen': {}, 'visual_assets': {},
                'document_summaries': {}, 'folder_summaries': {}, 'markdown_summaries': {}})
            model = OrganizerModel(self.store)
            baseline = copy.deepcopy(self.store.data)
            with patch('smart_desktop.COMMON_DESKTOP', desktop / 'missing'):
                model.organize_items([{'name': 'new.txt', 'category': 'work'}], dry_run=True)
                self.assertEqual(baseline, self.store.data)
                self.assertEqual(0, self.store.saved)
                model.manage_categories([{'action': 'create', 'title': 'preview'}], dry_run=True)
                self.assertEqual(baseline, self.store.data)
                self.assertEqual(0, self.store.saved)
                with self.assertRaises(ValueError):
                    model.manage_categories([{'action': 'create', 'title': 'preview'}, {'action': 'delete', 'category': 'work'}])
                self.assertEqual(baseline, self.store.data)
                self.assertEqual(0, self.store.saved)
                with self.assertRaises(ValueError):
                    model.organize_items([{'name': 'new.txt', 'category': 'work'}, {'name': 'missing.txt', 'category': 'work'}])
                self.assertEqual(baseline, self.store.data)
                self.assertEqual(0, self.store.saved)

    def test_parallel_tag_merges_preserve_both_updates(self):
        self.store.data['item_metadata']['manual.txt'] = {'tags': ['original']}
        errors = []
        def merge(tag):
            try:
                self.model.organize_items([{'name': 'manual.txt', 'tags': [tag], 'tag_mode': 'merge'}])
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=merge, args=(tag,)) for tag in ['first', 'second']]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual({'original', 'first', 'second'}, set(self.store.data['item_metadata']['manual.txt']['tags']))

    def test_http_dryrun_has_no_event_or_store_write(self):
        import queue
        events = queue.Queue()
        control = ControlService(self.model, events)
        endpoint = next(route.endpoint for route in control.app.routes if route.path == '/organize-batch')
        baseline = copy.deepcopy(self.store.data)
        endpoint({'operations': [{'name': 'manual.txt', 'summary': 'preview'}], 'dry_run': True})
        self.assertTrue(events.empty())
        self.assertEqual(baseline, self.store.data)
        self.assertEqual(0, self.store.saved)

    def test_http_and_model_reject_string_boolean_and_coerced_fields(self):
        baseline = copy.deepcopy(self.store.data)
        for operation in [{'name': 'manual.txt', 'reset_category': 'false'},
                          {'name': 'manual.txt', 'category': None},
                          {'name': 'manual.txt', 'tags': [12]},
                          {'name': 'manual.txt', 'summary': None}]:
            with self.assertRaises(ValueError):
                self.model.organize_items([operation], allow_manual=True)
        for action in [{'action': 'create', 'title': None}, {'action': 'create', 'title': 42}]:
            with self.assertRaises(ValueError):
                self.model.manage_categories([action])
        self.assertEqual(baseline, self.store.data)
        self.assertEqual(0, self.store.saved)

    def test_slow_snapshot_allows_manual_assignment_then_rejects_stale_batch(self):
        scanned, proceed, assignment_started = threading.Event(), threading.Event(), threading.Event()
        failures = []
        def scan():
            if not scanned.is_set():
                scanned.set()
                if not proceed.wait(5):
                    raise AssertionError('scan barrier timeout')
            return {'work': [{'name': 'manual.txt', 'manual': True}]}
        self.model.scan = scan
        def batch():
            try:
                self.model.organize_items([{'name': 'manual.txt', 'category': 'work', 'tags': ['kept']}])
            except Exception as error:
                failures.append(error)
        def assign():
            assignment_started.set()
            self.model.set_category('manual.txt', 'archive')
        worker = threading.Thread(target=batch)
        worker.start()
        self.assertTrue(scanned.wait(5))
        manual = threading.Thread(target=assign)
        manual.start()
        self.assertTrue(assignment_started.wait(5))
        manual.join(1)
        self.assertFalse(manual.is_alive(), 'slow snapshot must not hold live Store lock')
        proceed.set()
        worker.join(5); manual.join(5)
        self.assertFalse(worker.is_alive() or manual.is_alive())
        self.assertEqual(1, len(failures))
        self.assertIn('manual classification protected', str(failures[0]))
        self.assertEqual('archive', self.store.data['overrides']['manual.txt'])
        self.assertEqual({}, self.store.data['item_metadata'])

    def test_authorized_slow_snapshot_merges_latest_metadata(self):
        entered, proceed = threading.Event(), threading.Event()
        def scan():
            if not entered.is_set():
                entered.set()
                self.assertTrue(proceed.wait(5))
            return {'work': [{'name': 'manual.txt', 'manual': True}]}
        self.model.scan = scan
        errors = []
        def batch():
            try:
                self.model.organize_items([{'name': 'manual.txt', 'category': 'work', 'tags': ['batch'], 'tag_mode': 'merge'}], allow_manual=True)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=batch); thread.start()
        self.assertTrue(entered.wait(5))
        self.model.set_category('manual.txt', 'archive')
        with self.store.lock:
            self.store.data['item_metadata']['manual.txt'] = {'tags': ['latest']}
            self.store.data['seen'] = {'live-entry': {'first_seen_epoch': 123}}
        proceed.set(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual({'latest', 'batch'}, set(self.store.data['item_metadata']['manual.txt']['tags']))
        self.assertEqual(123, self.store.data['seen']['live-entry']['first_seen_epoch'])

    def test_real_scan_and_batch_rename_or_recycle_never_resurrect_old_keys(self):
        from smart_desktop import Store
        root = ROOT / 'build/d14/tmp'; root.mkdir(parents=True, exist_ok=True)
        for action in ('scan-rename', 'batch-rename', 'scan-recycle', 'scan-metadata'):
            with self.subTest(action=action), tempfile.TemporaryDirectory(dir=root) as directory:
                desktop = Path(directory)
                old = desktop / 'manual.txt'; old.write_text('document', encoding='utf-8')
                self.store.data = Store.__new__(Store)._defaults()
                self.store.data.update({'desktop': str(desktop), 'overrides': {'manual.txt': 'work'},
                    'item_metadata': {'manual.txt': {'summary': 'old'}}})
                model = OrganizerModel(self.store)
                entered, proceed = threading.Event(), threading.Event()
                original = OrganizerModel._scan_locked
                outputs, errors = [], []
                def blocked_scan(target):
                    result = original(target)
                    if not entered.is_set():
                        entered.set()
                        if not proceed.wait(5): raise AssertionError('barrier timeout')
                    return result
                def worker():
                    try:
                        outputs.append(model.organize_items([{'name': 'manual.txt', 'tags': ['new']}]) if action.startswith('batch') else model.scan())
                    except Exception as error:
                        errors.append(error)
                with patch('smart_desktop.COMMON_DESKTOP', desktop / 'missing'), patch.object(OrganizerModel, '_scan_locked', blocked_scan):
                    thread = threading.Thread(target=worker); thread.start()
                    self.assertTrue(entered.wait(5))
                    started = __import__('time').perf_counter()
                    if action.endswith('rename'):
                        model.rename_item(str(old), 'renamed.txt')
                    elif action.endswith('recycle'):
                        old.unlink(); model.forget_item_state(old.name)
                    else:
                        with self.store.lock:
                            self.store.data['item_metadata']['manual.txt'] = {'summary': 'latest'}
                            self.store.data['tip_summaries'].pop('manual.txt', None)
                    self.assertLess(__import__('time').perf_counter() - started, 1, 'slow scan held live Store lock')
                    proceed.set(); thread.join(5)
                    self.assertFalse(thread.is_alive())
                if action == 'batch-rename':
                    self.assertEqual(1, len(errors))
                    self.assertIn('not found', str(errors[0]))
                else:
                    self.assertEqual([], errors)
                if action.endswith(('rename', 'recycle')):
                    for field in ('overrides', 'seen', 'tip_summaries', 'document_summaries'):
                        self.assertNotIn('manual.txt', self.store.data[field])
                    if action.endswith('rename'):
                        self.assertTrue((desktop / 'renamed.txt').exists())
                        self.assertEqual('old', self.store.data['item_metadata']['renamed.txt']['summary'])
                    if outputs:
                        self.assertNotIn('manual.txt', [item['name'] for items in outputs[0].values() for item in items])
                else:
                    self.assertEqual('latest', self.store.data['item_metadata']['manual.txt']['summary'])
                    self.assertNotIn('manual.txt', self.store.data['tip_summaries'])



if __name__ == '__main__':
    unittest.main()
