"""Local browser-test fixture; never imports user configuration or contacts school."""
import argparse
from pathlib import Path
from xmu_rollcall.qr_server import QRServer

parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, default=8765)
args = parser.parse_args()
server = QRServer()
server.start = lambda: None
server.public_base_url = f'http://127.0.0.1:{args.port}'

@server._app.get('/')
def preview():
    return Path(__file__).resolve().parents[1].joinpath('artifacts/scanner-preview.html').read_text()

@server._app.get('/test-session')
def test_session():
    item = server.create_session(600, '123', lambda payload, **kw: 'failed' if payload and payload.get('data') == 'synthetic-rejected' else 'success')
    return {'sid':item.sid}

server._app.run(host='127.0.0.1', port=args.port, debug=False)
