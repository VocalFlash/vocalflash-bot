import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

import app
from meta_delivery_outcome_v1 import classify_meta_exception, classify_meta_response


class MetaDeliveryOutcomeTests(unittest.TestCase):
    def test_success_requires_wamid(self):
        result = classify_meta_response(200, {"messages": [{"id": "wamid.test-message-id"}]})
        self.assertEqual(result["outcome"], "accepted")
        self.assertEqual(result["provider_message_id"], "wamid.test-message-id")

    def test_success_without_wamid_is_unknown(self):
        self.assertEqual(classify_meta_response(200, {"messages": []})["outcome"], "outcome_unknown")

    def test_client_rejection_is_definitive(self):
        self.assertEqual(classify_meta_response(400)["outcome"], "definitive_rejection")
        self.assertEqual(classify_meta_response(429)["outcome"], "definitive_rejection")

    def test_timeout_and_server_errors_are_unknown(self):
        for status in (408, 500, 502, 503, 302):
            with self.subTest(status=status):
                self.assertEqual(classify_meta_response(status)["outcome"], "outcome_unknown")

    def test_exception_with_http_response_uses_status_classification(self):
        error = requests.HTTPError("provider rejected", response=SimpleNamespace(status_code=403))
        self.assertEqual(classify_meta_exception(error)["outcome"], "definitive_rejection")

    def test_transport_exception_is_unknown(self):
        self.assertEqual(classify_meta_exception(requests.Timeout("timeout"))["outcome"], "outcome_unknown")

    def test_app_wrapper_returns_accepted_only_with_wamid(self):
        response = SimpleNamespace(status_code=200, json=lambda: {"messages": [{"id": "wamid.wrapper-test"}]})
        with patch.object(app, "send_to_user", return_value=response):
            result = app.safe_send_outcome({}, "recipient", "message")
        self.assertEqual(result["outcome"], "accepted")
        self.assertEqual(result["provider_message_id"], "wamid.wrapper-test")

    def test_app_wrapper_preserves_definitive_http_rejection(self):
        error = requests.HTTPError("provider rejected", response=SimpleNamespace(status_code=400))
        with patch.object(app, "send_to_user", side_effect=error):
            result = app.safe_send_outcome({}, "recipient", "message")
        self.assertEqual(result["outcome"], "definitive_rejection")
        self.assertEqual(result["http_status"], 400)

    def test_app_wrapper_keeps_timeout_ambiguous(self):
        with patch.object(app, "send_to_user", side_effect=requests.Timeout("timeout")):
            result = app.safe_send_outcome({}, "recipient", "message")
        self.assertEqual(result["outcome"], "outcome_unknown")


if __name__ == "__main__":
    unittest.main()
