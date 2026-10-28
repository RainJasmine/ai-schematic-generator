"""Optional native Qt regression checks; all service operations are fake."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication, QMessageBox
    from ai_schematic.management_ui import ManagementDialog
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False

from ai_schematic.management_controller import MaintenanceController
from ai_schematic.ai_service import AiServiceError
from ai_schematic.session_store import SessionStore


@unittest.skipUnless(QT_AVAILABLE, "PySide6 is not installed in this interpreter")
class ManagementUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = SessionStore(self.directory.name)
        self.calls = []
        self.save_errors = []
        owner = self
        self.config = {"version": 1, "effective_version": 1, "pending_application": False,
            "active_provider": "mock", "sources": {},
            "fields": {"llm.timeout": {"type": "number", "min": 1, "max": 3600},
                       "llm.enabled": {"type": "boolean"}, "kicad_symbol_library.paths": {"type": "paths"}},
            "values": {"llm.timeout": 600, "llm.enabled": True, "kicad_symbol_library.paths": ["server/symbols"]},
            "providers": [{"id": "mock", "display_name": "测试厂商", "protocol": "openai-compatible",
                           "base_url": "https://invalid.example/v1", "model_name": "mock-model", "key_configured": True}]}
        self.build = {"job_id": "b" * 32, "operation": "build", "status": "succeeded", "publishable": True,
                      "configuration_version": 1, "target_fingerprint": "f" * 64, "generation_id": "new"}
        self.knowledge = {"configuration_version": 1, "saved_configuration_version": 1, "target_fingerprint": "f" * 64,
                          "target": {"url": "bolt://invalid.example:7687", "database": "mock"},
                          "database": {"status": "checked", "generation_id": "old"}, "local_generation_id": None,
                          "sources": [{"path": "server/symbols"}], "busy": [], "jobs": [self.build]}

        class Client:
            root = service_url = "http://localhost:8000"
            def __init__(self, *args, **kwargs):
                pass
            def record(self, action, payload=None):
                owner.calls.append((action, deepcopy(payload), threading.get_ident()))
            def configuration(self):
                self.record("configuration")
                return deepcopy(owner.config)
            def knowledge(self, **kwargs):
                self.record("knowledge", kwargs)
                return deepcopy(owner.knowledge)
            def save_configuration(self, payload):
                self.record("save", payload)
                if owner.save_errors:
                    raise owner.save_errors.pop(0)
                owner.config["version"] = 2
                owner.config["pending_application"] = payload["intent"] == "save"
                owner.config["effective_version"] = 1 if payload["intent"] == "save" else 2
                return {"version": 2, "effective_version": owner.config["effective_version"]}
            def test_model(self, payload):
                self.record("test", payload)
                return {"status": "passed", "actual_request_sent": True}
            def maintenance(self, job_id):
                self.record("maintenance", job_id)
                return deepcopy(owner.build)
            def post_json(self, path, payload, *, admin=False):
                self.record(path, payload)
                return {"job_id": "c" * 32, "operation": "publish", "status": "queued"}
        self.client_patch = patch("ai_schematic.management_ui.ServiceClient", Client)
        self.controller_patch = patch("ai_schematic.management_ui.MaintenanceController", lambda client: MaintenanceController(client, self.store))
        self.client_patch.start()
        self.controller_patch.start()
        self.dialog = ManagementDialog("http://localhost:8000", "access-secret")
        self.dialog._load()
        self.wait()

    def wait(self):
        deadline = time.monotonic() + 5
        while self.dialog._thread is not None or self.dialog._queued_publish:
            self.app.processEvents()
            time.sleep(.003)
            if time.monotonic() > deadline:
                self.fail("Management worker did not finish")
        self.app.processEvents()

    def tearDown(self):
        self.wait()
        self.dialog._timer.stop()
        self.dialog._config_pending = None
        self.dialog.reject()
        self.dialog.deleteLater()
        self.app.processEvents()
        self.client_patch.stop()
        self.controller_patch.stop()
        self.directory.cleanup()

    def test_fields_are_generated_from_metadata_and_http_uses_worker_thread(self):
        self.assertEqual(set(self.dialog._fields), set(self.config["fields"]))
        self.assertTrue(self.calls)
        self.assertTrue(all(thread != threading.get_ident() for _, _, thread in self.calls))

    def test_save_only_keeps_credentials_out_of_disk_and_clears_inputs(self):
        self.dialog.neo_action.setCurrentIndex(1)
        self.dialog.neo_password.setText("neo-secret")
        self.dialog._providers["custom"] = {"id": "custom", "display_name": "custom", "protocol": "openai-compatible",
            "base_url": "https://invalid.example/v1", "model_name": "model",
            "credential": {"action": "replace", "value": "model-secret"}}
        self.dialog._save_config("save")
        self.wait()
        body = next(payload for action, payload, _ in self.calls if action == "save")
        self.assertEqual(body["intent"], "save")
        self.assertEqual(body["neo4j_credential"]["value"], "neo-secret")
        self.assertEqual(self.dialog.neo_password.text(), "")
        self.assertEqual(self.dialog._providers, {})
        serialized = "".join(path.read_text(encoding="utf-8", errors="ignore") for path in Path(self.directory.name).rglob("*.json"))
        self.assertNotIn("neo-secret", serialized)
        self.assertNotIn("model-secret", serialized)
        self.assertNotIn("access-secret", serialized)

    def test_declined_model_cost_confirmation_sends_no_test(self):
        with patch("ai_schematic.management_ui.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
            self.dialog._test_model()
        self.assertFalse(any(action == "test" for action, _, _ in self.calls))

    def test_unknown_configuration_save_is_not_forgotten_after_a_later_401(self):
        self.save_errors = [AiServiceError("lost", code="CONNECTION_FAILED"),
                            AiServiceError("wrong token", code="UNAUTHORIZED", status=401)]
        self.dialog._save_config("save")
        self.wait()
        original = deepcopy(self.dialog._config_pending)
        self.assertTrue(original)
        self.dialog._retry_config()
        self.wait()
        self.assertEqual(self.dialog._config_pending, original)
        self.assertTrue(self.dialog._config_outcome_unknown)

    def test_publish_confirmation_really_dispatches_after_prepare_worker_finishes(self):
        self.dialog.build_combo.setCurrentIndex(0)
        with patch("ai_schematic.management_ui.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.dialog._prepare_publish(False)
            self.wait()
        publication = [(action, body) for action, body, _ in self.calls if action.endswith("/publications")]
        self.assertEqual(len(publication), 1)
        self.assertEqual(publication[0][1]["build_job_id"], "b" * 32)
        self.assertEqual(publication[0][1]["base_generation_id"], "old")
        self.assertTrue(publication[0][1]["confirm_publish"])


if __name__ == "__main__":
    unittest.main()
