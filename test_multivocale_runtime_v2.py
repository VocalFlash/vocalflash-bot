import unittest

from multivocale_runtime_v2 import (
    CLEANUP_KEY,
    DUE_KEY,
    PersistentMultivocaleRuntime,
)
from multivocale_state_v2 import MultivocaleStateStore


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.in_multi = False
        self.ops = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def watch(self, *keys):
        return None

    def unwatch(self):
        return None

    def multi(self):
        self.in_multi = True

    def get(self, key):
        if self.in_multi:
            self.ops.append(("get", key, None, None))
            return self
        return self.redis.get(key)

    def set(self, key, value, ex=None):
        if self.in_multi:
            self.ops.append(("set", key, value, ex))
            return self
        return self.redis.set(key, value, ex=ex)

    def delete(self, key):
        if self.in_multi:
            self.ops.append(("delete", key, None, None))
            return self
        return self.redis.delete(key)

    def execute(self):
        results = []
        for op, key, value, ex in self.ops:
            if op == "set":
                results.append(self.redis.set(key, value, ex=ex))
            elif op == "delete":
                results.append(self.redis.delete(key))
            elif op == "get":
                results.append(self.redis.get(key))
        self.ops = []
        self.in_multi = False
        return results


class FakeLock:
    def acquire(self, blocking=False):
        return True

    def release(self):
        return True


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.zsets = {}

    def pipeline(self):
        return FakePipeline(self)

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    def delete(self, key):
        return 1 if self.data.pop(key, None) is not None else 0

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def zrem(self, key, member):
        return 1 if self.zsets.setdefault(key, {}).pop(member, None) is not None else 0

    def zrangebyscore(self, key, minimum, maximum, start=0, num=None):
        items = [
            member
            for member, score in sorted(
                self.zsets.get(key, {}).items(),
                key=lambda item: item[1],
            )
            if float(minimum) <= score <= float(maximum)
        ]
        if num is None:
            return items[start:]
        return items[start:start + num]

    def lock(self, *args, **kwargs):
        return FakeLock()


class FakeStorage:
    def __init__(self):
        self.deleted = []
        self.downloads = {}

    def prepare_download(self, object_key):
        return "signed:" + object_key

    def download_bytes(self, signed_url):
        key = signed_url.split("signed:", 1)[1]
        return self.downloads.get(key, b"audio")

    def delete_object(self, object_key):
        self.deleted.append(object_key)
        return True


