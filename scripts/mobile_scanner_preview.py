"""Temporary real-camera preview. Only synthetic attendance, no school credentials."""
import argparse
from pathlib import Path

from flask import redirect, send_file, request
from werkzeug.middleware.proxy_fix import ProxyFix
from xmu_rollcall.qr_server import QRServer

parser = argparse.ArgumentParser()
parser.add_argument('--origin', required=True)
parser.add_argument('--port', type=int, default=8768)
args = parser.parse_args()


class PreviewServer(QRServer):
    def render_scanner(self, config, mock=False):
        html, nonce = super().render_scanner(config, mock=False)
        html = html.replace('扫码签到', '扫码测试').replace('签到成功', '识别成功')
        html = html.replace('class="preview-badge" hidden>预览', 'class="preview-badge">测试')
        return html, nonce


server = PreviewServer(port=args.port, public_base_url=args.origin)
# Only the loopback tunnel proxy can connect to this listener.
server._app.wsgi_app = ProxyFix(server._app.wsgi_app, x_proto=1)


@server._app.get('/')
def entry():
    previous = request.cookies.get('preview_sid')
    with server._lock:
        item = server._lookup(previous) if previous else None
        if not item or item.state != 'waiting':
            try:
                item = server.create_session(600, '123', lambda payload, **kw: 'success')
            except RuntimeError:
                return '测试人数已满，请稍后再试。', 503
    response = redirect('/scan/' + item.sid)
    response.set_cookie('preview_sid', item.sid, secure=True, httponly=True, samesite='Lax', max_age=600)
    return response


@server._app.get('/sample.png')
def sample():
    return send_file(Path(__file__).resolve().parents[1] / 'artifacts/synthetic-qr.png', mimetype='image/png')


if __name__ == '__main__':
    server.start()
    server._thread.join()
