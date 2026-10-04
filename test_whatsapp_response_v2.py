import unittest

import app as legacy
from whatsapp_response_v2 import build_whatsapp_response


class WhatsappResponseV2Tests(unittest.TestCase):
    def test_task_remains_visible_when_summary_mentions_it(self):
        data = {
            "ok": True,
            "summary": (
                "Attività da svolgere include chiamare Mario e "
                "comprare materiale per il lavoro."
            ),
            "salient_points": [],
            "important_details": [],
            "tasks": [
                {
                    "title": "Chiamare Mario",
                    "deadline": "domani",
                    "time": "09:00",
                    "status": "confermato",
                },
                {
                    "title": "Comprare materiale per il lavoro",
                    "deadline": None,
                    "time": None,
                    "status": "confermato",
                },
            ],
        }
        text = build_whatsapp_response(data, legacy)
        self.assertIn("Chiamare Mario", text)
        self.assertIn("domani", text)
        self.assertIn("09:00", text)
        self.assertIn("Comprare materiale per il lavoro", text)
        self.assertEqual(text.count("Comprare materiale per il lavoro"), 1)

    def test_duplicate_tasks_are_not_rendered_twice(self):
        data = {
            "ok": True,
            "summary": "Ci sono attività da svolgere.",
            "salient_points": [],
            "important_details": [],
            "tasks": [
                {"title": "Comprare materiale", "status": "confermato"},
                {"title": "Comprare materiale", "status": "confermato"},
            ],
        }
        text = build_whatsapp_response(data, legacy)
        self.assertEqual(text.count("Comprare materiale"), 1)


if __name__ == "__main__":
    unittest.main()
