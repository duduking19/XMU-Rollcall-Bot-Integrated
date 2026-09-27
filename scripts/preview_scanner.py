"""Render a self-contained mock with the production UI. No credentials/network."""
import argparse
import base64
from pathlib import Path

from xmu_rollcall.qr_server import QRServer

parser = argparse.ArgumentParser()
parser.add_argument('--output', default='artifacts/scanner-preview.html')
parser.add_argument('--qr-image')
args = parser.parse_args()
server = QRServer()
boot = {'sid': 'preview-only', 'state': 'waiting', 'expires_in': 300}
if args.qr_image:
    boot['mockQr'] = 'data:image/png;base64,' + base64.b64encode(Path(args.qr_image).read_bytes()).decode()
with server._app.app_context():
    html, _ = server.render_scanner(boot, mock=True)
path = Path(args.output); path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(html, encoding='utf-8')
server._workers.shutdown()
print(path.resolve())
