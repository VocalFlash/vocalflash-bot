import importlib.util
import os
import sys
import types
import unittest

from multivocale_runtime_v2 import PersistentMultivocaleRuntime

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
    def tearDown(self):
        os.environ.pop("VOCALFLASH_ASSISTANT_API_KEY", None)
        os.environ.pop("VERCEL_AUTOMATION_BYPASS_SECRET", None)

    def test_multivocale_commands_are_detected(self):
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
            {"phone_id": "p1", "api_key": "transcription-key"},
            assistant_url="",
        )
        self.assertEqual(result["status"], "DISABLED")

    def test_missing_assistant_key_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "ASSISTANT_API_KEY_NOT_CONFIGURED"):
            bridge.forward_customer_text(
                {
                    "from": "393331234567",
                    "id": "m-key",
                    "text": {"body": "ciao"},
                },
                {"phone_id": "p1", "api_key": "transcription-key"},
                assistant_url="https://assistant.example/api/v1/assistant",
            )

    def test_payload_uses_dedicated_assistant_key(self):
        req = FakeRequests()
        result = bridge.forward_customer_text(
            {
                "from": "393331234567",
                "id": "m2",
                "timestamp": "1790980000",
                "text": {"body": "Ho una perdita"},
            },
            {"phone_id": "pnid", "api_key": "transcription-key"},
            assistant_url="https://assistant.example/api/v1/assistant",
            assistant_api_key="assistant-secret",
            requests_module=req,
        )
        self.assertTrue(result["ok"])
        headers = req.call[1]["headers"]
        self.assertEqual(
            headers["X-VocalFlash-Assistant-Key"],
            "assistant-secret",
        )
        self.assertNotIn("X-API-Key", headers)
        self.assertNotIn("transcription-key", headers.values())
        payload = req.call[1]["json"]
        self.assertEqual(payload["external_account_id"], "pnid")
        self.assertEqual(payload["external_message_id"], "m2")
        self.assertEqual(payload["normalized_text"], "Ho una perdita")
        self.assertTrue(payload["occurred_at"].endswith("+00:00"))

    def test_vercel_bypass_is_optional_and_separate(self):
        req = FakeRequests()
        bridge.forward_customer_text(
            {
                "from": "393331234567",
                "id": "m-bypass",
                "text": {"body": "ciao"},
            },
            {"phone_id": "pnid", "api_key": "transcription-key"},
            assistant_url="https://assistant.example/api/v1/assistant",
            assistant_api_key="assistant-secret",
            vercel_bypass_secret="vercel-bypass",
            requests_module=req,
        )
        headers = req.call[1]["headers"]
        self.assertEqual(
            headers["x-vercel-protection-bypass"],
            "vercel-bypass",
        )
        self.assertEqual(
            headers["X-VocalFlash-Assistant-Key"],
            "assistant-secret",
        )
        self.assertNotIn("transcription-key", headers.values())


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
                self.values = {}

            def delete(self, key):
                self.deleted.append(key)
                self.values.pop(key, None)

            def set(self, key, value, nx=False, ex=None):
                if nx and key in self.values:
                    return False
                self.values[key] = value
                return True

        self.redis = Redis()
        self.calls = []
        fake = types.ModuleType("app")
        fake.app = object()
        fake.state_condition = Condition()
        fake.seen_messages = {}
        fake.SESSION_TTL = 1200
        fake.MAX_FILES = 5
        fake.MAX_TOTAL_BYTES = 25 * 1024 * 1024
        fake.AUTO_BATCH_SECONDS = 5.0
        fake.MULTI_BUTTONS = []
        fake.MULTI_START_BUTTONS = []
        fake.log = lambda value: self.calls.append(("log", value))
        fake.send_whatsapp_message = lambda *args: self.calls.append(("send", args))
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
        os.environ.pop("VOCALFLASH_ASSISTANT_API_KEY", None)
        os.environ.pop("VERCEL_AUTOMATION_BYPASS_SECRET", None)
        sys.modules.pop("app", None)

    def test_audio_and_multivocale_route_to_v2(self):
        os.environ["VOCALFLASH_ASSISTANT_API_URL"] = "https://assistant.example"
        seen = []
        self.module._multivocale_runtime.handle_message = (
            lambda message, config: seen.append(message.get("type")) or True
        )
        config = {"redis_url": "r", "phone_id": "p", "api_key": "k"}
        self.module.handle_message_with_customer_assistant(
            {"type": "text", "from": "1", "id": "a", "text": {"body": "MULTI"}},
            config,
        )
        self.module.handle_message_with_customer_assistant(
            {"type": "audio", "from": "1", "id": "b", "audio": {"id": "x"}},
            config,
        )
        self.assertEqual(seen, ["text", "audio"])
        legacy_calls = [item for item in self.calls if item[0] == "legacy"]
        self.assertEqual(legacy_calls, [])

    def test_unhandled_audio_does_not_fall_back_to_legacy_handler(self):
        self.module._multivocale_runtime.handle_message = lambda message, config: False
        self.module.handle_message_with_customer_assistant(
            {"type": "audio", "from": "1", "id": "audio-unhandled", "audio": {"id": "x"}},
            {"redis_url": "r", "phone_id": "p", "api_key": "k"},
        )
        legacy_calls = [item for item in self.calls if item[0] == "legacy"]
        self.assertEqual(legacy_calls, [])
        self.assertTrue(
            any("fallback legacy bloccato" in item[1] for item in self.calls if item[0] == "log")
        )

    def test_unhandled_multivocale_command_does_not_fall_back_to_legacy(self):
        self.module._multivocale_runtime.handle_message = lambda message, config: False
        self.module.handle_message_with_customer_assistant(
            {"type": "text", "from": "1", "id": "multi-unhandled", "text": {"body": "MULTI"}},
            {"redis_url": "r", "phone_id": "p", "api_key": "k"},
        )
        legacy_calls = [item for item in self.calls if item[0] == "legacy"]
        self.assertEqual(legacy_calls, [])

    def test_customer_text_uses_assistant(self):
        os.environ["VOCALFLASH_ASSISTANT_API_URL"] = "https://assistant.example"
        os.environ["VOCALFLASH_ASSISTANT_API_KEY"] = "assistant-secret"
        self.module.forward_customer_text = lambda *args, **kwargs: {
            "ok": True,
            "routing": {"replayed": False},
            "assistant_message": "Ho ricevuto la tua richiesta.",
        }
        self.module.handle_message_with_customer_assistant(
            {
                "type": "text",
                "from": "393331234567",
                "id": "m3",
                "text": {"body": "Ho una perdita"},
            },
            {"redis_url": "r", "phone_id": "pnid", "wa_token": "wa-secret", "api_key": "k"},
        )
        self.assertFalse(any(item[0] == "legacy" for item in self.calls))
        self.assertTrue(any(item[0] == "cleanup" for item in self.calls))
        sent = [item for item in self.calls if item[0] == "send"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1][2], "393331234567")
        self.assertEqual(sent[0][1][3], "Ho ricevuto la tua richiesta.")

    def test_missing_customer_assistant_reply_fails_closed(self):
        os.environ["VOCALFLASH_ASSISTANT_API_URL"] = "https://assistant.example"
        self.module.forward_customer_text = lambda *args, **kwargs: {
            "ok": True,
            "routing": {"replayed": False},
        }
        with self.assertRaisesRegex(RuntimeError, "ASSISTANT_REPLY_MISSING"):
            self.module.handle_message_with_customer_assistant(
                {
                    "type": "text",
                    "from": "393331234567",
                    "id": "m-no-reply",
                    "text": {"body": "Ho una perdita"},
                },
                {"redis_url": "r", "phone_id": "pnid", "wa_token": "wa-secret", "api_key": "k"},
            )
        self.assertFalse(any(item[0] == "send" for item in self.calls))
        self.assertTrue(any(item[0] == "cleanup" for item in self.calls))
        self.assertIn("vf:seen:m-no-reply", self.redis.deleted)
        self.assertNotIn("m-no-reply", self.fake.seen_messages)

    def test_customer_reply_is_not_sent_twice_for_same_message(self):
        config = {"redis_url": "r", "phone_id": "pnid", "wa_token": "wa-secret"}
        first = self.module._send_customer_reply_once(
            "same-message", "393331234567", "Risposta", config
        )
        second = self.module._send_customer_reply_once(
            "same-message", "393331234567", "Risposta", config
        )
        sent = [item for item in self.calls if item[0] == "send"]
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(sent), 1)
        self.assertEqual(
            self.redis.values["vf:assistant-reply-attempt:pnid:same-message"],
            "sent",
        )

    def test_customer_reply_attempts_are_isolated_by_runtime_namespace(self):
        self.fake.redis_namespace = lambda config=None: config["namespace"]
        config_a = {
            "redis_url": "r", "phone_id": "pnid", "wa_token": "wa-secret",
            "namespace": "staging-scope",
        }
        config_b = {
            "redis_url": "r", "phone_id": "pnid", "wa_token": "wa-secret",
            "namespace": "production-scope",
        }
        first = self.module._send_customer_reply_once(
            "same-message", "393331234567", "Risposta", config_a
        )
        second = self.module._send_customer_reply_once(
            "same-message", "393331234567", "Risposta", config_b
        )
        sent = [item for item in self.calls if item[0] == "send"]
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(len(sent), 2)
        self.assertIn("vf:assistant-reply-attempt:staging-scope:pnid:same-message", self.redis.values)
        self.assertIn("vf:assistant-reply-attempt:production-scope:pnid:same-message", self.redis.values)

    def test_ambiguous_send_failure_keeps_idempotency_marker(self):
        config = {"redis_url": "r", "phone_id": "pnid", "wa_token": "wa-secret"}
        self.fake.send_whatsapp_message = lambda *args: (_ for _ in ()).throw(
            TimeoutError("ambiguous")
        )
        with self.assertRaisesRegex(TimeoutError, "ambiguous"):
            self.module._send_customer_reply_once(
                "timeout-message", "393331234567", "Risposta", config
            )
        self.assertEqual(
            self.redis.values["vf:assistant-reply-attempt:pnid:timeout-message"],
            "attempted",
        )
        self.fake.send_whatsapp_message = lambda *args: self.calls.append(("send", args))
        self.assertFalse(
            self.module._send_customer_reply_once(
                "timeout-message", "393331234567", "Risposta", config
            )
        )
        self.assertFalse(any(item[0] == "send" for item in self.calls))

    def test_failure_releases_dedup_for_meta_retry(self):
        os.environ["VOCALFLASH_ASSISTANT_API_URL"] = "https://assistant.example"
        os.environ["VOCALFLASH_ASSISTANT_API_KEY"] = "assistant-secret"
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


