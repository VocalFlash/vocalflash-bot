import json
import threading
import time

from multivocale_state_v2 import BatchBusy, BatchNotFound, MultivocaleStateStore
from multivocale_storage_v1 import MultivocaleStorageClient


DUE_KEY = "vf:mv:v2:due"
COORDINATOR_LOCK_KEY = "vf:mv:v2:coordinator-lock"
LAST_AUDIO_TTL_SECONDS = 20 * 60
POLL_SECONDS = 1.0


class PersistentMultivocaleRuntime:
    """Persistent MultiVocale runtime.

    Redis is the source of truth for batch state and due times. Audio bytes are
    stored only in private temporary object storage. The coordinator does not
    rely on per-batch local timers: after a restart it resumes from Redis due
    entries and the persisted last_arrival_at_ms value.
    """

    def __init__(self, legacy):
        self.legacy = legacy
        self._thread = None
        self._thread_lock = threading.Lock()
        self._stop = threading.Event()

    def _redis(self, config):
        client = self.legacy.get_redis_client(config.get("redis_url"))
        if client is None:
            raise RuntimeError("REDIS_URL mancante")
        return client

    def _store(self, config):
        return MultivocaleStateStore(
            self._redis(config),
            ttl_seconds=self.legacy.SESSION_TTL,
            max_files=self.legacy.MAX_FILES,
        )

    @staticmethod
    def _last_key(sender):
        return f"vf:mv:v2:sender:{sender}:last-audio"

    @staticmethod
    def _queued_key(sender):
        return f"vf:mv:v2:sender:{sender}:queued"

    def _storage(self):
        return MultivocaleStorageClient()

    def ensure_coordinator(self, config):
        with self._thread_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._coordinator_loop,
                args=(dict(config),),
                daemon=True,
                name="vf-multivocale-v2-coordinator",
            )
            self._thread.start()

    def _schedule(self, config, batch):
        if batch.get("mode") != "automatic":
            return
        due_ms = int(batch.get("last_arrival_at_ms") or 0) + int(
            self.legacy.AUTO_BATCH_SECONDS * 1000
        )
        self._redis(config).zadd(DUE_KEY, {batch["batch_id"]: due_ms})

    def _unschedule(self, config, batch_id):
        self._redis(config).zrem(DUE_KEY, batch_id)

    def _remember_last_audio(self, config, sender, file_meta):
        payload = json.dumps(file_meta, separators=(",", ":"))
        self._redis(config).set(
            self._last_key(sender),
            payload,
            ex=LAST_AUDIO_TTL_SECONDS,
        )

    def _get_last_audio(self, config, sender):
        raw = self._redis(config).get(self._last_key(sender))
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            self._redis(config).delete(self._last_key(sender))
            return None

    def _clear_last_audio(self, config, sender):
        self._redis(config).delete(self._last_key(sender))

    def _total_ready_bytes(self, batch):
        return sum(
            int(item.get("size_bytes") or 0)
            for item in batch.get("files") or []
            if item.get("status") == "ready"
        )

    def _create_queued_batch(self, config, sender):
        redis_client = self._redis(config)
        queued_key = self._queued_key(sender)
        queued_id = redis_client.get(queued_key)
        if isinstance(queued_id, bytes):
            queued_id = queued_id.decode("utf-8")
        store = self._store(config)
        if queued_id:
            existing = store.get_batch(queued_id)
            if existing:
                return existing
            redis_client.delete(queued_key)

        # Create a temporary active batch then detach it from the active pointer.
        batch, _ = store.create_or_get_active_batch(sender, "automatic")
        if batch.get("status") == "processing":
            # The active pointer still refers to the processing batch; create a
            # standalone queued document directly with the same schema.
            now_ms = store.now_ms()
            import uuid
            batch_id = str(uuid.uuid4())
            batch = {
                "batch_id": batch_id,
                "sender": sender,
                "mode": "automatic",
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
            redis_client.set(
                store.batch_key(batch_id),
                store._encode(batch),
                ex=store.ttl_seconds,
            )
        redis_client.set(queued_key, batch["batch_id"], ex=store.ttl_seconds)
        return batch

    def _promote_queued(self, config, sender):
        redis_client = self._redis(config)
        queued_key = self._queued_key(sender)
        queued_id = redis_client.get(queued_key)
        if isinstance(queued_id, bytes):
            queued_id = queued_id.decode("utf-8")
        if not queued_id:
            return None
        store = self._store(config)
        batch = store.get_batch(queued_id)
        if not batch:
            redis_client.delete(queued_key)
            return None
        active_key = store.active_key(sender)
        if redis_client.set(active_key, queued_id, nx=True, ex=store.ttl_seconds):
            redis_client.delete(queued_key)
            self._schedule(config, batch)
            return batch
        return None

    def _reserve_target_batch(self, config, sender, message_id):
        store = self._store(config)
        active = store.get_active_batch(sender)
        if active and active.get("status") == "processing":
            batch = self._create_queued_batch(config, sender)
        else:
            batch, _ = store.create_or_get_active_batch(sender, "automatic")
        batch, result = store.reserve_audio(batch["batch_id"], message_id)
        return store, batch, result

    def handle_audio(self, message, config):
        sender = str(message.get("from") or "").strip()
        message_id = str(message.get("id") or "").strip()
        audio_id = str((message.get("audio") or {}).get("id") or "").strip()
        if not sender or not message_id or not audio_id:
            return True

        self.ensure_coordinator(config)
        store, batch, reservation = self._reserve_target_batch(
            config, sender, message_id
        )
        if not reservation.get("created"):
            return True

        try:
            filename, audio_bytes, mime_type = self.legacy.download_whatsapp_audio(
                audio_id,
                config["wa_token"],
            )
            current = store.get_batch(batch["batch_id"]) or batch
            if (
                self._total_ready_bytes(current) + len(audio_bytes)
                > self.legacy.MAX_TOTAL_BYTES
            ):
                store.fail_audio(batch["batch_id"], message_id)
                self.legacy.safe_send(
                    config,
                    sender,
                    "La raccolta ha raggiunto il limite di dimensione. "
                    "Questo vocale non è stato incluso.",
                )
                return True

            storage = self._storage()
            object_key, signed_url = storage.prepare_upload(
                batch["batch_id"], message_id, mime_type
            )
            storage.upload_bytes(signed_url, audio_bytes, mime_type)
            batch, _ = store.complete_audio(
                batch["batch_id"],
                message_id,
                object_key,
                filename,
                mime_type,
                len(audio_bytes),
            )
            if batch.get("mode") == "manual":
                count = len([x for x in batch["files"] if x.get("status") == "ready"])
                self.legacy.safe_send(
                    config,
                    sender,
                    self.legacy.collection_message(count),
                    self.legacy.MULTI_BUTTONS,
                )
            else:
                self._schedule(config, batch)
            return True
        except Exception as exc:
            try:
                store.fail_audio(batch["batch_id"], message_id)
            except Exception:
                pass
            self.legacy.log(
                "MultiVocale V2 audio non riuscito: " + type(exc).__name__
            )
            self.legacy.safe_send(
                config,
                sender,
                "Non sono riuscito a ricevere questo vocale. Riprova.",
            )
            return True

    def activate_manual(self, sender, config):
        self.ensure_coordinator(config)
        store = self._store(config)
        active = store.get_active_batch(sender)
        if active and active.get("status") == "processing":
            self.legacy.safe_send(
                config, sender, "Sto già elaborando una raccolta. Attendi il riepilogo."
            )
            return True
        if active:
            # Keep already collected audio and switch only the mode.
            def mutate(batch):
                batch["mode"] = "manual"
                batch["status"] = "collecting"
                return True
            active, _ = store._mutate(active["batch_id"], mutate)
            self._unschedule(config, active["batch_id"])
            count = len([x for x in active["files"] if x.get("status") == "ready"])
        else:
            active, _ = store.create_or_get_active_batch(sender, "manual")
            count = 0
            previous = self._get_last_audio(config, sender)
            if previous:
                synthetic_id = "previous:" + str(previous.get("message_id") or "audio")
                _, reservation = store.reserve_audio(active["batch_id"], synthetic_id)
                if reservation.get("created"):
                    active, _ = store.complete_audio(
                        active["batch_id"],
                        synthetic_id,
                        previous["object_key"],
                        previous.get("filename") or "audio.ogg",
                        previous.get("mime_type") or "audio/ogg",
                        previous.get("size_bytes") or 0,
                    )
                    count = 1
        if count:
            text = (
                "🎙️ *MultiVocale attivato*\n\n"
                "Ho conservato anche il primo vocale che ti ho appena sintetizzato.\n\n"
                f"*Vocali raccolti: {count}/{self.legacy.MAX_FILES}*\n\n"
                "Inoltrami gli altri vocali e premi *Riepiloga ora* quando hai finito."
            )
        else:
            text = (
                "🎙️ *MultiVocale attivato*\n\n"
                "Inoltrami fino a 5 vocali. Quando hai finito, premi *Riepiloga ora*."
            )
        self.legacy.safe_send(config, sender, text, self.legacy.MULTI_BUTTONS)
        return True

    def cancel(self, sender, config):
        store = self._store(config)
        batch = store.get_active_batch(sender)
        if not batch:
            self.legacy.safe_send(config, sender, "Non ci sono vocali da annullare.")
            return True
        if batch.get("status") == "processing":
            self.legacy.safe_send(
                config, sender, "La sintesi è già in elaborazione. Attendi il risultato."
            )
            return True
        self._unschedule(config, batch["batch_id"])
        store.finish_batch(batch["batch_id"], None, final_status="cancelled")
        self._delete_batch_objects(batch)
        self._clear_last_audio(config, sender)
        self.legacy.safe_send(
            config, sender, "Raccolta annullata. I vocali temporanei sono stati rimossi."
        )
        self._promote_queued(config, sender)
        return True

    def finish_manual(self, sender, config):
        store = self._store(config)
        batch = store.get_active_batch(sender)
        if not batch or batch.get("mode") != "manual":
            self.legacy.safe_send(
                config, sender, "Non ci sono vocali da riepilogare. Inoltra prima almeno un vocale."
            )
            return True
        return self._process_batch(config, batch["batch_id"], quiet_seconds=0)

    def retry(self, sender, config):
        store = self._store(config)
        batch = store.get_active_batch(sender)
        if not batch or batch.get("status") not in {"failed", "collecting"}:
            self.legacy.safe_send(config, sender, "Nessuna raccolta da riprovare.")
            return True
        return self._process_batch(config, batch["batch_id"], quiet_seconds=0)

    def _download_batch_files(self, batch):
        storage = self._storage()
        audio_files = []
        for item in sorted(batch.get("files") or [], key=lambda x: x.get("order", 0)):
            if item.get("status") != "ready":
                continue
            signed_url = storage.prepare_download(item["object_key"])
            audio_bytes = storage.download_bytes(signed_url)
            audio_files.append((
                item.get("filename") or "audio.ogg",
                audio_bytes,
                item.get("mime_type") or "audio/ogg",
            ))
        return audio_files

    def _delete_batch_objects(self, batch, preserve_key=None):
        storage = self._storage()
        for item in batch.get("files") or []:
            object_key = item.get("object_key")
            if not object_key or object_key == preserve_key:
                continue
            try:
                storage.delete_object(object_key)
            except Exception as exc:
                self.legacy.log(
                    "Cleanup storage differito: " + type(exc).__name__
                )

    def _process_batch(self, config, batch_id, quiet_seconds):
        store = self._store(config)
        batch, claim = store.claim_processing(
            batch_id,
            quiet_seconds=quiet_seconds,
        )
        if not claim.get("claimed"):
            reason = claim.get("reason")
            if reason == "QUIET_WINDOW":
                self._schedule(config, batch)
            return True
        lease_token = claim["lease_token"]
        sender = batch["sender"]
        try:
            answer = batch.get("ready_answer")
            if not answer:
                audio_files = self._download_batch_files(batch)
                if not audio_files:
                    raise ValueError("Nessun audio pronto")
                answer = self.legacy.process_audio_with_vocalflash(
                    audio_files,
                    config["api_key"],
                )
                store.store_ready_answer(batch_id, lease_token, answer)
            if not self.legacy.safe_send(config, sender, answer):
                raise RuntimeError("Invio WhatsApp non riuscito")

            fresh = store.get_batch(batch_id) or batch
            ready_files = [x for x in fresh.get("files") or [] if x.get("status") == "ready"]
            preserve_key = None
            if fresh.get("mode") == "automatic" and len(ready_files) == 1:
                last = ready_files[0]
                self._remember_last_audio(config, sender, last)
                preserve_key = last.get("object_key")
            else:
                self._clear_last_audio(config, sender)

            store.finish_batch(batch_id, lease_token, final_status="completed")
            self._unschedule(config, batch_id)
            self._delete_batch_objects(fresh, preserve_key=preserve_key)
            self._promote_queued(config, sender)

            if preserve_key:
                self.legacy.safe_send(
                    config,
                    sender,
                    "Vuoi creare un riepilogo unico di più vocali?\n\n"
                    "Conserverò anche questo primo vocale.",
                    self.legacy.MULTI_START_BUTTONS,
                )
            return True
        except Exception as exc:
            self.legacy.log(
                "MultiVocale V2 sintesi non riuscita: " + type(exc).__name__
            )
            try:
                store.mark_failed(batch_id, lease_token)
            except Exception:
                pass
            self.legacy.safe_send(
                config,
                sender,
                "Non sono riuscito a elaborare questa raccolta. "
                "Premi Riprova per ritentare senza reinviare i vocali.",
                [("vf_retry", "Riprova")],
            )
            return True

    def _coordinator_loop(self, config):
        redis_client = self._redis(config)
        while not self._stop.is_set():
            try:
                # A short distributed lease avoids duplicate coordinator work
                # if more than one process happens to run this loop.
                lock = redis_client.lock(
                    COORDINATOR_LOCK_KEY,
                    timeout=3,
                    blocking_timeout=0.1,
                )
                if lock.acquire(blocking=False):
                    try:
                        now_ms = int(time.time() * 1000)
                        due = redis_client.zrangebyscore(DUE_KEY, 0, now_ms, start=0, num=10)
                        for raw_batch_id in due:
                            batch_id = raw_batch_id.decode("utf-8") if isinstance(raw_batch_id, bytes) else raw_batch_id
                            self._process_batch(
                                config,
                                batch_id,
                                quiet_seconds=self.legacy.AUTO_BATCH_SECONDS,
                            )
                    finally:
                        try:
                            lock.release()
                        except Exception:
                            pass
            except Exception as exc:
                self.legacy.log(
                    "Coordinator MultiVocale V2: " + type(exc).__name__
                )
            self._stop.wait(POLL_SECONDS)

    def handle_message(self, message, config):
        if not isinstance(message, dict):
            return False
        sender = str(message.get("from") or "").strip()
        if not sender:
            return False
        message_type = message.get("type")
        action = ""
        if message_type == "interactive":
            interactive = message.get("interactive") or {}
            reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
            action = str(reply.get("id") or "").strip().lower()
        elif message_type == "text":
            action = str((message.get("text") or {}).get("body") or "").strip().lower()

        if action in {"vf_multi", "multi", "riepiloga più vocali"}:
            return self.activate_manual(sender, config)
        if action in {"vf_cancel", "annulla"}:
            return self.cancel(sender, config)
        if action in {"vf_finish", "riepiloga"}:
            return self.finish_manual(sender, config)
        if action == "vf_retry":
            return self.retry(sender, config)
        if action == "vf_more":
            batch = self._store(config).get_active_batch(sender)
            count = len([x for x in (batch or {}).get("files", []) if x.get("status") == "ready"])
            if batch:
                self.legacy.safe_send(
                    config,
                    sender,
                    self.legacy.collection_message(count),
                    self.legacy.MULTI_BUTTONS,
                )
            else:
                self.legacy.safe_send(
                    config, sender, "La raccolta non è più disponibile."
                )
            return True
        if message_type == "audio":
            return self.handle_audio(message, config)
        return False
