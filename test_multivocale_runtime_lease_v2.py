import unittest
from unittest.mock import patch

import multivocale_runtime_v2 as runtime_module
from multivocale_runtime_v2 import PersistentMultivocaleRuntime


class FakeLeaseEvent:
    def __init__(self):
        self.calls = 0
        self.stopped = False

    def wait(self, _timeout):
        self.calls += 1
        if self.stopped:
            return True
        return self.calls > 1

    def set(self):
        self.stopped = True


class FakeThread:
    def __init__(self, target, daemon=None, name=None):
        self.target = target
        self.daemon = daemon
        self.name = name
        self.started = False
        self.finished = False

    def start(self):
        self.started = True
        self.target()
        self.finished = True

    def is_alive(self):
        return self.started and not self.finished

    def join(self, timeout=None):
        return None


class FakeStore:
    def __init__(self, events, fail_renew=False, fail_finish=False):
        self.events = events
        self.fail_renew = fail_renew
        self.fail_finish = fail_finish
        self.renew_calls = 0
        self.batch = {
            "batch_id": "batch-1",
            "sender": "393331234567",
            "mode": "manual",
            "status": "processing",
            "ready_answer": "Risposta sintetica",
            "files": [],
        }

    def claim_processing(self, batch_id, quiet_seconds, lease_seconds):
        self.events.append(("claim", batch_id, lease_seconds))
        return self.batch, {"claimed": True, "lease_token": "lease-1"}

    def renew_lease(self, batch_id, lease_token, lease_seconds):
        self.renew_calls += 1
        self.events.append(("renew", self.renew_calls))
        if self.fail_renew:
            raise RuntimeError("simulated lease renewal failure")
        return self.batch, True

    def get_batch(self, batch_id):
        return self.batch

    def finish_batch(self, batch_id, lease_token, final_status):
        self.events.append(("finish", final_status))
        if self.fail_finish:
            raise RuntimeError("simulated completion persistence failure")
        self.batch["status"] = final_status
        return self.batch

    def mark_failed(self, batch_id, lease_token):
        self.events.append(("mark_failed",))
        self.batch["status"] = "failed"
        return self.batch


class FakeLegacy:
    MULTIVOCALE_LEASE_SECONDS = 3
    MULTI_BUTTONS = []
    MULTI_START_BUTTONS = []

    def __init__(self, events):
        self.events = events
        self.sent = []
        self.logs = []

    def safe_send(self, config, sender, text, buttons=None):
        self.events.append(("send", text))
        self.sent.append(text)
        return True

    def log(self, message):
        self.logs.append(message)


class RuntimeLeaseHeartbeatTests(unittest.TestCase):
    def _runtime(self, fail_renew=False, fail_finish=False):
        events = []
        legacy = FakeLegacy(events)
        store = FakeStore(events, fail_renew=fail_renew, fail_finish=fail_finish)
        runtime = PersistentMultivocaleRuntime(legacy)
        runtime._store = lambda config: store
        runtime._unschedule = lambda config, batch_id: events.append(("unschedule", batch_id))
        runtime._clear_last_audio = lambda config, sender: events.append(("clear_last", sender))
        runtime._delete_batch_objects = lambda config, batch, preserve_key=None: events.append(("cleanup",))
        runtime._promote_queued = lambda config, sender: events.append(("promote", sender))
        return runtime, legacy, store, events

    def test_heartbeat_renews_lease_and_pre_send_check_precedes_reply(self):
        runtime, legacy, store, events = self._runtime()

        with patch.object(runtime_module.threading, "Event", FakeLeaseEvent), patch.object(
            runtime_module.threading, "Thread", FakeThread
        ):
            runtime._process_batch({}, "batch-1", quiet_seconds=0)

        self.assertGreaterEqual(store.renew_calls, 2)
        reply_index = events.index(("send", "Risposta sintetica"))
        renew_indexes = [i for i, event in enumerate(events) if event[0] == "renew"]
        self.assertTrue(any(index < reply_index for index in renew_indexes))
        self.assertIn(("finish", "completed"), events)
        self.assertNotIn("Non sono riuscito a elaborare questa raccolta. Premi Riprova per ritentare senza reinviare i vocali.", legacy.sent)

    def test_failed_lease_renewal_blocks_sending_the_prepared_reply(self):
        runtime, legacy, store, events = self._runtime(fail_renew=True)

        with patch.object(runtime_module.threading, "Event", FakeLeaseEvent), patch.object(
            runtime_module.threading, "Thread", FakeThread
        ):
            runtime._process_batch({}, "batch-1", quiet_seconds=0)

        self.assertEqual(store.renew_calls, 2)
        self.assertNotIn("Risposta sintetica", legacy.sent)
        self.assertIn(("mark_failed",), events)
        self.assertTrue(any("lease rinnovo non riuscito" in message for message in legacy.logs))
        self.assertTrue(any(event[0] == "send" and "Premi Riprova" in event[1] for event in events))

    def test_send_occurs_before_completion_persistence_and_outbox_is_needed(self):
        runtime, legacy, store, events = self._runtime(fail_finish=True)

        with patch.object(runtime_module.threading, "Event", FakeLeaseEvent), patch.object(
            runtime_module.threading, "Thread", FakeThread
        ):
            runtime._process_batch({}, "batch-1", quiet_seconds=0)

        reply_index = events.index(("send", "Risposta sintetica"))
        finish_index = events.index(("finish", "completed"))
        self.assertLess(reply_index, finish_index)
        self.assertIn(("mark_failed",), events)
        self.assertEqual(store.batch["ready_answer"], "Risposta sintetica")
        # No unit test can establish whether a real Meta request was accepted;
        # durable delivery state and reconciliation are required for that case.


if __name__ == "__main__":
    unittest.main()