class MultivocaleConcurrencyTests(unittest.TestCase):
    def test_concurrent_queued_batch_creation_reuses_single_batch(self):
        import json
        import threading

        class FakeLock:
            def __init__(self, lock):
                self.lock = lock

            def acquire(self, blocking=True):
                return self.lock.acquire(blocking=blocking)

            def release(self):
                self.lock.release()

        class FakeRedis:
            def __init__(self):
                self.values = {}
                self.locks = {}
                self.guard = threading.Lock()

            def lock(self, name, timeout=None, blocking_timeout=None):
                with self.guard:
                    lock = self.locks.setdefault(name, threading.Lock())
                return FakeLock(lock)

            def get(self, key):
                return self.values.get(key)

            def set(self, key, value, ex=None, nx=False):
                if nx and key in self.values:
                    return False
                self.values[key] = value
                return True

            def delete(self, key):
                self.values.pop(key, None)

        class FakeStore:
            ttl_seconds = 1200

            def __init__(self, redis_client):
                self.redis = redis_client

            @staticmethod
            def now_ms():
                return 1000

            @staticmethod
            def batch_key(batch_id):
                return f"vf:mv:v2:batch:{batch_id}"

            @staticmethod
            def _encode(batch):
                return json.dumps(batch)

            def get_batch(self, batch_id):
                value = self.redis.get(self.batch_key(batch_id))
                return json.loads(value) if value else None

        runtime = PersistentMultivocaleRuntime(object())
        fake_redis = FakeRedis()
        runtime._redis = lambda config: fake_redis
        runtime._store = lambda config: FakeStore(fake_redis)
        results = []
        errors = []

        def create():
            try:
                results.append(runtime._create_queued_batch({}, "sender-1"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=create) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        batch_ids = {item["batch_id"] for item in results}
        self.assertEqual(len(batch_ids), 1)
        queued_id = fake_redis.get("vf:mv:v2:sender:sender-1:queued")
        self.assertEqual(queued_id, next(iter(batch_ids)))


if __name__ == "__main__":
    unittest.main()
