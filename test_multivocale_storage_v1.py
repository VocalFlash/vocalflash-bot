import unittest

from multivocale_storage_v1 import MultivocaleStorageClient


class FakeResponse:
    def __init__(self, data=None, content=b"", status=200):
        self._data = data
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


class FakeRequests:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        action = kwargs["json"]["action"]
        if action == "prepare_upload":
            return FakeResponse({
                "ok": True,
                "object_key": "tmp/11111111-1111-4111-8111-111111111111/" + "a" * 64 + ".ogg",
                "signed_upload": {"signedUrl": "https://storage.example/upload?token=x"},
            })
        if action == "prepare_download":
            return FakeResponse({
                "ok": True,
                "signed_url": "https://storage.example/download?token=y",
            })
        if action == "delete":
            return FakeResponse({"ok": True})
        raise AssertionError(action)

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return FakeResponse({"ok": True})

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse(content=b"audio")


class StorageClientTests(unittest.TestCase):
    def setUp(self):
        self.requests = FakeRequests()
        self.client = MultivocaleStorageClient(
            control_url="https://vf.example/api/v1/multivocale-storage",
            assistant_api_key="assistant-secret",
            requests_module=self.requests,
        )

    def test_control_uses_only_assistant_key(self):
        self.client.prepare_upload(
            "11111111-1111-4111-8111-111111111111",
            "wamid.1",
            "audio/ogg",
        )
        headers = self.requests.calls[0][2]["headers"]
        self.assertEqual(
            headers["X-VocalFlash-Assistant-Key"],
            "assistant-secret",
        )
        self.assertNotIn("X-API-Key", headers)
        self.assertNotIn("x-vercel-protection-bypass", headers)

    def test_vercel_bypass_only_reaches_control_api(self):
        client = MultivocaleStorageClient(
            control_url="https://vf.example/api/v1/multivocale-storage",
            assistant_api_key="assistant-secret",
            vercel_bypass_secret="vercel-bypass",
            requests_module=self.requests,
        )
        _, signed_url = client.prepare_upload(
            "11111111-1111-4111-8111-111111111111",
            "wamid.1",
            "audio/ogg",
        )
        control_headers = self.requests.calls[0][2]["headers"]
        self.assertEqual(
            control_headers["x-vercel-protection-bypass"],
            "vercel-bypass",
        )
        client.upload_bytes(signed_url, b"abc", "audio/ogg")
        upload_headers = self.requests.calls[-1][2]["headers"]
        self.assertNotIn("X-VocalFlash-Assistant-Key", upload_headers)
        self.assertNotIn("x-vercel-protection-bypass", upload_headers)

    def test_signed_upload_is_direct_and_has_no_vocalflash_secret(self):
        object_key, signed_url = self.client.prepare_upload(
            "11111111-1111-4111-8111-111111111111",
            "wamid.1",
            "audio/ogg",
        )
        self.assertTrue(object_key.startswith("tmp/"))
        self.client.upload_bytes(signed_url, b"abc", "audio/ogg")
        method, _, kwargs = self.requests.calls[-1]
        self.assertEqual(method, "PUT")
        self.assertEqual(kwargs["data"], b"abc")
        self.assertNotIn("X-VocalFlash-Assistant-Key", kwargs["headers"])

    def test_download_and_delete(self):
        object_key = "tmp/11111111-1111-4111-8111-111111111111/" + "a" * 64 + ".ogg"
        signed_url = self.client.prepare_download(object_key)
        self.assertEqual(self.client.download_bytes(signed_url), b"audio")
        self.assertTrue(self.client.delete_object(object_key))

    def test_missing_configuration_fails_closed(self):
        with self.assertRaises(RuntimeError):
            MultivocaleStorageClient(
                control_url="",
                assistant_api_key="x",
                requests_module=self.requests,
            )


if __name__ == "__main__":
    unittest.main()
