import unittest

from multivocale_state_v2 import BatchBusy, MultivocaleStateStore


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
            self.ops.append(("get", key, None))
            return self
        return self.redis.get(key)

    def set(self, key, value, ex=None):
        if self.in_multi:
            self.ops.append(("set", key, value))
            return self
        return self.redis.set(key, value, ex=ex)

    def delete(self, key):
        if self.in_multi:
            self.ops.append(("delete", key, None))
            return self
        return self.redis.delete(key)

    def execute(self):
        results = []
        for op, key, value in self.ops:
            if op == "set":
                results.append(self.redis.set(key, value))
            elif op == "delete":
                results.append(self.redis.delete(key))
            elif op == "get":
                results.append(self.redis.get(key))
        self.ops = []
        self.in_multi = False
        return results


class FakeRedis:
    def __init__(self):
        self.data = {}

    def pipeline(self):
        return FakePipeline(self)

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self.data[key] = value
        return True

    def delete(self, key):
        return 1 if self.data.pop(key, None) is not None else 0


class MultivocaleStateTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.store = MultivocaleStateStore(
            self.redis,
            ttl_seconds=1200,
            max_files=5,
        )
        self.sender = "393331234567"
        self.batch_id = "11111111-1111-4111-8111-111111111111"
        self.batch, created = self.store.create_or_get_active_batch(
            self.sender,
            "automatic",
            batch_id=self.batch_id,
            now_ms=1000,
        )
        self.assertTrue(created)

    def _ready(self, message_id, order_time, suffix):
        _, reserved = self.store.reserve_audio(
            self.batch_id,
            message_id,
            now_ms=order_time,
        )
        self.assertTrue(reserved["created"])
        self.store.complete_audio(
            self.batch_id,
            message_id,
            f"tmp/{self.batch_id}/{suffix}.ogg",
            "audio.ogg",
            "audio/ogg",
            100,
        )

    def test_same_sender_reuses_active_batch(self):
        batch, created = self.store.create_or_get_active_batch(
            self.sender,
            "automatic",
            batch_id="22222222-2222-4222-8222-222222222222",
            now_ms=2000,
        )
        self.assertFalse(created)
        self.assertEqual(batch["batch_id"], self.batch_id)

    def test_message_dedup_and_order(self):
        _, first = self.store.reserve_audio(self.batch_id, "m1", now_ms=2000)
        _, replay = self.store.reserve_audio(self.batch_id, "m1", now_ms=2100)
        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(first["order"], replay["order"])

    def test_max_five_audio(self):
        for index in range(5):
            self.store.reserve_audio(self.batch_id, f"m{index}", now_ms=2000 + index)
        with self.assertRaises(BatchBusy):
            self.store.reserve_audio(self.batch_id, "m6", now_ms=3000)

    def test_quiet_window_and_active_lease(self):
        self._ready("m1", 10000, "a" * 64)
        _, too_early = self.store.claim_processing(
            self.batch_id,
            quiet_seconds=5,
            lease_token="lease-1",
            now_ms=14999,
        )
        self.assertFalse(too_early["claimed"])
        self.assertEqual(too_early["reason"], "QUIET_WINDOW")

        _, claimed = self.store.claim_processing(
            self.batch_id,
            quiet_seconds=5,
            lease_token="lease-1",
            lease_seconds=120,
            now_ms=15000,
        )
        self.assertTrue(claimed["claimed"])
        self.assertFalse(claimed["recovered"])

        _, blocked = self.store.claim_processing(
            self.batch_id,
            quiet_seconds=5,
            lease_token="lease-2",
            now_ms=16000,
        )
        self.assertFalse(blocked["claimed"])
        self.assertEqual(blocked["reason"], "LEASE_ACTIVE")

    def test_expired_lease_can_be_recovered(self):
        self._ready("m1", 10000, "b" * 64)
        self.store.claim_processing(
            self.batch_id,
            quiet_seconds=5,
            lease_token="lease-1",
            lease_seconds=1,
            now_ms=15000,
        )
        _, recovered = self.store.claim_processing(
            self.batch_id,
            quiet_seconds=5,
            lease_token="lease-2",
            lease_seconds=120,
            now_ms=17000,
        )
        self.assertTrue(recovered["claimed"])
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["lease_token"], "lease-2")

    def test_finish_clears_active_sender(self):
        self._ready("m1", 10000, "c" * 64)
        self.store.claim_processing(
            self.batch_id,
            quiet_seconds=5,
            lease_token="lease-1",
            now_ms=15000,
        )
        batch = self.store.finish_batch(
            self.batch_id,
            "lease-1",
            final_status="completed",
        )
        self.assertEqual(batch["status"], "completed")
        self.assertIsNone(self.store.get_active_batch(self.sender))


if __name__ == "__main__":
    unittest.main()
