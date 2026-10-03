import json
import time
import uuid

from redis.exceptions import WatchError


DEFAULT_TTL_SECONDS = 20 * 60
DEFAULT_LEASE_SECONDS = 120
MAX_TRANSACTION_RETRIES = 8


class BatchNotFound(RuntimeError):
    pass


class BatchBusy(RuntimeError):
    pass


class MultivocaleStateStore:
    """Persistent MultiVocale coordination state backed by Redis.

    Audio bytes never live here. Each file entry stores only metadata and the
    object-storage key. Batch documents are intentionally small (max 5 files),
    so optimistic WATCH/MULTI transactions keep the implementation compact
    while preventing lost updates between workers.
    """

    def __init__(self, redis_client, prefix="vf:mv:v2", ttl_seconds=DEFAULT_TTL_SECONDS):
        if redis_client is None:
            raise ValueError("redis_client obbligatorio")
        self.redis = redis_client
        self.prefix = prefix.rstrip(":")
        self.ttl_seconds = int(ttl_seconds)

    @staticmethod
    def now_ms():
        return int(time.time() * 1000)

    def batch_key(self, batch_id):
        return f"{self.prefix}:batch:{batch_id}"

    def active_key(self, sender):
        return f"{self.prefix}:sender:{sender}:active"

    @staticmethod
    def _decode(value):
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        return json.loads(value)

    @staticmethod
    def _encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def get_batch(self, batch_id):
        return self._decode(self.redis.get(self.batch_key(batch_id)))

    def get_active_batch(self, sender):
        batch_id = self.redis.get(self.active_key(sender))
        if isinstance(batch_id, bytes):
            batch_id = batch_id.decode("utf-8")
        if not batch_id:
            return None
        batch = self.get_batch(batch_id)
        if batch is None:
            self.redis.delete(self.active_key(sender))
            return None
        return batch

    def create_or_get_active_batch(self, sender, mode, batch_id=None, now_ms=None):
        sender = str(sender or "").strip()
        mode = str(mode or "").strip()
        if not sender or mode not in {"manual", "automatic"}:
            raise ValueError("sender/mode non validi")

        now_ms = int(now_ms if now_ms is not None else self.now_ms())
        batch_id = batch_id or str(uuid.uuid4())
        active_key = self.active_key(sender)
        batch_key = self.batch_key(batch_id)

        for _ in range(MAX_TRANSACTION_RETRIES):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(active_key)
                    current = pipe.get(active_key)
                    if isinstance(current, bytes):
                        current = current.decode("utf-8")
                    if current:
                        existing = self.get_batch(current)
                        pipe.unwatch()
                        if existing is not None:
                            return existing, False
                        self.redis.delete(active_key)
                        continue

                    batch = {
                        "batch_id": batch_id,
                        "sender": sender,
                        "mode": mode,
                        "status": "collecting",
                        "created_at_ms": now_ms,
                        "updated_at_ms": now_ms,
                        "last_arrival_at_ms": now_ms,
                        "pending": 0,
                        "next_order": 0,
                        "files": [],
                        "lease_token": None,
                        "lease_expires_at_ms": None,
                        "retry_count": 0,
                        "ready_answer": None,
                    }
                    pipe.multi()
                    pipe.set(batch_key, self._encode(batch), ex=self.ttl_seconds)
                    pipe.set(active_key, batch_id, ex=self.ttl_seconds)
                    pipe.execute()
                    return batch, True
                except WatchError:
                    continue
        raise BatchBusy("Impossibile creare batch dopo retry concorrenti")

    def _mutate(self, batch_id, mutator):
        key = self.batch_key(batch_id)
        for _ in range(MAX_TRANSACTION_RETRIES):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    batch = self._decode(pipe.get(key))
                    if batch is None:
                        pipe.unwatch()
                        raise BatchNotFound(batch_id)
                    result = mutator(batch)
                    batch["updated_at_ms"] = self.now_ms()
                    pipe.multi()
                    pipe.set(key, self._encode(batch), ex=self.ttl_seconds)
                    pipe.execute()
                    return batch, result
                except WatchError:
                    continue
        raise BatchBusy("Impossibile aggiornare batch dopo retry concorrenti")

    def reserve_audio(self, batch_id, message_id, now_ms=None):
        message_id = str(message_id or "").strip()
        if not message_id:
            raise ValueError("message_id obbligatorio")
        now_ms = int(now_ms if now_ms is not None else self.now_ms())

        def mutate(batch):
            for item in batch["files"]:
                if item.get("message_id") == message_id:
                    return {"created": False, "order": item["order"]}
            if batch["status"] not in {"collecting", "failed"}:
                raise BatchBusy("Batch non accetta nuovi audio")
            order = int(batch["next_order"])
            batch["next_order"] = order + 1
            batch["pending"] = int(batch["pending"]) + 1
            batch["last_arrival_at_ms"] = now_ms
            batch["status"] = "collecting"
            batch["files"].append({
                "message_id": message_id,
                "order": order,
                "status": "downloading",
                "object_key": None,
                "filename": None,
                "mime_type": None,
                "size_bytes": None,
            })
            return {"created": True, "order": order}

        return self._mutate(batch_id, mutate)

    def complete_audio(self, batch_id, message_id, object_key, filename, mime_type, size_bytes):
        object_key = str(object_key or "").strip()
        if not object_key:
            raise ValueError("object_key obbligatorio")

        def mutate(batch):
            for item in batch["files"]:
                if item.get("message_id") != message_id:
                    continue
                if item.get("status") == "ready":
                    return {"completed": False, "already_ready": True}
                item.update({
                    "status": "ready",
                    "object_key": object_key,
                    "filename": str(filename or "audio.ogg"),
                    "mime_type": str(mime_type or "audio/ogg"),
                    "size_bytes": int(size_bytes or 0),
                })
                batch["pending"] = max(0, int(batch["pending"]) - 1)
                return {"completed": True, "already_ready": False}
            raise ValueError("message_id non riservato nel batch")

        return self._mutate(batch_id, mutate)

    def fail_audio(self, batch_id, message_id):
        def mutate(batch):
            for item in batch["files"]:
                if item.get("message_id") != message_id:
                    continue
                if item.get("status") == "downloading":
                    batch["pending"] = max(0, int(batch["pending"]) - 1)
                item["status"] = "failed"
                return True
            return False
        return self._mutate(batch_id, mutate)

    def claim_processing(self, batch_id, quiet_seconds, lease_token=None, lease_seconds=DEFAULT_LEASE_SECONDS, now_ms=None):
        now_ms = int(now_ms if now_ms is not None else self.now_ms())
        lease_token = lease_token or str(uuid.uuid4())
        quiet_ms = int(float(quiet_seconds) * 1000)
        lease_ms = int(float(lease_seconds) * 1000)

        def mutate(batch):
            if int(batch.get("pending", 0)) != 0:
                return {"claimed": False, "reason": "PENDING_DOWNLOADS"}
            ready_files = [x for x in batch["files"] if x.get("status") == "ready"]
            if not ready_files:
                return {"claimed": False, "reason": "NO_READY_AUDIO"}
            if now_ms - int(batch.get("last_arrival_at_ms", 0)) < quiet_ms:
                return {"claimed": False, "reason": "QUIET_WINDOW"}

            status = batch.get("status")
            lease_expires = batch.get("lease_expires_at_ms")
            if status == "processing" and lease_expires and int(lease_expires) > now_ms:
                return {"claimed": False, "reason": "LEASE_ACTIVE"}

            recovered = status == "processing"
            if status not in {"collecting", "failed", "processing"}:
                return {"claimed": False, "reason": "STATUS_BLOCKED"}

            batch["status"] = "processing"
            batch["lease_token"] = lease_token
            batch["lease_expires_at_ms"] = now_ms + lease_ms
            if recovered:
                batch["retry_count"] = int(batch.get("retry_count", 0)) + 1
            return {
                "claimed": True,
                "lease_token": lease_token,
                "recovered": recovered,
                "files": sorted(ready_files, key=lambda x: x["order"]),
            }

        return self._mutate(batch_id, mutate)

    def mark_failed(self, batch_id, lease_token):
        def mutate(batch):
            if batch.get("lease_token") != lease_token:
                raise BatchBusy("Lease non valida")
            batch["status"] = "failed"
            batch["lease_token"] = None
            batch["lease_expires_at_ms"] = None
            batch["retry_count"] = int(batch.get("retry_count", 0)) + 1
            return True
        return self._mutate(batch_id, mutate)

    def store_ready_answer(self, batch_id, lease_token, answer):
        def mutate(batch):
            if batch.get("lease_token") != lease_token:
                raise BatchBusy("Lease non valida")
            batch["ready_answer"] = str(answer or "")
            return True
        return self._mutate(batch_id, mutate)

    def finish_batch(self, batch_id, lease_token, final_status="completed"):
        if final_status not in {"completed", "cancelled"}:
            raise ValueError("final_status non valido")
        key = self.batch_key(batch_id)

        for _ in range(MAX_TRANSACTION_RETRIES):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    batch = self._decode(pipe.get(key))
                    if batch is None:
                        pipe.unwatch()
                        raise BatchNotFound(batch_id)
                    if final_status == "completed" and batch.get("lease_token") != lease_token:
                        pipe.unwatch()
                        raise BatchBusy("Lease non valida")
                    active_key = self.active_key(batch["sender"])
                    pipe.watch(active_key)
                    batch["status"] = final_status
                    batch["lease_token"] = None
                    batch["lease_expires_at_ms"] = None
                    batch["updated_at_ms"] = self.now_ms()
                    pipe.multi()
                    pipe.set(key, self._encode(batch), ex=self.ttl_seconds)
                    current = pipe.get(active_key)
                    pipe.execute()
                    current = self.redis.get(active_key)
                    if isinstance(current, bytes):
                        current = current.decode("utf-8")
                    if current == batch_id:
                        self.redis.delete(active_key)
                    return batch
                except WatchError:
                    continue
        raise BatchBusy("Impossibile chiudere batch dopo retry concorrenti")
