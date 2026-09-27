import json
import re
import threading
import time
import uuid
from urllib.parse import parse_qs, urlparse

import requests

from .config import get_qr_config, load_config
from .notifier import build_notifier


base_url = "https://lnt.xmu.edu.cn"

mobile_headers = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Mobile Safari/537.36 Edg/141.0.0.0"
    ),
    "Content-Type": "application/json",
}

_server = None
_server_lock = threading.Lock()

# QR payload parser adapted from:
# https://github.com/KrsMt-0113/XMU-Rollcall-bot_qrCode/blob/main/qrRollcall/parse_code.py
_TA = chr(30)
_EA = chr(31)
_NA = chr(26)
_RA = chr(16)
_IA = _NA + "1"
_OA = _NA + "0"


def to_base36(num):
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if num < 0:
        return "-" + to_base36(-num)
    if num < 36:
        return chars[num]
    result = ""
    while num:
        num, rem = divmod(num, 36)
        result = chars[rem] + result
    return result


_AA = {
    key: to_base36(i)
    for i, key in enumerate(
        [
            "courseId",
            "activityId",
            "activityType",
            "data",
            "rollcallId",
            "groupSetId",
            "accessCode",
            "action",
            "enableGroupRollcall",
            "createUser",
            "joinCourse",
        ]
    )
}
_UA = {
    key: _NA + to_base36(i + 2)
    for i, key in enumerate(["classroom-exam", "feedback", "vote"])
}
_CA = {value: key for key, value in _AA.items()}
_SA = {value: key for key, value in _UA.items()}


class QRConfigError(RuntimeError):
    pass


class QRParseError(ValueError):
    pass


def parse_sign_qr_code(text):
    result = {}
    if not text or not isinstance(text, str):
        return result

    for part in filter(None, text.split("!")):
        splitted = part.split("~", 1)
        if len(splitted) < 2:
            continue
        raw_key, raw_value = splitted[0], splitted[1]
        key = _CA.get(raw_key, raw_key)

        if raw_value.startswith(_NA):
            if raw_value == _IA:
                value = True
            elif raw_value != _OA:
                value = _SA.get(raw_value, raw_value)
            else:
                value = False
        elif raw_value.startswith(_RA):
            parts = raw_value[1:].split(".")
            try:
                nums = [int(item, 36) for item in parts]
            except Exception:
                nums = []
            if len(nums) > 1:
                value = float(f"{nums[0]}.{nums[1]}")
            elif nums:
                value = nums[0]
            else:
                value = raw_value
        else:
            value = raw_value.replace(_EA, "~").replace(_TA, "!")

        result[key] = value
    return result


def parse_qr_code_text(text):
    if not text or not isinstance(text, str):
        raise QRParseError("empty QR code text")

    raw_text = text.strip()
    if raw_text.startswith("{"):
        data = json.loads(raw_text)
    else:
        data = _parse_qr_url_or_payload(raw_text)

    if not isinstance(data, dict) or not data:
        raise QRParseError("QR code payload is not a JSON object")
    if "rollcallId" not in data:
        raise QRParseError("QR code payload does not contain rollcallId")
    if "data" not in data:
        raise QRParseError("QR code payload does not contain data")
    if isinstance(data["rollcallId"], bool) or not re.fullmatch(r"[0-9]{1,20}", str(data["rollcallId"])):
        raise QRParseError("invalid rollcallId")
    if not isinstance(data["data"], str) or not 1 <= len(data["data"]) <= 4096:
        raise QRParseError("invalid QR token")
    return data


def _parse_qr_url_or_payload(text):
    candidate = text
    if candidate.startswith("/"):
        candidate = base_url + candidate
    elif "/j?p=" in candidate and not candidate.startswith("http"):
        candidate = base_url + candidate

    if not candidate.startswith("http"):
        parsed_payload = parse_sign_qr_code(candidate)
        if parsed_payload:
            return parsed_payload
        return json.loads(candidate)

    try:
        parsed_url = urlparse(candidate)
    except Exception as exc:
        raise QRParseError(f"invalid QR code URL: {exc}") from exc

    if parsed_url.scheme != "https" or parsed_url.hostname != "lnt.xmu.edu.cn" or parsed_url.username or parsed_url.port not in (None, 443):
        raise QRParseError("unsupported QR code origin")
    if parsed_url.path not in {"/j", "/scanner-jumper"}:
        raise QRParseError("unsupported QR code URL path")

    query = parse_qs(parsed_url.query)
    encoded_json = query.get("_p", [None])[0]
    if encoded_json:
        return json.loads(encoded_json)

    payload = query.get("p", [""])[0]
    parsed_payload = parse_sign_qr_code(payload)
    if not parsed_payload:
        raise QRParseError("failed to parse QR code payload")
    return parsed_payload


