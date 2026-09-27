"""Bounded scan sessions. Receiving a QR is not the same as completing rollcall."""
import hashlib
import json
import logging
import math
import re
import secrets
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request
from pyngrok import ngrok
from werkzeug.serving import WSGIRequestHandler, make_server

from .qr_handler import parse_qr_code_text

LOG = logging.getLogger(__name__)
ASSETS = Path(__file__).parent / "static"
MAX_SESSIONS = 64
RETENTION = 600
MESSAGES = {
    "waiting": "请扫描本次签到二维码",
    "submitting": "已识别，正在向学校确认签到",
    "verifying": "正在核对学校签到记录",
    "success": "签到成功，学校已确认",
    "failed": "签到未完成，请重新扫描最新二维码",
    "uncertain": "暂时无法确认签到结果，请核对结果，勿重复提交",
    "expired": "此签到链接已过期，请使用新的通知链接",
}


class PrivateRequestHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        # Session URLs are bearer capabilities; never write them to access logs.
        path = re.sub(r"/(scan|submit|status|verify|events)/[^/? ]+", r"/\1/<session>", self.path)
        self.log("info", '"%s %s" %s %s', self.command, path.split("?")[0], code, size)


@dataclass
class QRScanSession:
    sid: str
    url: str
    timeout: int
    expected_id: str
    callback: object
    key: object
    expires: float
    state: str = "waiting"
    attempts: dict = field(default_factory=dict)
    request_id: str = ""
    retryable: bool = False
    created: bool = True
    events: int = 0
    last_verify: float = 0
    notice_attempts: int = 0
    next_notice: float = 0
    notice_sent: bool = False