class FakeLegacy:
    SESSION_TTL = 1200
    MAX_FILES = 5
    MAX_TOTAL_BYTES = 25 * 1024 * 1024
    AUTO_BATCH_SECONDS = 5.0
    MULTI_BUTTONS = [("vf_finish", "Riepiloga ora")]
    MULTI_START_BUTTONS = [("vf_multi", "Riepiloga più vocali")]

    def __init__(self, redis):
        self.redis = redis
        self.sent = []
        self.logs = []
        self.processed = []

    def get_redis_client(self, _):
        return self.redis

    def collection_message(self, count):
        return f"count={count}"

    def safe_send(self, config, sender, text, buttons=None):
        self.sent.append((sender, text, buttons))
        return True

    def log(self, text):
        self.logs.append(text)

    def process_audio_with_vocalflash(self, audio_files, api_key):
        self.processed.append((audio_files, api_key))
        return "risposta"


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.legacy = FakeLegacy(self.redis)
        self.runtime = PersistentMultivocaleRuntime(self.legacy)
        self.runtime.ensure_coordinator = lambda config: None
        self.storage = FakeStorage()
        self.runtime._storage = lambda: self.storage
        self.config = {
            "redis_url": "redis://test",
            "api_key": "transcribe-key",
            "wa_token": "wa",
        }
        self.sender = "393331234567"

    def test_due_time_is_persisted_in_redis(self):
        batch = {
            "batch_id": "b1",
            "sender": self.sender,
            "mode": "automatic",
            "last_arrival_at_ms": 10000,
        }
        self.runtime._schedule(self.config, batch)
        self.assertEqual(self.redis.zsets[DUE_KEY]["b1"], 15000)

    def test_last_audio_round_trip_is_persistent(self):
        item = {
            "message_id": "m1",
            "object_key": "tmp/b1/a.ogg",
            "filename": "audio.ogg",
            "mime_type": "audio/ogg",
            "size_bytes": 123,
        }
        self.runtime._remember_last_audio(self.config, self.sender, item)
        self.assertEqual(self.runtime._get_last_audio(self.config, self.sender), item)
        self.assertEqual(len(self.redis.zsets[CLEANUP_KEY]), 1)
        self.runtime._clear_last_audio(self.config, self.sender)
        self.assertIsNone(self.runtime._get_last_audio(self.config, self.sender))

    def test_manual_activation_reuses_previous_single_audio(self):
        item = {
            "message_id": "m1",
            "object_key": "tmp/old/a.ogg",
            "filename": "audio.ogg",
            "mime_type": "audio/ogg",
            "size_bytes": 123,
        }
        self.runtime._remember_last_audio(self.config, self.sender, item)
        self.runtime.activate_manual(self.sender, self.config)
        store = MultivocaleStateStore(self.redis, ttl_seconds=1200, max_files=5)
        batch = store.get_active_batch(self.sender)
        self.assertEqual(batch["mode"], "manual")
        ready = [x for x in batch["files"] if x["status"] == "ready"]
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["object_key"], item["object_key"])
        self.assertIn("Ho conservato anche il primo vocale", self.legacy.sent[-1][1])

    def test_successful_single_auto_batch_preserves_object_for_multi(self):
        store = MultivocaleStateStore(self.redis, ttl_seconds=1200, max_files=5)
        store.create_or_get_active_batch(
            self.sender, "automatic", batch_id="b1", now_ms=1000
        )
        store.reserve_audio("b1", "m1", now_ms=1000)
        store.complete_audio(
            "b1", "m1", "tmp/b1/a.ogg", "audio.ogg", "audio/ogg", 100
        )
        self.storage.downloads["tmp/b1/a.ogg"] = b"abc"
        self.runtime._process_batch(self.config, "b1", quiet_seconds=0)
        self.assertEqual(self.legacy.processed[0][0][0][1], b"abc")
        self.assertNotIn("tmp/b1/a.ogg", self.storage.deleted)
        remembered = self.runtime._get_last_audio(self.config, self.sender)
        self.assertEqual(remembered["object_key"], "tmp/b1/a.ogg")
        self.assertIsNone(store.get_active_batch(self.sender))

    def test_successful_multi_batch_deletes_all_objects(self):
        store = MultivocaleStateStore(self.redis, ttl_seconds=1200, max_files=5)
        store.create_or_get_active_batch(
            self.sender, "automatic", batch_id="b2", now_ms=1000
        )
        for index in range(2):
            mid = f"m{index}"
            key = f"tmp/b2/{index}.ogg"
            store.reserve_audio("b2", mid, now_ms=1000 + index)
            store.complete_audio(
                "b2", mid, key, "audio.ogg", "audio/ogg", 100
            )
        self.runtime._process_batch(self.config, "b2", quiet_seconds=0)
        self.assertEqual(
            sorted(self.storage.deleted),
            ["tmp/b2/0.ogg", "tmp/b2/1.ogg"],
        )
        self.assertIsNone(self.runtime._get_last_audio(self.config, self.sender))

    def test_queued_batch_waits_for_promotion(self):
        store = MultivocaleStateStore(self.redis, ttl_seconds=1200, max_files=5)
        store.create_or_get_active_batch(
            self.sender, "automatic", batch_id="active", now_ms=1000
        )
        store.reserve_audio("active", "m0", now_ms=1000)
        store.complete_audio(
            "active", "m0", "tmp/active/a.ogg", "audio.ogg", "audio/ogg", 100
        )
        store.claim_processing(
            "active", quiet_seconds=0, lease_token="lease", now_ms=2000
        )

        queued = self.runtime._create_queued_batch(self.config, self.sender)
        store.reserve_audio(queued["batch_id"], "m1", now_ms=3000)
        queued, _ = store.complete_audio(
            queued["batch_id"],
            "m1",
            "tmp/queued/a.ogg",
            "audio.ogg",
            "audio/ogg",
            100,
        )
        self.runtime._schedule(self.config, queued)
        self.assertNotIn(queued["batch_id"], self.redis.zsets.get(DUE_KEY, {}))

        store.finish_batch("active", "lease", final_status="completed")
        promoted = self.runtime._promote_queued(self.config, self.sender)
        self.assertEqual(promoted["batch_id"], queued["batch_id"])
        self.assertIn(queued["batch_id"], self.redis.zsets[DUE_KEY])

    def test_expired_unreferenced_last_audio_is_deleted(self):
        item = {
            "message_id": "m1",
            "object_key": "tmp/old/orphan.ogg",
            "filename": "audio.ogg",
            "mime_type": "audio/ogg",
            "size_bytes": 123,
        }
        self.runtime._remember_last_audio(self.config, self.sender, item)
        self.runtime._clear_last_audio(self.config, self.sender)
        self.runtime._cleanup_due_objects(self.config, now_ms=10**15)
        self.assertIn(item["object_key"], self.storage.deleted)
        self.assertEqual(self.redis.zsets[CLEANUP_KEY], {})


if __name__ == "__main__":
    unittest.main()
