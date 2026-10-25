"""Integration checks against an ephemeral, real Redis server in CI.

These tests deliberately use a unique key prefix and never flush the Redis DB.
"""
import os
import threading
import unittest
import uuid

import redis

from multivocale_state_v2 import BatchBusy, MultivocaleStateStore


REDIS_URL = os.getenv("VF_TEST_REDIS_URL")


@unittest.skipUnless(REDIS_URL, "VF_TEST_REDIS_URL is only configured in CI")
class RealRedisLeaseConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.redis = redis.Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        self.redis.ping()
        self.prefix = "vf:test:lease:" + uuid.uuid4().hex
        self.store = MultivocaleStateStore(
            self.redis, prefix=self.prefix, ttl_seconds=300, max_files=5
        )
        self.sender = "test-" + uuid.uuid4().hex
        self.batch_id = str(uuid.uuid4())
        now_ms = self.store.now_ms()
        self.store.create_or_get_active_batch(
            self.sender, "automatic", batch_id=self.batch_id, now_ms=now_ms
        )
        self.store.reserve_audio(self.batch_id, "message-1", now_ms=now_ms)
        self.store.complete_audio(
            self.batch_id,
            "message-1",
            "test-object/audio.ogg",
            "audio.ogg",
            "audio/ogg",
            12,
        )

    def tearDown(self):
        for key in self.redis.scan_iter(match=self.prefix + ":*"):
            self.redis.delete(key)
        self.redis.close()

    def test_concurrent_claim_and_stale_fencing_after_recovery(self):
        barrier = threading.Barrier(3)
        outcomes = []
        errors = []
        lock = threading.Lock()
        claim_time = self.store.now_ms() + 10

        def claim(token):
            try:
                barrier.wait(timeout=3)
                _, result = self.store.claim_processing(
                    self.batch_id,
                    quiet_seconds=0,
                    lease_token=token,
                    lease_seconds=1,
                    now_ms=claim_time,
                )
                with lock:
                    outcomes.append((token, result))
            except Exception as exc:
                with lock:
                    errors.append(exc)

        workers = [
            threading.Thread(target=claim, args=("worker-a",)),
            threading.Thread(target=claim, args=("worker-b",)),
        ]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=3)
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), "claim worker did not finish")

        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 2)
        winners = [(token, result) for token, result in outcomes if result.get("claimed")]
        self.assertEqual(
            len(winners), 1,
            "Real Redis WATCH/MULTI must allow exactly one concurrent lease claimant.",
        )
        stale_token = winners[0][0]
        claimed = self.store.get_batch(self.batch_id)
        self.assertEqual(claimed["lease_token"], stale_token)

        recovery_time = int(claimed["lease_expires_at_ms"]) + 1
        _, recovery = self.store.claim_processing(
            self.batch_id,
            quiet_seconds=0,
            lease_token="recovery-worker",
            lease_seconds=30,
            now_ms=recovery_time,
        )
        self.assertTrue(recovery["claimed"])
        self.assertTrue(recovery["recovered"])

        with self.assertRaises(BatchBusy):
            self.store.store_ready_answer(
                self.batch_id, stale_token, "stale answer"
            )
        with self.assertRaises(BatchBusy):
            self.store.finish_batch(
                self.batch_id, stale_token, final_status="completed"
            )

        self.store.store_ready_answer(
            self.batch_id, "recovery-worker", "current answer"
        )
        final_batch = self.store.finish_batch(
            self.batch_id, "recovery-worker", final_status="completed"
        )
        self.assertEqual(final_batch["status"], "completed")
        self.assertEqual(final_batch["ready_answer"], "current answer")
        self.assertIsNone(self.store.get_active_batch(self.sender))


    def test_processing_state_survives_client_restart_and_expired_lease_recovers(self):
        original_now = self.store.now_ms()
        _, claimed = self.store.claim_processing(
            self.batch_id,
            quiet_seconds=0,
            lease_token="before-restart",
            lease_seconds=1,
            now_ms=original_now + 10,
        )
        self.assertTrue(claimed["claimed"])
        self.store.store_ready_answer(
            self.batch_id, "before-restart", "prepared answer survives restart"
        )
        before_restart = self.store.get_batch(self.batch_id)
        self.assertEqual(before_restart["status"], "processing")
        self.assertEqual(
            before_restart["ready_answer"], "prepared answer survives restart"
        )

        # Simulate a bot-process restart: discard the old Redis client and
        # construct a new client/store against the same persistent Redis keys.
        self.redis.close()
        self.redis = redis.Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        self.redis.ping()
        restarted_store = MultivocaleStateStore(
            self.redis, prefix=self.prefix, ttl_seconds=300, max_files=5
        )
        persisted = restarted_store.get_batch(self.batch_id)
        self.assertEqual(persisted["lease_token"], "before-restart")
        self.assertEqual(
            persisted["ready_answer"], "prepared answer survives restart"
        )

        _, recovered = restarted_store.claim_processing(
            self.batch_id,
            quiet_seconds=0,
            lease_token="after-restart",
            lease_seconds=30,
            now_ms=int(persisted["lease_expires_at_ms"]) + 1,
        )
        self.assertTrue(recovered["claimed"])
        self.assertTrue(recovered["recovered"])
        with self.assertRaises(BatchBusy):
            restarted_store.finish_batch(
                self.batch_id, "before-restart", final_status="completed"
            )
        final_batch = restarted_store.finish_batch(
            self.batch_id, "after-restart", final_status="completed"
        )
        self.assertEqual(final_batch["status"], "completed")
        self.assertEqual(
            final_batch["ready_answer"], "prepared answer survives restart"
        )



if __name__ == "__main__":
    unittest.main()
