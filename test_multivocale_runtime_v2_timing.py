import unittest

from multivocale_runtime_v2_timing import (
    DUE_KEY,
    SINGLE_AUDIO_INGRESS_GRACE_SECONDS,
    PersistentMultivocaleRuntime,
)


class FakeRedis:
    def __init__(self):
        self.zsets = {}
        self.data = {}

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def get(self, key):
        return self.data.get(key)


class FakeLegacy:
    AUTO_BATCH_SECONDS = 5.0

    def __init__(self, redis):
        self.redis = redis

    def get_redis_client(self, _):
        return self.redis


class TimingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.runtime = PersistentMultivocaleRuntime(FakeLegacy(self.redis))
        self.runtime._is_queued = lambda config, batch: False
        self.config = {"redis_url": "redis://test"}

    def test_single_audio_gets_transport_grace(self):
        batch = {
            "batch_id": "single",
            "mode": "automatic",
            "last_arrival_at_ms": 10000,
            "files": [{"status": "ready"}],
        }
        self.runtime._schedule(self.config, batch)
        expected = 10000 + int((5.0 + SINGLE_AUDIO_INGRESS_GRACE_SECONDS) * 1000)
        self.assertEqual(self.redis.zsets[DUE_KEY]["single"], expected)

    def test_second_audio_returns_to_five_second_sliding_window(self):
        batch = {
            "batch_id": "multi",
            "mode": "automatic",
            "last_arrival_at_ms": 18000,
            "files": [{"status": "ready"}, {"status": "ready"}],
        }
        self.runtime._schedule(self.config, batch)
        self.assertEqual(self.redis.zsets[DUE_KEY]["multi"], 23000)

    def test_manual_batch_is_not_scheduled(self):
        batch = {
            "batch_id": "manual",
            "mode": "manual",
            "last_arrival_at_ms": 10000,
            "files": [{"status": "ready"}],
        }
        self.runtime._schedule(self.config, batch)
        self.assertNotIn("manual", self.redis.zsets.get(DUE_KEY, {}))


if __name__ == "__main__":
    unittest.main()
