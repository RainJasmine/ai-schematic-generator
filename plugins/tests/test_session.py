from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_schematic.session import SessionStore


class SessionStoreTests(unittest.TestCase):
    def test_round_trip_and_no_secret_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "nested" / "session.json")
            state = {"service_url": "http://127.0.0.1:8000",
                     "current_job": {"job_id": "a" * 32}, "pending": None}
            store.save(state)
            self.assertEqual(store.load(), state)
            self.assertEqual([path.name for path in store.path.parent.iterdir()],
                             ["session.json"])
            with self.assertRaisesRegex(ValueError, "令牌"):
                store.save({"token": "secret"})
            self.assertEqual(store.load(), state)


if __name__ == "__main__":
    unittest.main()
