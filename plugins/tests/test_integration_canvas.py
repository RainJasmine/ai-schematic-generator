"""真实状态机 + 绘图器的离线整页更新契约；HTTP/IPC 都是替身。"""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from schematic import schematic_types_pb2 as sch
from ai_schematic import generator
from ai_schematic.api_client import ApiCapabilityError, ApiError, KiCadClient
from ai_schematic.session_store import SessionStore
from ai_schematic.workflow import ClientWorkflow
from test_generator import FakeClient as FakeKiCad, graph
from test_workflow import FakeClient as FakeService, OLD_ID, NEW_ID, job


class CanvasIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.host = FakeKiCad()
        mocked = patch("ai_schematic.generator.KiCadClient", return_value=self.host)
        client_type = mocked.start()
        client_type.require_success = KiCadClient.require_success
        self.addCleanup(mocked.stop)
        self.client = FakeService()
        self.client.graphs[OLD_ID] = deepcopy(graph().source)
        self.client.graphs[NEW_ID] = deepcopy(graph(extra=True).source)
        self.store = SessionStore(Path(self.directory.name) / "sessions")
        self.workflow = ClientWorkflow(self.client, self.store, canvas=generator)
        reads = patch.object(self.host, "get_items", side_effect=AssertionError("No snapshot read allowed"))
        reads.start()
        self.addCleanup(reads.stop)

    def seed(self):
        self.workflow.save(success={"job": job(OLD_ID), "graph": self.client.graphs[OLD_ID],
            "baseline": self.client.revision_state(OLD_ID)}, operation={"job": job(OLD_ID), "kind": "create"})

    def inventory(self):
        return {key: item.SerializeToString() for key, item in self.host.objects.items()}

    def add_manual_line(self):
        item = sch.Line()
        item.id.value = "manual-old-line"
        item.start.x_nm, item.end.x_nm = 1, 500
        self.host.objects[item.id.value] = item

    def test_generation_and_recovery_never_touch_unreachable_canvas(self):
        with patch.object(self.host, "active_context", side_effect=AssertionError("No IPC before apply")):
            self.assertTrue(self.workflow.create("text", user_text="Design receiver")["ready"])
            self.assertTrue(self.workflow.resume()["ready"])
        self.assertEqual(self.host.begins, 0)
        self.assertEqual(self.host.objects, {})

    def test_manual_apply_clears_all_old_objects_including_manual_additions(self):
        self.add_manual_line()
        self.workflow.create("text", user_text="receiver")
        self.assertIn("manual-old-line", self.host.objects)
        self.workflow.apply_current_graph()
        self.assertNotIn("manual-old-line", self.host.objects)
        self.assertEqual(self.host.page_clears, 1)
        self.assertTrue(self.workflow.state["import"]["page_replaced"])
        self.assertFalse(self.workflow.state["import"]["readback_verified"])

    def test_feedback_does_not_replace_until_same_update_button_is_called(self):
        self.seed()
        self.workflow.apply_current_graph()
        before = self.inventory()
        self.workflow.revise("add a component")
        self.assertEqual(self.inventory(), before)
        self.assertEqual(self.host.page_clears, 1)
        self.workflow.apply_current_graph()
        self.assertEqual(self.host.page_clears, 2)
        self.assertEqual(self.workflow.state["applied_job_id"], NEW_ID)
        self.assertEqual(self.workflow.state["import"]["created_nodes"], ["A", "B", "C"])

    def test_clear_then_create_failure_restores_original_page_without_snapshot(self):
        self.add_manual_line()
        old = self.inventory()
        self.workflow.create("text", user_text="receiver")
        self.host.fail_line = True
        with self.assertRaises(ApiError) as error:
            self.workflow.apply_current_graph()
        self.assertTrue(error.exception.rollback_confirmed)
        self.assertFalse(error.exception.rollback_verified)
        self.assertEqual(self.inventory(), old)
        self.assertFalse(self.workflow.state["uncertain"])
        self.assertTrue(self.workflow.state["success"])

    def test_missing_clear_interface_keeps_old_page_and_allows_retry_same_result(self):
        self.add_manual_line()
        old = self.inventory()
        self.workflow.create("text", user_text="receiver")
        self.host.clear_unsupported = True
        with self.assertRaises(ApiCapabilityError):
            self.workflow.apply_current_graph()
        self.assertEqual(self.inventory(), old)
        self.assertFalse(self.workflow.state["uncertain"])
        self.host.clear_unsupported = False
        self.workflow.apply_current_graph()
        self.assertEqual(len(self.client.posts), 1)

    def test_lost_commit_ack_never_automatically_replays_clear(self):
        self.workflow.create("text", user_text="receiver")
        self.host.fail_commit = True
        with self.assertRaises(generator.GenerationUncertain):
            self.workflow.apply_current_graph()
        self.assertTrue(self.workflow.state["uncertain"])
        self.workflow.resume()
        self.assertEqual(self.host.page_clears, 1)
        with self.assertRaises(ValueError):
            self.workflow.apply_current_graph()

    def test_manual_changes_do_not_enter_backend_feedback(self):
        self.seed()
        self.add_manual_line()
        old = self.inventory()
        with patch.object(self.host, "active_context", side_effect=AssertionError("Feedback must not read canvas")):
            self.workflow.revise("increase gain")
        payload = self.client.posts[-1]["payload"]
        self.assertEqual(payload["canvas_policy"], "backend_only")
        self.assertNotIn("snapshot", payload)
        self.assertEqual(self.inventory(), old)

    def test_journal_failure_prevents_clear_and_preserves_ready_result(self):
        self.workflow.create("text", user_text="receiver")
        self.add_manual_line()
        old = self.inventory()
        real_save = self.store.save
        def fail_journal(service, project, state, **kwargs):
            if state.get("mutation_checkpoint"):
                raise OSError("disk full")
            return real_save(service, project, state, **kwargs)
        with patch.object(self.store, "save", side_effect=fail_journal):
            with self.assertRaises(ApiError):
                self.workflow.apply_current_graph()
        self.assertEqual(self.host.begins, 0)
        self.assertEqual(self.inventory(), old)
        self.assertTrue(self.workflow.state["success"])