def handle_qr_rollcall(session, rollcall):
    """Schedule once, then return immediately so account polling can continue."""
    config_data = load_config()
    qr_config = get_qr_config(config_data)
    if not qr_config.get("enabled"):
        return False
    notifier = build_notifier(config_data)
    # A requests.Session must not be shared between the polling thread and workers.
    cookies = session.cookies.copy()
    headers = dict(session.headers)
    notified = False

    def complete(payload, verify_only=False):
        nonlocal notified
        with requests.Session() as upstream:
            upstream.cookies.update(cookies)
            upstream.headers.update(headers)
            result = (verify_qr_rollcall(upstream, rollcall["rollcall_id"]) if verify_only else
                      submit_qr_rollcall(upstream, payload))
        if result == "success" and not notified:
            notified = True
            try:
                notifier.send_rollcall_success(rollcall, "QRcode")
            except Exception:
                # Confirmation already succeeded; notification must not change it.
                print("[QR] Success notification failed.")
        return result

    try:
        scanner = _get_server(qr_config)
        item = scanner.create_session(
            qr_config["session_timeout"], rollcall["rollcall_id"], complete,
            key=(rollcall.get("account_id", "default"), str(rollcall["rollcall_id"])),
        )
        if scanner.claim_notice(item):
            result = notifier.send_qr_link(rollcall, item.url, max(1, int(item.expires - time.monotonic())))
            if result.ok:
                scanner.notice_delivered(item)
            else:
                print("[QR] Scan notification failed; monitoring continues.")
        return None  # Pending. The worker, not this caller, sends the success notice.
    except Exception:
        print("[QR] Could not prepare scanner; monitoring will retry.")
        return False


def verify_qr_rollcall(session, rollcall_id):
    """Only a matching school attendance record constitutes confirmed success."""
    try:
        response = session.get(f"{base_url}/api/radar/rollcalls", headers=mobile_headers,
                               timeout=(5, 12), allow_redirects=False)
        if response.status_code != 200 or "application/json" not in response.headers.get("Content-Type", "").lower():
            return "uncertain"
        body = response.json()
        rows = body.get("rollcalls") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            return "uncertain"
        for row in rows:
            if isinstance(row, dict) and str(row.get("rollcall_id")) == str(rollcall_id):
                if row.get("status") == "on_call_fine":
                    return "success"
        return "uncertain"
    except (requests.RequestException, ValueError):
        return "uncertain"


def submit_qr_rollcall(session, payload):
    """Never automatically repeat a PUT whose outcome is unknown."""
    try:
        response = session.put(
            f"{base_url}/api/rollcall/{payload['rollcallId']}/answer_qr_rollcall",
            headers=mobile_headers, json={"data": payload["data"], "deviceId": str(uuid.uuid4())},
            timeout=(5, 12), allow_redirects=False,
        )
    except requests.RequestException:
        return verify_qr_rollcall(session, payload["rollcallId"])
    confirmed = verify_qr_rollcall(session, payload["rollcallId"])
    if confirmed == "success":
        return confirmed
    # A definite rejection can be retried with a fresh code. 2xx, redirects and
    # server errors may have committed; keep them uncertain until verified.
    if response.status_code in {400, 401, 403, 404, 409, 410, 422, 429}:
        return "failed"
    return "uncertain"


def _get_server(qr_config):
    global _server
    if _server is not None:
        _server.start()
        return _server

    with _server_lock:
        if _server is None:
            from .qr_server import QRServer

            _server = QRServer(
                port=qr_config["flask_port"],
                ngrok_token=qr_config["ngrok_token"],
                public_base_url=qr_config.get("public_base_url", ""),
            )
    _server.start()
    return _server
