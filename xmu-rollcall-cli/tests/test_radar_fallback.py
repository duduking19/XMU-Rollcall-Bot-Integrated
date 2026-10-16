"""Fixed-location retries use mocked requests only, after the original Radar path."""

import contextlib
import io
import json
import threading
import unittest
from unittest.mock import MagicMock, patch

import requests

from attendance_fixtures import ACCOUNT, detected, radar_detail, response, session_for
from xmu_rollcall.attendance_guard import AttendanceGuard, REQUEST_TIMEOUT
from xmu_rollcall.rollcall_handler import handle_rollcalls
from xmu_rollcall.verify import headers, send_radar


FIXED_LOCATIONS = [(24.6082, 118.2953), (24.6087, 118.3090), (24.6075, 118.2950)]
SECRET = "private-fallback-cookie-and-response-secret"


def fixed_response(status):
    reply = response({"private": SECRET}, status)
    reply.text = SECRET
    reply.json.side_effect = AssertionError("Fixed-location responses must not be decoded")
    return reply


class RadarFallbackTest(unittest.TestCase):
    def run_radar(self, session):
        output = io.StringIO()
        thread = threading.current_thread()
        previous_name = thread.name
        try:
            thread.name = "xmu-account-3"
            with contextlib.redirect_stdout(output):
                result = send_radar(session, 123)
        finally:
            thread.name = previous_name
        raw = output.getvalue()
        self.assertNotIn(SECRET, raw)
        for coordinate in ("24.6082", "118.2953", "24.6087", "118.309", "24.6075", "118.295"):
            self.assertNotIn(coordinate, raw)
        events = [json.loads(line[len("[Radar] "):]) for line in raw.splitlines()
                  if line.startswith("[Radar] ")]
        for event in events:
            self.assertEqual(event["rollcall_id"], 123)
            self.assertEqual(event["worker"], "xmu-account-3")
            self.assertFalse({"latitude", "longitude", "payload", "response_body", "body", "data"} & set(event))
        session.get.assert_not_called()
        return result, events

    def assert_event(self, events, **expected):
        self.assertTrue(any(all(event.get(key) == value for key, value in expected.items())
                            for event in events), f"Missing event {expected}; got {events}")

    def assert_fixed_calls(self, session, original_count, count=3):
        self.assertEqual(session.put.call_count, original_count + count)
        for call, (latitude, longitude) in zip(session.put.call_args_list[original_count:], FIXED_LOCATIONS[:count]):
            self.assertEqual(call.args, ("https://lnt.xmu.edu.cn/api/rollcall/123/answer",))
            self.assertEqual(call.kwargs["headers"], headers)
            self.assertEqual(call.kwargs["timeout"], REQUEST_TIMEOUT)
            payload = call.kwargs["json"]
            self.assertEqual((payload["latitude"], payload["longitude"]), (latitude, longitude))
            self.assertEqual(round(payload["latitude"], 4), payload["latitude"])
            self.assertEqual(round(payload["longitude"], 4), payload["longitude"])
            self.assertEqual(set(payload), {"accuracy", "altitude", "altitudeAccuracy", "deviceId",
                                            "heading", "latitude", "longitude", "speed"})
            self.assertEqual({key: value for key, value in payload.items()
                              if key not in {"deviceId", "latitude", "longitude"}},
                             {"accuracy": 35, "altitude": 0, "altitudeAccuracy": None,
                              "heading": None, "speed": None})

    def test_original_failure_paths_all_reach_the_three_fixed_locations(self):
        non_json = response({}, 400)
        non_json.json.side_effect = json.JSONDecodeError(SECRET, SECRET, 0)
        scenarios = [
            ("missing_distance", [response({}, 400)]),
            ("invalid_distance", [response({"distance": -1}, 400)]),
            ("nan_distance", [response({"distance": float("nan")}, 400)]),
            ("non_json", [non_json]),
            ("second_probe_timeout", [response({"distance": 25000}, 400), requests.Timeout(SECRET)]),
            ("no_intersection", [response({"distance": 1}, 400), response({"distance": 1}, 400)]),
            ("candidate_rejections", [response({"distance": 25000}, 400), response({"distance": 25000}, 400),
                                      response({}, 403), response({}, 500)]),
        ]
        for name, original in scenarios:
            with self.subTest(scenario=name):
                fixed = [fixed_response(403), fixed_response(500), fixed_response(200)]
                session = MagicMock()
                session.put.side_effect = original + fixed
                result, events = self.run_radar(session)
                self.assertTrue(result)
                self.assert_fixed_calls(session, len(original))
                self.assertEqual(sum(event["event"] == "fallback_start" for event in events), 1)
                self.assert_event(events, event="http_response", attempt="fixed_1", http_status=403)
                self.assert_event(events, event="http_response", attempt="fixed_2", http_status=500)
                self.assert_event(events, event="success", attempt="fixed_3", http_status=200)
                self.assertFalse(any(event.get("reason") == "all_fixed_locations_rejected" for event in events))
                for reply in fixed:
                    reply.json.assert_not_called()

    def test_first_fixed_success_stops_before_any_later_fixed_request(self):
        fixed = fixed_response(200)
        session = MagicMock()
        session.put.side_effect = [response({}, 400), fixed]
        result, events = self.run_radar(session)
        self.assertTrue(result)
        self.assert_fixed_calls(session, 1, count=1)
        self.assert_event(events, event="success", attempt="fixed_1", http_status=200)
        self.assertFalse(any(event.get("attempt") in {"fixed_2", "fixed_3"} for event in events))
        fixed.json.assert_not_called()

    def test_second_fixed_success_stops_before_the_third_fixed_request(self):
        fixed = [fixed_response(403), fixed_response(200)]
        session = MagicMock()
        session.put.side_effect = [response({}, 400)] + fixed
        result, events = self.run_radar(session)
        self.assertTrue(result)
        self.assert_fixed_calls(session, 1, count=2)
        self.assert_event(events, event="success", attempt="fixed_2", http_status=200)
        self.assertFalse(any(event.get("attempt") == "fixed_3" for event in events))
        for reply in fixed:
            reply.json.assert_not_called()

    def test_first_fixed_exception_is_logged_and_second_fixed_is_still_attempted(self):
        for error in [requests.Timeout(SECRET), requests.ConnectionError(SECRET), ValueError(SECRET),
                      TypeError(SECRET), OverflowError(SECRET), ZeroDivisionError(SECRET)]:
            with self.subTest(exception_type=type(error).__name__):
                fixed = fixed_response(200)
                session = MagicMock()
                session.put.side_effect = [response({}, 400), error, fixed]
                result, events = self.run_radar(session)
                self.assertTrue(result)
                self.assert_fixed_calls(session, 1, count=2)
                self.assert_event(events, event="exception", attempt="fixed_1", phase="request",
                                  http_status=None, exception_type=type(error).__name__, reason="request_failed")
                self.assert_event(events, event="success", attempt="fixed_2", http_status=200)
                self.assertFalse(any(event.get("attempt") == "fixed_3" for event in events))
                fixed.json.assert_not_called()

    def test_second_fixed_exception_is_logged_and_third_fixed_is_still_attempted(self):
        for error in [requests.Timeout(SECRET), requests.ConnectionError(SECRET), ValueError(SECRET),
                      TypeError(SECRET), OverflowError(SECRET), ZeroDivisionError(SECRET)]:
            with self.subTest(exception_type=type(error).__name__):
                first, third = fixed_response(403), fixed_response(200)
                session = MagicMock()
                session.put.side_effect = [response({}, 400), first, error, third]
                result, events = self.run_radar(session)
                self.assertTrue(result)
                self.assert_fixed_calls(session, 1)
                self.assert_event(events, event="exception", attempt="fixed_2", phase="request",
                                  http_status=None, exception_type=type(error).__name__, reason="request_failed")
                self.assert_event(events, event="success", attempt="fixed_3", http_status=200)
                first.json.assert_not_called()
                third.json.assert_not_called()

    def test_all_fixed_rejections_or_exceptions_are_bounded_and_report_failure(self):
        for fixed in [
            [fixed_response(403), fixed_response(500), fixed_response(403)],
            [requests.Timeout(SECRET), requests.ConnectionError(SECRET), requests.Timeout(SECRET)],
            [fixed_response(500), requests.Timeout(SECRET), fixed_response(403)],
            [requests.Timeout(SECRET), fixed_response(403), requests.Timeout(SECRET)],
        ]:
            with self.subTest(fixed=fixed):
                session = MagicMock()
                session.put.side_effect = [response({}, 400)] + fixed
                result, events = self.run_radar(session)
                self.assertFalse(result)
                self.assert_fixed_calls(session, 1)
                self.assertEqual(sum(event["event"] == "fallback_start" for event in events), 1)
                self.assert_event(events, event="failed", attempt="fixed_3", reason="all_fixed_locations_rejected")
                for reply in fixed:
                    if not isinstance(reply, Exception):
                        reply.json.assert_not_called()

    def test_seven_requests_maximum_with_fresh_device_ids_after_candidate_failures(self):
        fixed = [fixed_response(403), fixed_response(500), fixed_response(403)]
        session = MagicMock()
        session.put.side_effect = [response({"distance": 25000}, 400), response({"distance": 25000}, 400),
                                   response({}, 403), response({}, 500)] + fixed
        ids = [f"device-{index}" for index in range(1, 8)]
        with patch("xmu_rollcall.verify.uuid.uuid4", side_effect=ids) as new_uuid:
            result, events = self.run_radar(session)
        self.assertFalse(result)
        self.assert_fixed_calls(session, 4)
        self.assertEqual(new_uuid.call_count, 7)
        self.assertEqual([call.kwargs["json"]["deviceId"] for call in session.put.call_args_list], ids)
        self.assertEqual([event["attempt"] for event in events if event["event"] == "http_response"],
                         ["probe_1", "probe_2", "candidate_1", "candidate_2", "fixed_1", "fixed_2", "fixed_3"])
        self.assert_event(events, event="failed", attempt="fixed_3", http_status=403,
                          reason="all_fixed_locations_rejected")
        for reply in fixed:
            reply.json.assert_not_called()

    def test_success_at_each_original_attempt_never_uses_fixed_locations(self):
        for successful_attempt in range(4):
            with self.subTest(successful_attempt=successful_attempt):
                replies = [response({"distance": 25000}, 400), response({"distance": 25000}, 400),
                           response({}, 403), response({}, 500)]
                replies[successful_attempt].status_code = 200
                replies[successful_attempt].json.side_effect = AssertionError("200 must not be decoded")
                session = MagicMock()
                session.put.side_effect = replies
                result, events = self.run_radar(session)
                self.assertTrue(result)
                self.assertEqual(session.put.call_count, successful_attempt + 1)
                self.assertFalse(any(event["event"] == "fallback_start" or
                                     str(event.get("attempt", "")).startswith("fixed_") for event in events))
                replies[successful_attempt].json.assert_not_called()

    def test_radar_below_attendance_threshold_never_submits_even_with_fallback(self):
        for attended, total in [(9, 20), (10, 34)]:
            with self.subTest(attended=attended, total=total):
                session = session_for([], radar_record=radar_detail(attended, total))
                notifier = MagicMock()
                with patch("xmu_rollcall.rollcall_handler.build_notifier", return_value=notifier), \
                        contextlib.redirect_stdout(io.StringIO()):
                    result = handle_rollcalls({"rollcalls": [detected(is_radar=True, is_number=False)]},
                                              session, ACCOUNT, AttendanceGuard([1]))
                self.assertEqual(result, [False])
                session.put.assert_not_called()
                notifier.send_rollcall_success.assert_not_called()


if __name__ == "__main__":
    unittest.main()
