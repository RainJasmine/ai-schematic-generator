from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from ai_schematic.ai_service import AiServiceError
from ai_schematic.session_store import SessionConflict, SessionStore, read_legacy


class SessionStoreTests(unittest.TestCase):
    def test_library_sync_generation_is_not_an_access_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            state = {"operation": {"diagnostic": {"trace": [{"result": {"diff": {"node_changes": [
                {"after": {"ports": [{"kicad_sync_token": "797449f78a79bc84-7408c0bf51dc"}]}}
            ]}}}]}}}
            store.save("http://localhost:8000", "project", state)
            self.assertEqual(state, store.load("http://localhost:8000", "project"))
            for key, value in (("kicad_sync_token", "Bearer sensitive"),
                               ("kicad_sync_token", {"token": "secret"}),
                               ("custom_token", "797449f78a79bc84-7408c0bf51dc"),
                               ("access_token", "797449f78a79bc84-7408c0bf51dc")):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    store.save("http://localhost:8000", "project", {"nested": {key: value}})

    def test_namespace_isolated_by_service_and_project(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            state = {"success": {"graph_hash": "a" * 64}, "uncertain": False}
            store.save("http://localhost:8000", "project-a", state)
            self.assertEqual(store.load("http://LOCALHOST:8000/", "project-a"), state)
            self.assertEqual(store.load("http://localhost:8001", "project-a"), {})
            self.assertEqual(store.load("http://localhost:8000", "project-b"), {})

    def test_word_snapshot_survives_original_edit_and_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "技术协议.docx"
            source.write_bytes(b"original")
            store = SessionStore(Path(directory) / "cache")
            frozen = store.freeze_create("word", source, "保留原链路", "接收机")
            self.assertEqual(Path(frozen["path"]).name, source.name)
            source.write_bytes(b"new")
            source.unlink()
            self.assertEqual(Path(frozen["path"]).read_bytes(), b"original")
            self.assertTrue(store.verify_frozen(frozen))
            Path(frozen["path"]).write_bytes(b"tampered")
            self.assertFalse(store.verify_frozen(frozen))

    def test_text_payload_cannot_change_silently_on_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            frozen = SessionStore(directory).freeze_create("text", user_text="设计接收机", run_name="demo")
            self.assertTrue(SessionStore.verify_frozen(frozen))
            frozen["user_text"] = "设计发射机"
            self.assertFalse(SessionStore.verify_frozen(frozen))

    def test_credentials_are_rejected_recursively_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            for key in ("token", "api_key", "neo4j_password", "Authorization", "X-AI-EDA-Admin-Key", "credential"):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    store.save("http://localhost:8000", "project", {"nested": [{key: "secret"}]})
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_atomic_save_failure_keeps_previous_state_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            store.save("http://localhost:8000", "project", {"operation": {"job_id": "a" * 32}})
            with patch("ai_schematic.session_store.os.replace", side_effect=OSError("blocked")):
                with self.assertRaises(OSError):
                    store.save("http://localhost:8000", "project", {"operation": {"job_id": "b" * 32}})
            self.assertEqual(store.load("http://localhost:8000", "project")["operation"]["job_id"], "a" * 32)
            self.assertEqual(len(list(Path(directory).glob("*.json"))), 1)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_corruption_does_not_silently_reset_pending_request(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            store.save("http://localhost:8000", "project", {"pending": {"kind": "create"}})
            next(Path(directory).glob("*.json")).write_text("{broken", encoding="utf-8")
            with self.assertRaises(AiServiceError) as error:
                store.load("http://localhost:8000", "project")
            self.assertEqual(error.exception.code, "SESSION_INVALID")

    def test_clear_only_removes_one_session_and_preserves_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            source = Path(directory) / "source.docx"
            source.write_bytes(b"source")
            frozen = store.freeze_create("word", source)
            store.save("http://localhost:8000", "a", {"pending": frozen})
            store.save("http://localhost:8000", "b", {"ended": True})
            store.clear("http://localhost:8000", "a")
            self.assertEqual(store.load("http://localhost:8000", "a"), {})
            self.assertTrue(store.load("http://localhost:8000", "b")["ended"])
            self.assertTrue(Path(frozen["path"]).is_file())

    def test_stale_instance_cannot_overwrite_another_window_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = SessionStore(directory), SessionStore(directory)
            a = first.load("http://localhost:8000", "project")
            b = second.load("http://localhost:8000", "project")
            a["pending"] = {"request_id": "create-1"}
            first.save("http://localhost:8000", "project", a)
            b["pending"] = {"request_id": "create-2"}
            with self.assertRaises(SessionConflict):
                second.save("http://localhost:8000", "project", b)
            self.assertEqual(first.load("http://localhost:8000", "project")["pending"]["request_id"], "create-1")

    def test_stale_copy_on_same_instance_is_also_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            state = {"ended": False}
            store.save("http://localhost:8000", "project", state)
            stale = deepcopy(state)
            state["pending"] = {"request_id": "create-1"}
            store.save("http://localhost:8000", "project", state)
            with self.assertRaises(SessionConflict):
                store.save("http://localhost:8000", "project", stale)

    def test_atomic_mutations_from_competing_instances_do_not_lose_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            def increment(_):
                store = SessionStore(directory)
                def change(state):
                    state["count"] = state.get("count", 0) + 1
                store.mutate("http://localhost:8000", "project", change)
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(increment, range(16)))
            self.assertEqual(SessionStore(directory).load("http://localhost:8000", "project")["count"], 16)

    def test_revision_payload_is_frozen_after_baseline_and_selection(self):
        payload = {"request_id": "edit-1", "base_revision": 1, "base_graph_hash": "f" * 64,
                   "instruction": "增加滤波器", "selection": {"node_ids": ["N1"], "edge_ids": []},
                   "client_graph_dirty": False, "continuation_job_id": "b" * 32}
        frozen = SessionStore.freeze_revision("a" * 32, payload)
        payload["selection"]["node_ids"].append("N2")
        self.assertEqual(frozen["payload"]["selection"]["node_ids"], ["N1"])
        self.assertTrue(SessionStore.verify_frozen(frozen))
        frozen["payload"]["base_graph_hash"] = "e" * 64
        self.assertFalse(SessionStore.verify_frozen(frozen))

    def test_document_identity_is_allowed_but_not_access_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            store.save("http://localhost:8000", "project", {"placement": {"document_token": "document-identity"}})
            self.assertEqual(store.load("http://localhost:8000", "project")["placement"]["document_token"], "document-identity")

    def test_legacy_read_is_explicit_secret_free_and_never_replays_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.json"
            raw = {"token": "secret", "service_url": "http://localhost:8000", "api_key": "modelsecret",
                   "current_job": {"job_id": "a" * 32, "status": "succeeded", "result_url": "http://foreign/graph"},
                   "placement": {"document_token": "document", "item_ids": ["uuid"]},
                   "pending": {"request_id": "create-1", "instruction": "unfrozen text", "password": "secret"}}
            original = json.dumps(raw).encode()
            path.write_bytes(original)
            migrated = read_legacy(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertNotIn("secret", json.dumps(migrated))
            self.assertEqual(migrated["current_job"]["result_url"], "/api/v1/jobs/" + "a" * 32 + "/graph")
            self.assertTrue(migrated["pending"]["legacy_incomplete"])
            self.assertNotIn("instruction", migrated["pending"])
            self.assertEqual(migrated["placement"]["item_ids"], ["uuid"])


if __name__ == "__main__":
    unittest.main()