class QRServer:
    def __init__(self, port=5001, ngrok_token="", host="127.0.0.1", public_base_url=""):
        self.port, self.host, self.ngrok_token = int(port), host, ngrok_token
        self.public_base_url = public_base_url.rstrip("/")
        parsed = urlparse(self.public_base_url)
        if self.public_base_url and (parsed.scheme != "https" or not parsed.hostname or
                                     parsed.username or parsed.password or parsed.path not in ("", "/") or
                                     parsed.query or parsed.fragment):
            raise ValueError("QR public base URL must be an HTTPS origin without path or credentials")
        self._lock = threading.RLock()
        self._started = False
        self._sessions, self._keys = {}, {}
        self._http_server = self._thread = None
        self._workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="qr-submit")
        self._stop = threading.Event()
        self._app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"), static_folder=None)
        self._app.config["MAX_CONTENT_LENGTH"] = 20 * 1024
        self._register_routes()

    def start(self):
        with self._lock:
            if self._started:
                return
            if not self.public_base_url and not self.ngrok_token:
                raise RuntimeError("Configure qr.public_base_url or qr.ngrok_token")
            try:
                self._stop.clear()
                self._http_server = make_server(self.host, self.port, self._app, threaded=True,
                                                request_handler=PrivateRequestHandler)
                self._thread = threading.Thread(target=self._http_server.serve_forever, daemon=True)
                self._thread.start()
                self._wait_for_local_server()
                if not self.public_base_url:
                    ngrok.set_auth_token(self.ngrok_token)
                    tunnel = ngrok.connect(str(self.port))
                    self.public_base_url = tunnel.public_url.rstrip("/")
                    if not self.public_base_url.startswith("https://"):
                        raise RuntimeError("Scanner requires an HTTPS public endpoint")
                self._started = True
                threading.Thread(target=self._reaper, name="qr-session-cleanup", daemon=True).start()
            except Exception:
                self._shutdown_local_server()
                raise

    def create_session(self, timeout, expected_id, callback, key=None):
        self.start()
        timeout = max(30, min(int(timeout), 600))
        with self._lock:
            self._cleanup()
            existing = self._sessions.get(self._keys.get(key)) if key is not None else None
            if existing and existing.state != "expired":
                existing.created = False
                return existing
            if len(self._sessions) >= MAX_SESSIONS:
                raise RuntimeError("Scanner capacity reached; retry shortly")
            sid = uuid.uuid4().hex
            item = QRScanSession(sid, f"{self.public_base_url}/scan/{sid}", timeout,
                                 str(expected_id), callback, key, time.monotonic() + timeout)
            self._sessions[sid] = item
            if key is not None:
                self._keys[key] = sid
            return item

    def claim_notice(self, item):
        with self._lock:
            now = time.monotonic()
            if item.notice_sent or item.notice_attempts >= 3 or now < item.next_notice or item.state != "waiting":
                return False
            item.notice_attempts += 1
            item.next_notice = now + 30
            return True

    def notice_delivered(self, item):
        with self._lock:
            item.notice_sent = True

    def _cleanup(self):
        now = time.monotonic()
        for sid, item in list(self._sessions.items()):
            if now >= item.expires and item.state in {"waiting", "failed"}:
                item.state, item.retryable = "expired", False
            if now >= item.expires + RETENTION and item.state not in {"submitting", "verifying"}:
                del self._sessions[sid]
                if self._keys.get(item.key) == sid:
                    del self._keys[item.key]

    def _reaper(self):
        while not self._stop.wait(5):
            with self._lock:
                self._cleanup()

    def _view(self, item):
        return {"ok": True, "state": item.state, "message": MESSAGES[item.state],
                "retryable": item.retryable, "request_id": item.request_id,
                "expires_in": max(0, math.ceil(item.expires - time.monotonic()))}

    def _lookup(self, sid):
        self._cleanup()
        return self._sessions.get(sid)

    def _register_routes(self):
        @self._app.after_request
        def protect(response):
            response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                     "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"})
            return response

        @self._app.errorhandler(413)
        def too_large(_):
            return jsonify(ok=False, message="请求过大，请重新选择二维码", code="too_large"), 413

        @self._app.before_request
        def check_origin():
            if request.method != "POST":
                return None
            origin = request.headers.get("Origin")
            allowed = {request.host_url.rstrip("/"), self.public_base_url}
            if origin and origin not in allowed:
                return jsonify(ok=False, message="请求来源无效"), 403
            if not request.is_json:
                return jsonify(ok=False, message="请使用 JSON 请求"), 415

        @self._app.get("/scan/<sid>")
        def scan_page(sid):
            with self._lock:
                item = self._lookup(sid)
                if not item:
                    return "签到链接不存在或已过期，请打开最新通知。", 404
                config = {"sid": sid, **self._view(item)}
            html, nonce = self.render_scanner(config)
            response = self._app.make_response(html)
            response.headers["Content-Security-Policy"] = (
                f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "img-src 'self' blob: data:; media-src blob:; connect-src 'self'; worker-src blob:; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            return response

        @self._app.get("/status/<sid>")
        def status(sid):
            with self._lock:
                item = self._lookup(sid)
                return (jsonify(self._view(item)) if item else
                        (jsonify(ok=False, state="expired", message=MESSAGES["expired"]), 404))

        @self._app.post("/submit/<sid>")
        def submit(sid):
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify(ok=False, message="请求格式无效"), 400
            text, rid = data.get("text"), data.get("request_id")
            if not isinstance(text, str) or not 1 <= len(text) <= 8192:
                return jsonify(ok=False, message="二维码内容无效，请重新扫描"), 400
            if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", rid):
                return jsonify(ok=False, message="缺少有效请求标识"), 400
            digest = hashlib.sha256(text.encode()).hexdigest()
            with self._lock:
                item = self._lookup(sid)
                if not item:
                    return jsonify(ok=False, state="expired", message=MESSAGES["expired"]), 404
                if rid in item.attempts:
                    if item.attempts[rid] != digest:
                        return jsonify(ok=False, message="请重新扫描并创建新的请求"), 409
                    return jsonify(self._view(item))
                if item.state in {"submitting", "verifying", "success", "uncertain"}:
                    return jsonify(self._view(item)), 409
                if item.state == "expired":
                    return jsonify(self._view(item)), 410
                if len(item.attempts) >= 8:
                    return jsonify(ok=False, message="尝试次数过多，请使用新的签到链接"), 429
                try:
                    payload = parse_qr_code_text(text)
                    if str(payload["rollcallId"]) != item.expected_id:
                        raise ValueError("wrong rollcall")
                except (ValueError, TypeError, KeyError):
                    return jsonify(ok=False, message="这不是本次签到二维码，请扫描老师展示的最新二维码"), 422
                item.attempts[rid] = digest
                item.request_id, item.state, item.retryable = rid, "submitting", False
                self._workers.submit(self._run, item, payload, False)
                return jsonify(self._view(item)), 202

        @self._app.post("/verify/<sid>")
        def verify(sid):
            with self._lock:
                item = self._lookup(sid)
                if not item:
                    return jsonify(ok=False, state="expired", message=MESSAGES["expired"]), 404
                now = time.monotonic()
                if item.state == "uncertain" and now - item.last_verify >= 5:
                    item.last_verify, item.state = now, "verifying"
                    self._workers.submit(self._run, item, None, True)
                return jsonify(self._view(item)), 202

        @self._app.post("/events/<sid>")
        def events(sid):
            data = request.get_json(silent=True)
            allowed = {"ready", "decoder_error", "camera_error", "photo_error", "photo_miss",
                       "photo_decoded", "submit_error", "zoom_error", "lens_error", "runtime_error"}
            if not isinstance(data, dict) or not isinstance(data.get("event"), str) or data["event"] not in allowed:
                return jsonify(ok=False), 400
            with self._lock:
                item = self._lookup(sid)
                if not item:
                    return jsonify(ok=False), 404
                if item.events >= 30:
                    return jsonify(ok=False), 429
                item.events += 1
                safe = {"event": data["event"], "version": "scan-v2"}
                for key in ("width", "height", "elapsed_ms"):
                    value = data.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100000:
                        safe[key] = round(value)
                LOG.info("QR event %s %s", hashlib.sha256(sid.encode()).hexdigest()[:12], json.dumps(safe))
            return jsonify(ok=True)

        @self._app.get("/healthz")
        def health():
            return jsonify(ok=True, version="scan-v2")

    def _run(self, item, payload, verify_only):
        with self._lock:
            if not verify_only and time.monotonic() >= item.expires:
                item.state, item.retryable = "expired", False
                return
        try:
            result = item.callback(payload, verify_only=verify_only)
            state = result if result in {"success", "failed", "uncertain"} else "uncertain"
        except Exception:
            # Exception strings can contain URLs or QR tokens; log category only.
            LOG.warning("QR upstream operation failed", exc_info=False)
            state = "uncertain"
        with self._lock:
            item.state = state
            item.retryable = state == "failed" and time.monotonic() < item.expires
            if state == "failed" and not item.retryable:
                item.state = "expired"

    def render_scanner(self, config, mock=False):
        nonce = secrets.token_urlsafe(24)
        assets = {name: (ASSETS / path).read_text(encoding="utf-8").replace("</script", "<\\/script")
                  for name, path in {"decoder_js": "jsQR.js", "scanner_js": "scanner.js",
                                     "worker_js": "qr-worker.js", "scanner_css": "scanner.css"}.items()}
        return render_template("scan.html", boot={**config, "mock": mock}, nonce=nonce, **assets), nonce

    def _wait_for_local_server(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("Scanner did not start")

    def _shutdown_local_server(self):
        self._stop.set()
        if self._http_server:
            self._http_server.shutdown()
            self._http_server.server_close()
        self._http_server = self._thread = None
