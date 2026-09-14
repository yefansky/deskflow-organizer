"""Exercise real frozen Tk/Qt startup and stdio MCP over an isolated HTTP fixture."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def verify(bundle):
    bundle = Path(bundle).resolve()
    evidence = Path('build/verification').resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    output = evidence / 'gui-smoke.json'
    output.unlink(missing_ok=True)
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
    result = subprocess.run([str(bundle / 'DESKFLOW.exe'), '--smoke-test', str(output)], env=env, timeout=90, capture_output=True)
    assert result.returncode == 0 and output.is_file(), (result.returncode, result.stderr, output.with_suffix('.error.txt').read_text(encoding='utf-8') if output.with_suffix('.error.txt').exists() else 'No smoke log')
    assert json.loads(output.read_text())['ok']
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            seen.append((self.path, payload))
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'ok': True, 'query': payload.get('query'), 'items': [{'name': '测试 文件.txt'}]}, ensure_ascii=False).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'verification', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'get_desktop_catalog', 'arguments': {'query': '中文 测试'}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'invalid_tool', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'ping'},
        ]
        env['DESKFLOW_API_PORT'] = str(server.server_port)
        result = subprocess.run([str(bundle / 'DeskflowMCP.exe')], input=''.join(json.dumps(m, ensure_ascii=False) + '\n' for m in messages),
                                text=True, encoding='utf-8', capture_output=True, env=env, timeout=30)
        assert result.returncode == 0, result.stderr
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        assert [r['id'] for r in replies] == [1, 2, 3, 4, 5], replies
        assert len(replies[1]['result']['tools']) >= 17
        content = json.loads(replies[2]['result']['content'][0]['text'])
        assert content['query'] == '中文 测试'
        assert 'error' in replies[3]
        assert seen == [('/catalog', {'query': '中文 测试'})], seen
        (evidence / 'mcp-smoke.json').write_text(json.dumps({'ok': True, 'tool_count': len(replies[1]['result']['tools']), 'checks': ['initialize', 'notification', 'tools/list', 'UTF-8 roundtrip', 'HTTP bridge', 'invalid tool', 'ping']}, indent=2), encoding='utf-8')
    finally:
        server.shutdown(); server.server_close()
    print(f'Frozen GUI and MCP verified: {bundle}')


if __name__ == '__main__':
    verify(sys.argv[1])
