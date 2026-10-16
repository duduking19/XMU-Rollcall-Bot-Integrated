import uuid
import math
import json
import threading
import requests

from .attendance_guard import REQUEST_TIMEOUT, valid_number_code

base_url = "https://lnt.xmu.edu.cn"
headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://ids.xmu.edu.cn/authserver/login",
}

# (latitude, longitude), in fallback order; retain the supplied four decimal places.
RADAR_FALLBACK_LOCATIONS = ((24.6082, 118.2953), (24.6087, 118.3090))

def send_code(in_session, rollcall_id, number_code):
    """Submit only the exact code selected by the attendance guard."""
    if not valid_number_code(number_code):
        print("Number code is unavailable; no answer submitted.")
        return False
    answer_url = f"{base_url}/api/rollcall/{rollcall_id}/answer_number_rollcall"
    payload = {"deviceId": str(uuid.uuid4()), "numberCode": number_code}
    try:
        response = in_session.put(
            answer_url, json=payload, headers=in_session.headers, timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 200:
            print("Number code rollcall answered successfully.")
            return True
        print(f"Failed to submit number code. Status: {response.status_code}")
    except requests.RequestException:
        print("Failed to submit number code: request failed or timed out.")
    return False


def send_radar(in_session, rollcall_id):
    diagnostic = {
        "rollcall_id": rollcall_id,
        "worker": threading.current_thread().name,
        "attempt": "probe_1", "phase": "request", "http_status": None,
    }
    try:
        if _send_radar(in_session, rollcall_id, diagnostic):
            return True
    except (requests.RequestException, ValueError, TypeError, OverflowError, ZeroDivisionError) as exc:
        _radar_exception(diagnostic, exc)

    diagnostic.update(attempt="fixed_1", phase="request", http_status=None)
    diagnostic.pop("json_type", None)
    diagnostic.pop("distance_type", None)
    _radar_log(diagnostic, "fallback_start", reason="distance_method_failed")
    for index, (latitude, longitude) in enumerate(RADAR_FALLBACK_LOCATIONS, 1):
        try:
            response = _radar_submit(
                in_session, rollcall_id, diagnostic,
                _radar_payload(latitude, longitude), f"fixed_{index}",
            )
            # Fixed locations do not depend on a distance or JSON response body.
            if response.status_code == 200:
                _radar_log(diagnostic, "success")
                return True
        except (requests.RequestException, ValueError, TypeError, OverflowError, ZeroDivisionError) as exc:
            _radar_exception(diagnostic, exc)
    _radar_log(diagnostic, "failed", reason="all_fixed_locations_rejected")
    return False


def _radar_log(diagnostic, event, **details):
    print("[Radar] " + json.dumps({**diagnostic, "event": event, **details}, ensure_ascii=True))


def _radar_exception(diagnostic, exc):
    reason = {
        "request": "request_failed", "json": "json_decode_failed",
        "distance": "distance_validation_failed", "geometry": "geometry_failed",
    }.get(diagnostic["phase"], "radar_failed")
    # Request/JSON exceptions can contain URLs, tokens or response bodies.
    if type(exc) is ValueError and exc.args in (
        ("missing radar distance",), ("invalid radar distance",),
    ):
        reason = exc.args[0]
    details = {"exception_type": type(exc).__name__, "reason": reason}
    if isinstance(exc, (json.JSONDecodeError, requests.exceptions.JSONDecodeError)):
        details.update(json_line=exc.lineno, json_column=exc.colno)
    _radar_log(diagnostic, "exception", **details)
    print("Failed to submit radar answer: invalid response or request failure.")


def _radar_payload(lat, lon):
    return {
        "accuracy": 35,
        "altitude": 0,
        "altitudeAccuracy": None,
        "deviceId": str(uuid.uuid4()),
        "heading": None,
        "latitude": lat,
        "longitude": lon,
        "speed": None,
    }


def _radar_submit(in_session, rollcall_id, diagnostic, data, attempt):
    diagnostic.update(attempt=attempt, phase="request", http_status=None)
    diagnostic.pop("json_type", None)
    diagnostic.pop("distance_type", None)
    response = in_session.put(
        f"{base_url}/api/rollcall/{rollcall_id}/answer",
        json=data, headers=headers, timeout=REQUEST_TIMEOUT,
    )
    diagnostic.update(phase="response", http_status=response.status_code)
    _radar_log(diagnostic, "http_response")
    return response


def _send_radar(in_session, rollcall_id, diagnostic):
    lat_1, lat_2 = 24.3, 24.6
    lon_1, lon_2 = 118.0, 118.2

    def submit(data, attempt):
        return _radar_submit(in_session, rollcall_id, diagnostic, data, attempt)

    def read_distance(response):
        diagnostic["phase"] = "json"
        data = response.json()
        diagnostic.update(
            phase="distance", json_type=type(data).__name__,
            distance_type=(type(data["distance"]).__name__ if "distance" in data else "missing")
            if isinstance(data, dict) else "unavailable",
        )
        return _distance(data)

    res_1 = submit(_radar_payload(lat_1, lon_1), "probe_1")
    if res_1.status_code == 200:
        return True
    distance_1 = read_distance(res_1)

    res_2 = submit(_radar_payload(lat_2, lon_2), "probe_2")
    if res_2.status_code == 200:
        return True
    distance_2 = read_distance(res_2)

    def latlon_to_xy(lat, lon, lat0, lon0):
        R = 6371000
        x = math.radians(lon - lon0) * R * math.cos(math.radians(lat0))
        y = math.radians(lat - lat0) * R
        return x, y

    def xy_to_latlon(x, y, lat0, lon0):
        R = 6371000
        lat = lat0 + math.degrees(y / R)
        lon = lon0 + math.degrees(x / (R * math.cos(math.radians(lat0))))
        return lat, lon

    def circle_intersections(x1, y1, d1, x2, y2, d2):
        D = math.hypot(x2 - x1, y2 - y1)

        if D == 0 or D > d1 + d2 or D < abs(d1 - d2):
            return None

        a = (d1**2 - d2**2 + D**2) / (2 * D)
        h = math.sqrt(max(0.0, d1**2 - a**2))

        xm = x1 + a * (x2 - x1) / D
        ym = y1 + a * (y2 - y1) / D

        rx = -(y2 - y1) * (h / D)
        ry = (x2 - x1) * (h / D)

        p1 = (xm + rx, ym + ry)
        p2 = (xm - rx, ym - ry)
        return p1, p2

    def solve_two_points(lat1, lon1, lat2, lon2, d1, d2):
        lat0 = (lat1 + lat2) / 2
        lon0 = (lon1 + lon2) / 2
        x1, y1 = latlon_to_xy(lat1, lon1, lat0, lon0)
        x2, y2 = latlon_to_xy(lat2, lon2, lat0, lon0)

        sols = circle_intersections(x1, y1, d1, x2, y2, d2)
        if sols is None:
            return None

        p1 = xy_to_latlon(sols[0][0], sols[0][1], lat0, lon0)
        p2 = xy_to_latlon(sols[1][0], sols[1][1], lat0, lon0)
        return p1, p2

    diagnostic.update(attempt="solve", phase="geometry", http_status=None)
    diagnostic.pop("json_type", None)
    diagnostic.pop("distance_type", None)
    resolutions = solve_two_points(lat_1, lon_1, lat_2, lon_2, distance_1, distance_2)
    if resolutions:
        ((sol_x_1, sol_y_1), (sol_x_2, sol_y_2)) = resolutions
    else:
        _radar_log(diagnostic, "failed", reason="no_circle_intersection")
        return False

    payload_1 = _radar_payload(sol_x_1, sol_y_1)
    payload_2 = _radar_payload(sol_x_2, sol_y_2)

    res_3 = submit(payload_1, "candidate_1")
    if res_3.status_code == 200:
        return True
    else:
        res_4 = submit(payload_2, "candidate_2")
        if res_4.status_code == 200:
            return True

    _radar_log(diagnostic, "failed", reason="all_candidates_rejected")
    return False


def _distance(data):
    value = data.get("distance") if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("missing radar distance")
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid radar distance")
    return value
