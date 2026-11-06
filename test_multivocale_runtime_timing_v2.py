import unittest

from multivocale_runtime_v2_timing import PersistentMultivocaleRuntime


class FakeRedis:
    def __init__(self):
        self.scheduled = []

    def zadd(self, key, mapping):
        self.scheduled.append((key, mapping))


class FakeLegacy:
    AUTO_BATCH_SECONDS = 5.0


class MultivocaleTimingTests(unittest.TestCase):
    def setUp(self):
        self.runtime = PersistentMultivocaleRuntime(FakeLegacy())
        self.redis = FakeRedis()
        self.runtime._redis = lambda config: self.redis
        self.runtime._is_queued = lambda config, batch: False

    def test_single_audio_gets_transport_ingress_grace(self):
        batch = {
            "batch_id": "batch-single",
            "mode": "automatic",
            "last_arrival_at_ms": 1000,
            "files": [{"status": "ready"}],
        }

        self.runtime._schedule({}, batch)

        self.assertEqual(len(self.redis.scheduled), 1)
        key, mapping = self.redis.scheduled[0]
        self.assertEqual(key, "vf:mv:v2:due")
        self.assertEqual(mapping, {"batch-single": 10000})

    def test_second_audio_uses_sliding_five_second_window(self):
        batch = {
            "batch_id": "batch-two",
            "mode": "automatic",
            "last_arrival_at_ms": 2000,
            "files": [{"status": "ready"}, {"status": "ready"}],
        }

        self.runtime._schedule({}, batch)

        self.assertEqual(len(self.redis.scheduled), 1)
        key, mapping = self.redis.scheduled[0]
        self.assertEqual(key, "vf:mv:v2:due")
        self.assertEqual(mapping, {"batch-two": 7000})

    def test_queued_batch_is_not_scheduled_before_promotion(self):
        batch = {
            "batch_id": "batch-queued",
            "mode": "automatic",
            "last_arrival_at_ms": 1000,
            "files": [{"status": "ready"}],
        }
        self.runtime._is_queued = lambda config, item: True

        self.runtime._schedule({}, batch)

        self.assertEqual(self.redis.scheduled, [])


if __name__ == "__main__":
    unittest.main()
