from multivocale_runtime_v2 import (
    DUE_KEY,
    PersistentMultivocaleRuntime as BasePersistentMultivocaleRuntime,
)


# Meta can deliver consecutive audio webhooks a few seconds apart even when the
# user sends the voice notes almost back-to-back. Keep the product's 5-second
# quiet window, but give a single first audio a short ingress grace so a delayed
# companion webhook can still join the same automatic batch.
SINGLE_AUDIO_INGRESS_GRACE_SECONDS = 4.0


class PersistentMultivocaleRuntime(BasePersistentMultivocaleRuntime):
    """V2 runtime with transport-aware timing for automatic voice-note bursts."""

    def _schedule(self, config, batch):
        if batch.get("mode") != "automatic":
            return
        if self._is_queued(config, batch):
            return

        quiet_seconds = float(getattr(self.legacy, "AUTO_BATCH_SECONDS", 5.0))
        ready_count = len(
            [item for item in (batch.get("files") or []) if item.get("status") == "ready"]
        )

        # Only the first ready audio gets transport grace. As soon as a second
        # audio joins the batch, the normal 5-second sliding window applies from
        # the latest arrival, preserving the intended UX.
        ingress_grace = (
            SINGLE_AUDIO_INGRESS_GRACE_SECONDS if ready_count <= 1 else 0.0
        )
        due_ms = int(batch.get("last_arrival_at_ms") or 0) + int(
            (quiet_seconds + ingress_grace) * 1000
        )
        self._redis(config).zadd(DUE_KEY, {batch["batch_id"]: due_ms})
