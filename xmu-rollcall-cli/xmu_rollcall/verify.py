import uuid
import math
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
    try:
        return _send_radar(in_session, rollcall_id)
    except (requests.RequestException, ValueError, TypeError, OverflowError, ZeroDivisionError):
        print("Failed to submit radar answer: invalid response or request failure.")
        return False


def _send_radar(in_session, rollcall_id):
    url = f"{base_url}/api/rollcall/{rollcall_id}/answer"

    lat_1, lat_2 = 24.3, 24.6
    lon_1, lon_2 = 118.0, 118.2

    def payload(lat, lon):
        return {
            "accuracy": 35,
            "altitude": 0,
            "altitudeAccuracy": None,
            "deviceId": str(uuid.uuid4()),
            "heading": None,
            "latitude": lat,
            "longitude": lon,
            "speed": None
        }

    res_1 = in_session.put(url, json=payload(lat_1, lon_1), headers=headers, timeout=REQUEST_TIMEOUT)
    if res_1.status_code == 200:
        return True
    data_1 = res_1.json()
    distance_1 = _distance(data_1)

    res_2 = in_session.put(url, json=payload(lat_2, lon_2), headers=headers, timeout=REQUEST_TIMEOUT)
    if res_2.status_code == 200:
        return True
    data_2 = res_2.json()
    distance_2 = _distance(data_2)

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

    resolutions = solve_two_points(lat_1, lon_1, lat_2, lon_2, distance_1, distance_2)
    if resolutions:
        ((sol_x_1, sol_y_1), (sol_x_2, sol_y_2)) = resolutions
    else:
        return False

    payload_1 = payload(sol_x_1, sol_y_1)
    payload_2 = payload(sol_x_2, sol_y_2)

    res_3 = in_session.put(url, json=payload_1, headers=headers, timeout=REQUEST_TIMEOUT)
    if res_3.status_code == 200:
        return True
    else:
        res_4 = in_session.put(url, json=payload_2, headers=headers, timeout=REQUEST_TIMEOUT)
        if res_4.status_code == 200:
            return True

    return False


def _distance(data):
    value = data.get("distance") if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("missing radar distance")
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid radar distance")
    return value
