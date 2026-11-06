import unittest
from unittest.mock import patch

import app as webhook_app


class FakeResponse:
    def __init__(self, payload=None, headers=None, chunks=None):
        self.payload = payload or {}
        self.headers = headers or {}
        self.chunks = chunks or []
        self.closed = False
        self.iterated = False

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        self.iterated = True
        self.chunk_size = chunk_size
        yield from self.chunks

    def close(self):
        self.closed = True


class BoundedAudioDownloadTests(unittest.TestCase):
    def setUp(self):
        self.metadata = FakeResponse({
            "url": "https://media.example/audio",
            "mime_type": "audio/ogg",
        })

    def call_download(self, media_response, on_size_change=None):
        with patch.object(
            webhook_app.requests,
            "get",
            side_effect=[self.metadata, media_response],
        ) as get:
            result = webhook_app.download_whatsapp_audio(
                "media-id", "token", on_size_change=on_size_change
            )
        return result, get

    def test_reservation_callback_runs_before_first_body_chunk(self):
        response = FakeResponse(headers={"Content-Length": "5"}, chunks=[b"abcde"])
        events = []
        original_iter = response.iter_content

        def tracked_iter(chunk_size):
            events.append("read")
            yield from original_iter(chunk_size)

        response.iter_content = tracked_iter
        self.call_download(response, on_size_change=lambda size: events.append(("reserve", size)))
        self.assertEqual(events, [("reserve", 5), "read"])

    def test_missing_length_reserves_maximum_before_reading(self):
        response = FakeResponse(chunks=[b"abc"])
        reservations = []
        with patch.object(webhook_app, "MAX_FILE_BYTES", 5):
            self.call_download(response, on_size_change=reservations.append)
        self.assertEqual(reservations, [5])

    def test_inaccurate_length_expands_reservation_before_extra_chunk(self):
        response = FakeResponse(headers={"Content-Length": "2"}, chunks=[b"ab", b"cd"])
        reservations = []
        self.call_download(response, on_size_change=reservations.append)
        self.assertEqual(reservations, [2, 4])

    def test_download_streams_in_bounded_chunks(self):
        response = FakeResponse(
            headers={"Content-Length": "5"},
            chunks=[b"ab", b"", b"cde"],
        )
        result, get = self.call_download(response)

        self.assertEqual(result, ("audio.ogg", b"abcde", "audio/ogg"))
        self.assertTrue(response.closed)
        self.assertEqual(get.call_args.kwargs["stream"], True)
        self.assertEqual(response.chunk_size, 64 * 1024)

    def test_rejects_advertised_oversized_file_before_reading(self):
        response = FakeResponse(
            headers={"Content-Length": "6"},
            chunks=[b"abcdef"],
        )
        with patch.object(webhook_app, "MAX_FILE_BYTES", 5):
            with self.assertRaisesRegex(ValueError, "supera il limite"):
                self.call_download(response)

        self.assertFalse(response.iterated)
        self.assertTrue(response.closed)

    def test_rejects_stream_that_exceeds_limit_without_buffering_remainder(self):
        response = FakeResponse(chunks=[b"abc", b"def", b"ghi"])
        with patch.object(webhook_app, "MAX_FILE_BYTES", 5):
            with self.assertRaisesRegex(ValueError, "supera il limite"):
                self.call_download(response)

        self.assertTrue(response.closed)

    def test_rejects_invalid_content_length(self):
        response = FakeResponse(headers={"Content-Length": "unknown"})
        with self.assertRaisesRegex(ValueError, "Content-Length"):
            self.call_download(response)

        self.assertFalse(response.iterated)
        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
