import os

import requests


CONTROL_TIMEOUT = (10, 60)
STORAGE_TIMEOUT = (10, 120)


def clean_text(value):
    return str(value or "").strip()


def get_storage_api_url():
    return clean_text(
        os.getenv("VOCALFLASH_MULTIVOCALE_STORAGE_API_URL", "")
    ).rstrip("/")


def get_assistant_api_key():
    return clean_text(
        os.getenv("VOCALFLASH_ASSISTANT_API_KEY", "")
    )


def get_vercel_bypass_secret():
    return clean_text(
        os.getenv("VERCEL_AUTOMATION_BYPASS_SECRET", "")
    )


class MultivocaleStorageClient:
    """Narrow client for temporary MultiVocale audio storage.

    Render never receives Supabase credentials. It authenticates only to the
    VocalFlash internal storage-control endpoint, then uses time-limited signed
    URLs for the exact object authorized by Vercel.
    """

    def __init__(
        self,
        control_url=None,
        assistant_api_key=None,
        vercel_bypass_secret=None,
        requests_module=requests,
    ):
        self.control_url = clean_text(
            control_url or get_storage_api_url()
        )
        self.assistant_api_key = clean_text(
            assistant_api_key or get_assistant_api_key()
        )
        self.vercel_bypass_secret = clean_text(
            vercel_bypass_secret
            if vercel_bypass_secret is not None
            else get_vercel_bypass_secret()
        )
        self.requests = requests_module

        if not self.control_url:
            raise RuntimeError("MULTIVOCALE_STORAGE_API_URL_NOT_CONFIGURED")
        if not self.assistant_api_key:
            raise RuntimeError("ASSISTANT_API_KEY_NOT_CONFIGURED")

    def _control_headers(self):
        headers = {
            "X-VocalFlash-Assistant-Key": self.assistant_api_key,
            "Content-Type": "application/json",
        }
        if self.vercel_bypass_secret:
            headers["x-vercel-protection-bypass"] = self.vercel_bypass_secret
        return headers

    def _control(self, payload):
        response = self.requests.post(
            self.control_url,
            headers=self._control_headers(),
            json=payload,
            timeout=CONTROL_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise ValueError("Risposta storage control non valida")
        return data

    def prepare_upload(self, batch_id, message_id, mime_type):
        data = self._control({
            "action": "prepare_upload",
            "batch_id": clean_text(batch_id),
            "message_id": clean_text(message_id),
            "mime_type": clean_text(mime_type) or "audio/ogg",
        })
        signed = data.get("signed_upload") or {}
        signed_url = clean_text(signed.get("signedUrl"))
        object_key = clean_text(data.get("object_key"))
        if not signed_url or not object_key:
            raise ValueError("Signed upload incompleto")
        return object_key, signed_url

    def upload_bytes(self, signed_url, audio_bytes, mime_type):
        if not audio_bytes:
            raise ValueError("Audio vuoto")
        response = self.requests.put(
            signed_url,
            headers={
                "Content-Type": clean_text(mime_type) or "audio/ogg",
                "Cache-Control": "no-store",
                "x-upsert": "false",
            },
            data=audio_bytes,
            timeout=STORAGE_TIMEOUT,
        )
        response.raise_for_status()
        return True

    def prepare_download(self, object_key):
        data = self._control({
            "action": "prepare_download",
            "object_key": clean_text(object_key),
        })
        signed_url = clean_text(data.get("signed_url"))
        if not signed_url:
            raise ValueError("Signed download incompleto")
        return signed_url

    def download_bytes(self, signed_url):
        response = self.requests.get(
            signed_url,
            timeout=STORAGE_TIMEOUT,
        )
        response.raise_for_status()
        return response.content

    def delete_object(self, object_key):
        self._control({
            "action": "delete",
            "object_key": clean_text(object_key),
        })
        return True
