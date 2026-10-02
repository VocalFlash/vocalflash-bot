import importlib.util
import os
import sys
import types
import unittest

import customer_text_bridge as bridge


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True, "routing": {"replayed": False}}


class FakeRequests:
    def __init__(self):
        self.call = None

    def post(self, *args, **kwargs):
        self.call = (args, kwargs)
        return FakeResponse()


class CustomerTextBridgeTests(unittest.TestCase):
    def test_multivocale_commands_stay_legacy(self):
        self.assertTrue(bridge.is_multivocale_text_command("MULTI"))
        self.assertTrue(bridge.is_multivocale_text_command("annulla"))
        self.assertFalse(
            bridge.is_multivocale_text_command("Ho una perdita in cucina")
        )

    def test_disabled_without_assistant_url(self):
        result = bridge.forward_customer_text(
            {
                "from": "393331234567",
                "id": "m1",
                "text": {"body": "ciao"},
            },
            {"phone_id": "p1", "api_key": "k"},
            assistant_url="",
        )
        self.assertEqual(result["status"], "DISABLED")

    def test_payload_preserves_meta_identity_and_timestamp(self):
        req = FakeRequests()
        result = bridge.forward_customer_text(
            {
                "from": "393331234567",
                "id": "m2",
                "timestamp": "1790980000",
                "text": {"body": "Ho una perdita"},
            },
            {"phone_id": "pnid", "api_key": "secret"},
            assistant_url="https://assistant.example/api/v1/assistant",
            requests_module=req,
        )
        self.assertTrue(result["ok"])
        payload = req.call[1]["json"]
        self.assertEqual(payload["external_account_id"], "pnid")
        self.assertEqual(payload["external_message_id"], "m2")
        self.assertEqual(payload["normalized_text"], "Ho una perdita")
        self.assertTrue(payload["occurred_at"].endswith("+00:00"))


class WrapperTests(unittest.TestCase):
    def setUp(self):
        class Condition:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def notify_all(self):
                return None

        class Redis:
            def __init__(self):
                self.deleted = []

            def delete(self, key):
                self.deleted.append(key)

        self.redis = Redis()
        self.calls = []
        fake = types.ModuleType("app")
        fake.app = object()
        fake.state_condition = Condition()
        fake.seen_messages = {}
        fake.log = lambda value: self.calls.append(("log", value))
        fake.cleanup_expired = lambda: self.calls.append(("cleanup", None))
        fake.is_duplicate = lambda message_id, redis_url: False
        fake.get_redis_client = lambda redis_url: self.redis
        fake.handle_message = lambda message, config: self.calls.append(
            ("legacy", message.get("type"))
        )
        sys.modules["app"] = fake
        spec = importlib.util.spec_from_file_location(
            "app_customer_test",
            os.path.join(os.path.dirname(__file__), "app_customer.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.module = module
        self.fake = fake

    def tearDown(self):
        os.environ.pop("VOCALFLASH_ASSISTANT_API_URL", None)
        sys.modules.pop("app", None)

    def test_audio_and_multivocale_remain_legacy(self):
        os.environ["VOCALFLASH_ASSISTANT_API_URL"] = "https://assistant.example"
        config = {"redis_url": "r", "phone_id": "p", "api_key": "k"}
        self.module.handle_message_with_customer_assistant(
            {"type": "text", "from": "1", "id": "a", "text": {"body": "MULTI"}},
            config,
        )
        self.module.handle_message_with_customer_assistant(
            {"type": "audio", "from": "1", "id": "b", "audio": {"id": "x"}},
            config,
        )
        legacy_calls = [item for item in self.calls if item[0] == "legacy"]
        self.assertEqual(len(legacy_calls), 2)

    def test_customer_text_uses_assistant(self):
        os.environ["VOCALFLASH_ASSISTANT_API_URL"] = "https://assistant.example"
        self.module.forward_customer_text = lambda *args, **kwargs: {
            "ok": True,
            "routing": {"replayed": False},
        }
        self.module.handle_message_with_customer_assistant(
            {
                "type": "text",
                "from": "393331234567",
                "id": "m3",
                "text": {"body": "Ho una perdita"},
            },
            {"redis_url": "r", "phone_id": "pnid", "api_key": "k"},
        )
        self.assertFalse(any(item[0] == "legacy" for item in self.calls))
        self.assertTrue(any(item[0] == "cleanup" for item in self.calls))

    def test_failure_releases_dedup_for_meta_retry(self):
        os.environ["VOCALFLASH_ASSISTANT_API_URL"] = "https://assistant.example"
        self.fake.seen_messages["m4"] = 1

        def fail(*args, **kwargs):
            raise RuntimeError("boom")

        self.module.forward_customer_text = fail
        with self.assertRaises(RuntimeError):
            self.module.handle_message_with_customer_assistant(
                {
                    "type": "text",
                    "from": "393331234567",
                    "id": "m4",
                    "text": {"body": "Test errore"},
                },
                {"redis_url": "r", "phone_id": "pnid", "api_key": "k"},
            )
        self.assertIn("vf:seen:m4", self.redis.deleted)
        self.assertNotIn("m4", self.fake.seen_messages)


if __name__ == "__main__":
    unittest.main()
