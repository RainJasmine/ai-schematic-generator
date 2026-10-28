from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from ai_schematic.ai_service import AiServiceError
from ai_schematic.management_controller import (
    MaintenanceController, build_request, configuration_change, credential_change, publication_request,
)
from ai_schematic.session_store import SessionConflict, SessionStore


def configuration():
    return {"version": 2, "effective_version": 1, "active_provider": "original",
            "fields": {"llm.timeout": {"type": "number", "min": 1, "max": 3600},
                       "kicad_symbol_library.paths": {"type": "paths"},
                       "llm.enabled": {"type": "boolean"}},
            "values": {"llm.timeout": 600, "kicad_symbol_library.paths": [], "llm.enabled": True}}


def knowledge():
    return {"configuration_version": 2, "target_fingerprint": "f" * 64, "busy": [],
            "database": {"status": "checked", "generation_id": "old"}}


def build():
    return {"operation": "build", "job_id": "b" * 32, "status": "succeeded", "publishable": True,
            "configuration_version": 2, "target_fingerprint": "f" * 64, "generation_id": "new"}


class FakeClient:
    root = "http://localhost:8000"

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def post_json(self, path, payload, *, admin=False):
        self.calls.append((path, deepcopy(payload), admin))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


class ManagementControllerTests(unittest.TestCase):
    def test_keep_clear_never_send_value_or_mask(self):
        self.assertEqual(credential_change("keep", "unused"), {"action": "keep"})
        self.assertEqual(credential_change("clear", "unused"), {"action": "clear"})
        with self.assertRaises(ValueError):
            credential_change("replace", "***masked")

    def test_apply_has_no_incidental_form_edits(self):
        value = configuration_change(configuration(), {"uneditable": 5}, intent="apply",
                                     providers=[{"credential": {"action": "replace", "value": "secret"}}])
        self.assertEqual(set(value), {"request_id", "base_version", "intent"})
        self.assertEqual(value["intent"], "apply")

    def test_restore_creates_new_version_from_explicit_old_base(self):
        value = configuration_change(configuration(), {}, restore_version=0)
        self.assertEqual(value["base_version"], 2)
        self.assertEqual(value["restore_version"], 0)
        self.assertNotIn("changes", value)

    def test_read_only_or_out_of_range_fields_are_rejected(self):
        for changes in ({"uneditable": 1}, {"llm.timeout": 0}, {"llm.enabled": "true"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                configuration_change(configuration(), changes)

    def test_provider_and_paths_payload_are_distinct_from_credentials(self):
        provider = {"id": "custom", "display_name": "自定义", "protocol": "openai-compatible",
                    "base_url": "https://api.example/v1", "model_name": "model",
                    "credential": credential_change("replace", "new-key")}
        value = configuration_change(configuration(), {"kicad_symbol_library.paths": ["server/symbols"]},
                                     providers=[provider], active_provider="custom")
        self.assertEqual(value["changes"], {"kicad_symbol_library.paths": ["server/symbols"]})
        self.assertEqual(value["provider_updates"][0]["credential"]["value"], "new-key")
        with self.assertRaises(ValueError):
            configuration_change(configuration(), {}, active_provider="custom", delete_provider_ids=["custom"])

    def test_publish_requires_real_database_check_and_matching_build(self):
        for change in ({"database": {"status": "not_checked"}}, {"configuration_version": 3},
                       {"target_fingerprint": "e" * 64}):
            current = {**knowledge(), **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                publication_request(current, build())
        value = publication_request(knowledge(), build())
        self.assertEqual(value["base_generation_id"], "old")
        self.assertTrue(value["confirm_publish"])

    def test_bad_build_and_active_maintenance_cannot_publish(self):
        for status in ("failed", "needs_attention", "running"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                publication_request(knowledge(), {**build(), "status": status})
        with self.assertRaises(ValueError):
            build_request({**knowledge(), "busy": [{"status": "needs_attention"}]})

    def test_unknown_post_is_durable_and_retry_uses_exact_request_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            payload = build_request(knowledge())
            client = FakeClient(AiServiceError("lost", code="CONNECTION_FAILED"))
            controller = MaintenanceController(client, store)
            with self.assertRaises(AiServiceError):
                controller.submit("/api/v1/admin/knowledge/builds", payload)
            recovered = MaintenanceController(FakeClient(build()), SessionStore(directory))
            job = recovered.retry_pending()
            self.assertEqual(job["job_id"], "b" * 32)
            self.assertEqual(recovered.client.calls[0][1], payload)
            self.assertNotIn("pending", recovered.state)

    def test_other_window_cannot_replace_unknown_request(self):
        with tempfile.TemporaryDirectory() as directory:
            first = MaintenanceController(FakeClient(AiServiceError("lost")), SessionStore(directory))
            second = MaintenanceController(FakeClient(build()), SessionStore(directory))
            with self.assertRaises(AiServiceError):
                first.submit("/api/v1/admin/knowledge/builds", build_request(knowledge()))
            with self.assertRaises(SessionConflict):
                second.submit("/api/v1/admin/knowledge/builds", build_request(knowledge()))
            self.assertEqual(second.client.calls, [])

    def test_later_401_cannot_prove_an_earlier_unknown_post_was_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = build_request(knowledge())
            first = MaintenanceController(FakeClient(AiServiceError("lost")), SessionStore(directory))
            with self.assertRaises(AiServiceError):
                first.submit("/api/v1/admin/knowledge/builds", payload)
            recovered = MaintenanceController(FakeClient(AiServiceError("bad token", code="UNAUTHORIZED", status=401)), SessionStore(directory))
            with self.assertRaises(AiServiceError):
                recovered.retry_pending()
            self.assertEqual(recovered.state["pending"]["payload"], payload)
            self.assertTrue(recovered.state["pending_outcome_unknown"])

    def test_definite_rejection_clears_pending_but_conflicting_request_preserved(self):
        for code, status, retained in (("ADMIN_REQUIRED", 403, False), ("REQUEST_CONFLICT", 409, True), ("HTTP_500", 500, True)):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                controller = MaintenanceController(FakeClient(AiServiceError("error", code=code, status=status)), SessionStore(directory))
                with self.assertRaises(AiServiceError):
                    controller.submit("/api/v1/admin/knowledge/builds", build_request(knowledge()))
                self.assertEqual(bool(controller.state.get("pending")), retained)

    def test_recovery_uses_original_build_and_new_request_id(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = publication_request(knowledge(), build())
            row = {"job_id": "c" * 32, "operation": "publish", "status": "needs_attention", "requires_recovery": True}
            controller = MaintenanceController(FakeClient(row), SessionStore(directory))
            controller.submit("/api/v1/admin/knowledge/publications", payload)
            self.assertEqual(controller.recovery_build_id(), "b" * 32)
            retry = publication_request(knowledge(), build())
            self.assertNotEqual(retry["request_id"], payload["request_id"])
            self.assertEqual(retry["build_job_id"], payload["build_job_id"])

    def test_foreign_adopted_job_cannot_reuse_an_old_publication_request(self):
        with tempfile.TemporaryDirectory() as directory:
            row = {"job_id": "c" * 32, "operation": "publish", "status": "succeeded", "requires_recovery": False}
            controller = MaintenanceController(FakeClient(row), SessionStore(directory))
            controller.submit("/api/v1/admin/knowledge/publications", publication_request(knowledge(), build()))
            controller.record_job({"job_id": "d" * 32, "operation": "publish", "status": "needs_attention", "requires_recovery": True})
            with self.assertRaises(ValueError):
                controller.recovery_build_id()


if __name__ == "__main__":
    unittest.main()
