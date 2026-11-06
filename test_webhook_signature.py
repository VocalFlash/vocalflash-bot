import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import patch

import app as webhook_app


class MetaWebhookSignatureTests(unittest.TestCase):
    def setUp(self):
        self.client = webhook_app.app.test_client()
        self.secret = "test-meta-app-secret"
        os.environ["META_APP_SECRET"] = self.secret
        self.body = json.dumps({"entry": []}, separators=(",", ":")).encode()

    def tearDown(self):
        os.environ.pop("META_APP_SECRET", None)

    def signature(self, body):
        digest = hmac.new(
            self.secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        return "sha256=" + digest

    def test_rejects_missing_signature(self):
        response = self.client.post(
            "/whatsapp",
            data=self.body,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_rejects_invalid_signature(self):
        response = self.client.post(
            "/whatsapp",
            data=self.body,
            headers={"X-Hub-Signature-256": "sha256=invalid"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_rejects_tampered_body(self):
        changed_body = json.dumps({"entry": [{"unexpected": True}]}).encode()
        response = self.client.post(
            "/whatsapp",
            data=changed_body,
            headers={"X-Hub-Signature-256": self.signature(self.body)},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_fails_closed_when_app_secret_is_missing(self):
        os.environ.pop("META_APP_SECRET", None)
        response = self.client.post(
            "/whatsapp",
            data=self.body,
            headers={"X-Hub-Signature-256": self.signature(self.body)},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 503)

    def test_accepts_valid_signature_before_processing_payload(self):
        with patch.object(
            webhook_app,
            "get_config",
            return_value={
                "wa_token": "test-token",
                "phone_id": "test-phone-id",
                "api_key": "test-api-key",
                "redis_url": "redis://localhost:6379/0",
            },
        ), patch.object(webhook_app, "redis_healthcheck"):
            response = self.client.post(
                "/whatsapp",
                data=self.body,
                headers={"X-Hub-Signature-256": self.signature(self.body)},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
