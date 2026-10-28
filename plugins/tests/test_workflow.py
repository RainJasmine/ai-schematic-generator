"""Workflow recovery contracts with a fake HTTP service and fake KiCad canvas.

The real session journal is confined to TemporaryDirectory.  No socket, model,
Neo4j, user settings or user schematic is accessed by these tests.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_schematic.ai_service import AiServiceError, WaitingStopped
from ai_schematic.session_store import SessionConflict, SessionStore
from ai_schematic.workflow import ClientWorkflow


OLD_ID, NEW_ID, OTHER_ID = "1" * 32, "2" * 32, "3" * 32
SERVICE = "http://127.0.0.1:8000"


def graph(name):
    return {"graph_schema": "ai-eda-graph-ir/v2.0", "task6_status": "completed",
            "graph_id": name, "nodes": [{"node_id": "N", "component_id": "X"}], "edges": []}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def job(identifier, status="succeeded"):
    prefix = f"/api/v1/jobs/{identifier}"
    return {"job_id": identifier, "status": status, "status_url": prefix,
            "result_url": prefix + "/graph" if status == "succeeded" else None,
            "report_url": prefix + "/report", "revision_result_url": prefix + "/revision-result",
            "run_name": identifier[:1], "summary": {}, "error": None}


class FakeReport:
    def __init__(self, raw, target):
        self.target = deepcopy(target)
        self.value = {"graph_id": raw["graph_id"], "graph_hash": digest(raw),
                      "target": deepcopy(target), "revision_supported": True,
                      "node_items": {"N": ["owned-symbol"]}, "edge_items": {}}

    def to_dict(self):
        return deepcopy(self.value)

    def format(self):
        return "fake complete import"


class CanvasUncertain(RuntimeError):
    uncertain = True


class FakeCanvas:
    def __init__(self):
        self.target = {"key": "sheet-A", "screen": "sheet-A.kicad_sch"}
        self.calls, self.mutations, self.mode = [], 0, "complete"
        self.checked = {"selection": {"node_ids": ["N"], "edge_ids": []},
                        "client_graph_dirty": False, "display_aliases": {"N": "U1"}}
        self.verified, self.cleared = False, 0
        self.adoptions = []
        self.target_reads = 0

    def capture_target(self, *, readback=True):
        self.target_reads += 1
        return deepcopy(self.target)

    def prepare_feedback(self, previous):
        return deepcopy(self.checked)

    def generate(self, parsed, progress, *, target, previous, on_checkpoint, replace_all=False):
        self.calls.append({"graph": deepcopy(parsed.source), "previous": deepcopy(previous), "replace_all": replace_all})
        if self.mode == "preflight-failure":
            raise ValueError("fake missing symbol")
        if previous and previous.get("graph_hash") == digest(parsed.source):
            return FakeReport(parsed.source, target)
        on_checkpoint({"target": deepcopy(target), "created_ids": ["new-symbol"], "phase": "creating"})
        if self.mode == "crash":
            raise CanvasUncertain("fake transport outcome unknown")
        if self.mode == "unexpected-crash":
            raise RuntimeError("unexpected exception after checkpoint")
        self.mutations += 1
        return FakeReport(parsed.source, target)

    def clear_graph_selection(self, previous):
        self.cleared += 1

    def select_graph_objects(self, previous, node_ids, edge_ids):
        return {"node_ids": node_ids, "edge_ids": edge_ids}

    def verify_uncertain(self, checkpoint, previous):
        return {"verified": self.verified, "message": "fake read-only verification"}

    def adopt_legacy_placement(self, placement, parsed):
        self.adoptions.append(deepcopy(placement))
        return FakeReport(parsed.source, self.target)


class FakeClient:
    service_url = SERVICE

    def __init__(self):
        self.posts, self.gets, self.submission_effects = [], [], []
        self.jobs = {OLD_ID: job(OLD_ID), NEW_ID: job(NEW_ID), OTHER_ID: job(OTHER_ID)}
        self.graphs = {OLD_ID: graph("old"), NEW_ID: graph("new"), OTHER_ID: graph("other")}
        self.baseline_override = None
        self.diagnostic = {"interaction_status": "awaiting_input", "message": "Choose a node",
                           "choices": [{"label": "Node N", "node_id": "N"}]}

    def capabilities(self):
        return {"revision": {"canvas_policies": ["verify_local", "backend_only"]}}

    def submit_frozen(self, request):
        if not SessionStore.verify_frozen(request):
            raise ValueError("frozen payload was changed")
        self.posts.append(deepcopy(request))
        if self.submission_effects:
            effect = self.submission_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return deepcopy(effect)
        return job(NEW_ID, "queued")

    def get_job(self, identifier):
        self.gets.append(identifier)
        return deepcopy(self.jobs[identifier])

    def follow_job(self, identifier, *, on_progress, stop_event):
        if stop_event and stop_event.is_set():
            raise WaitingStopped()
        identifier = identifier.rstrip("/").rsplit("/", 1)[-1]
        row = deepcopy(self.jobs[identifier])
        on_progress(row)
        return row

    def graph(self, identifier):
        if "/" in identifier:
            identifier = identifier.rstrip("/").split("/")[-2]
        return deepcopy(self.graphs[identifier])

    def revision_state(self, identifier):
        if self.baseline_override is not None:
            return deepcopy(self.baseline_override)
        return {"head_job_id": identifier, "pending_job_id": None,
                "revision": 1 if identifier == OLD_ID else 2,
                "graph_hash": "a" * 64}

    def revision_result(self, identifier):
        return deepcopy(self.diagnostic)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SessionStore(Path(self.directory.name) / "state")
        self.canvas, self.client = FakeCanvas(), FakeClient()
        self.workflow = ClientWorkflow(self.client, self.store, project_id="sheet-A", canvas=self.canvas)

    def seed_success(self):
        self.workflow.save(success={"job": job(OLD_ID), "graph": self.client.graphs[OLD_ID],
            "baseline": self.client.revision_state(OLD_ID)}, operation={"kind": "create", "job": job(OLD_ID)})

    def test_initial_generation_and_recovery_never_read_or_write_canvas(self):
        with patch.object(self.canvas, "capture_target", side_effect=AssertionError("No IPC during generation")):
            result = self.workflow.create("text", user_text="receiver")
            self.assertTrue(result["ready"])
            self.workflow.resume()
        self.assertEqual(self.canvas.calls, [])
        self.assertIsNone(self.workflow.state.get("import"))
        self.assertEqual(self.workflow.state["success"]["job"]["job_id"], NEW_ID)

    def test_only_explicit_apply_performs_full_page_replacement(self):
        self.workflow.create("text", user_text="receiver")
        self.assertEqual(self.canvas.mutations, 0)
        self.assertTrue(self.workflow.apply_current_graph()["delivered"])
        self.assertEqual(self.canvas.mutations, 1)
        self.assertTrue(self.canvas.calls[-1]["replace_all"])
        self.assertEqual(self.workflow.state["applied_job_id"], NEW_ID)

    def test_feedback_without_applying_or_reading_canvas_produces_pending_new_graph(self):
        self.seed_success()
        with patch.object(self.canvas, "capture_target", side_effect=AssertionError("No canvas binding")), \
             patch.object(self.canvas, "prepare_feedback", side_effect=AssertionError("No canvas read")):
            result = self.workflow.revise("增加滤波器")
        self.assertTrue(result["ready"])
        self.assertEqual(self.canvas.mutations, 0)
        payload = self.client.posts[-1]["payload"]
        self.assertEqual(payload["canvas_policy"], "backend_only")
        self.assertNotIn("client_graph_dirty", payload)
        self.assertEqual(payload["selection"], {"node_ids": [], "edge_ids": []})

    def test_feedback_keeps_old_canvas_until_manual_apply(self):
        self.seed_success()
        self.workflow.apply_current_graph()
        old_import = deepcopy(self.workflow.state["import"])
        self.workflow.revise("增加放大器")
        self.assertEqual(self.canvas.mutations, 1)
        self.assertEqual(self.workflow.state["import"], old_import)
        self.assertEqual(self.workflow.state["applied_job_id"], OLD_ID)
        self.workflow.apply_current_graph()
        self.assertEqual(self.canvas.mutations, 2)
        self.assertEqual(self.workflow.state["applied_job_id"], NEW_ID)

    def test_old_service_is_rejected_before_revision_post(self):
        self.seed_success()
        with patch.object(self.client, "capabilities", return_value={}):
            with self.assertRaises(AiServiceError) as error:
                self.workflow.revise("增加放大器")
        self.assertEqual(error.exception.code, "BACKEND_FEEDBACK_UNSUPPORTED")
        self.assertEqual(self.client.posts, [])

    def test_unknown_create_retry_uses_same_request_id_text_and_bytes(self):
        self.client.submission_effects = [AiServiceError("lost", code="CONNECTION_FAILED")]
        with self.assertRaises(AiServiceError):
            self.workflow.create("text", user_text="original text", run_name="original name")
        frozen = deepcopy(self.workflow.state["pending"]["request"])
        self.workflow.resume()
        self.assertEqual(self.client.posts, [frozen, frozen])
        self.assertEqual(self.canvas.mutations, 0)

    def test_unknown_word_upload_retry_retains_original_bytes(self):
        source = Path(self.directory.name) / "agreement.docx"
        source.write_bytes(b"original docx bytes")
        self.client.submission_effects = [AiServiceError("lost", code="CONNECTION_FAILED")]
        with self.assertRaises(AiServiceError):
            self.workflow.create("word", path=str(source), user_text="original")
        frozen = deepcopy(self.workflow.state["pending"]["request"])
        source.write_bytes(b"changed")
        self.workflow.resume()
        self.assertEqual(self.client.posts, [frozen, frozen])
        self.assertEqual(Path(frozen["path"]).read_bytes(), b"original docx bytes")

    def test_unknown_revision_retry_keeps_exact_base_and_instruction(self):
        self.seed_success()
        self.client.submission_effects = [AiServiceError("lost", code="CONNECTION_FAILED")]
        with self.assertRaises(AiServiceError):
            self.workflow.revise("original feedback")
        frozen = deepcopy(self.workflow.state["pending"]["request"])
        self.workflow.resume()
        self.assertEqual(self.client.posts, [frozen, frozen])

    def test_later_rejection_does_not_discard_prior_unknown_request(self):
        self.client.submission_effects = [AiServiceError("lost", code="CONNECTION_FAILED"),
                                        AiServiceError("denied", status=401, code="UNAUTHORIZED")]
        with self.assertRaises(AiServiceError):
            self.workflow.create("text", user_text="original")
        pending = deepcopy(self.workflow.state["pending"])
        with self.assertRaises(AiServiceError):
            self.workflow.resume()
        self.assertEqual(self.workflow.state["pending"]["request"], pending["request"])

    def test_definite_first_rejection_allows_input_correction(self):
        self.client.submission_effects = [AiServiceError("bad", status=422, code="INVALID_INPUT")]
        with self.assertRaises(AiServiceError):
            self.workflow.create("text", user_text="bad input")
        self.assertIsNone(self.workflow.state["pending"])

    def test_stopped_before_submission_posts_nothing(self):
        stopped = threading.Event(); stopped.set()
        with self.assertRaises(WaitingStopped):
            self.workflow.create("text", user_text="receiver", stop_event=stopped)
        self.assertEqual(self.client.posts, [])

    def test_unknown_request_cannot_be_replaced_by_new_job_or_id(self):
        self.client.submission_effects = [AiServiceError("lost", code="CONNECTION_FAILED")]
        with self.assertRaises(AiServiceError):
            self.workflow.create("text", user_text="receiver")
        for action in (lambda: self.workflow.resume(OTHER_ID), self.workflow.clear_records,
                       self.workflow.arm_new_design, lambda: self.workflow.create("text", user_text="new")):
            with self.assertRaises(ValueError):
                action()

    def test_head_failure_keeps_candidate_and_prevents_manual_apply(self):
        self.client.baseline_override = {}
        with self.assertRaises(AiServiceError):
            self.workflow.create("text", user_text="receiver")
        self.assertTrue(self.workflow.state["operation"]["candidate_graph"])
        self.assertIsNone(self.workflow.state.get("success"))
        with self.assertRaises(ValueError):
            self.workflow.apply_current_graph()
        self.assertEqual(self.canvas.mutations, 0)

    def test_historical_result_cannot_be_applied_over_new_head(self):
        self.seed_success()
        self.client.baseline_override = {"head_job_id": OTHER_ID, "revision": 3, "graph_hash": "a" * 64}
        with self.assertRaises(ValueError):
            self.workflow.apply_current_graph()
        self.assertEqual(self.canvas.mutations, 0)

    def test_finish_and_continue_are_local_and_require_no_canvas(self):
        self.seed_success()
        self.workflow.finish()
        self.assertTrue(self.workflow.state["ended"])
        with self.assertRaises(ValueError):
            self.workflow.revise("after finish")
        self.workflow.new_feedback()
        self.assertFalse(self.workflow.state["ended"])
        self.assertEqual(self.canvas.target_reads, 0)
        self.assertEqual(self.canvas.cleared, 0)

    def test_clear_conversation_survives_reopen_without_deleting_task(self):
        self.seed_success()
        self.workflow.save(conversation=[{"role": "user", "content": "old chat"}])
        self.workflow.clear_conversation()
        reopened = ClientWorkflow(self.client, SessionStore(self.store.directory), project_id="sheet-A", canvas=self.canvas)
        self.assertEqual(reopened.state["conversation"], [])
        self.assertTrue(reopened.state["success"])

    def test_clear_records_removes_display_data_but_preserves_drawing_receipt(self):
        self.seed_success()
        self.workflow.apply_current_graph()
        receipt = deepcopy(self.workflow.state["import"])
        self.workflow.save(conversation=[{"content": "old"}], history=[{"job_id": OLD_ID}], draft={"feedback": "old"})
        self.workflow.clear_records()
        reopened = ClientWorkflow(self.client, SessionStore(self.store.directory), project_id="sheet-A", canvas=self.canvas)
        for key in ("conversation", "history", "interactions", "success", "operation", "draft"):
            self.assertFalse(reopened.state[key], key)
        self.assertEqual(reopened.state["import"], receipt)
        self.assertEqual(self.canvas.mutations, 1)

    def test_new_design_clears_chat_and_draft_without_clearing_canvas(self):
        self.seed_success()
        self.workflow.save(conversation=[{"content": "old"}], draft={"user_text": "old"})
        self.workflow.arm_new_design()
        self.assertEqual(self.workflow.state["conversation"], [])
        self.assertEqual(self.workflow.state["draft"], {})
        self.assertTrue(self.workflow.state.get("history") or self.workflow.state["interactions"])
        self.assertEqual(self.canvas.mutations, 0)

    def test_local_json_also_waits_for_manual_update(self):
        source = Path(self.directory.name) / "graph.json"
        source.write_text(json.dumps(graph("local")), encoding="utf-8")
        result = self.workflow.create("json", path=str(source))
        self.assertTrue(result["local"])
        self.assertEqual(self.client.posts, [])
        self.assertEqual(self.canvas.mutations, 0)
        self.workflow.apply_current_graph()
        self.assertEqual(self.canvas.mutations, 1)

    def test_import_failure_or_unknown_outcome_preserves_ai_result(self):
        self.workflow.create("text", user_text="receiver")
        success = deepcopy(self.workflow.state["success"])
        self.canvas.mode = "crash"
        with self.assertRaises(CanvasUncertain):
            self.workflow.apply_current_graph()
        self.assertEqual(self.workflow.state["success"], success)
        self.assertTrue(self.workflow.state["uncertain"])
        with self.assertRaises(ValueError):
            self.workflow.apply_current_graph()

    def test_unknown_drawing_retry_requires_explicit_confirmation_without_fake_verification(self):
        self.workflow.save(uncertain=True, mutation_checkpoint={"planned_ids": ["old-attempt"]})
        with self.assertRaises(ValueError):
            self.workflow.permit_full_page_retry()
        self.assertTrue(self.workflow.state["uncertain"])
        self.workflow.permit_full_page_retry(user_confirmed=True)
        self.assertFalse(self.workflow.state["uncertain"])
        self.assertFalse(self.workflow.state["manual_recovery"]["canvas_verified"])
        self.assertEqual(self.canvas.target_reads, 0)
        self.assertEqual(self.canvas.mutations, 0)

    def test_readonly_history_preserves_current_graph_and_pending(self):
        self.seed_success()
        self.workflow.save(pending={"request": {"request_id": "unknown"}})
        before = deepcopy(self.workflow.state)
        self.workflow.inspect(OTHER_ID)
        self.assertEqual(self.workflow.state, before)

    def test_store_cas_prevents_second_window_from_overwriting_request(self):
        other = ClientWorkflow(self.client, SessionStore(self.store.directory), project_id="sheet-A", canvas=self.canvas)
        self.workflow.save(draft={"user_text": "window 1"})
        with self.assertRaises(SessionConflict):
            other.save(draft={"user_text": "window 2"})

    def test_failed_or_consultation_round_keeps_last_success_and_canvas(self):
        self.seed_success()
        old = deepcopy(self.workflow.state["success"])
        self.client.jobs[NEW_ID] = job(NEW_ID, "needs_attention")
        self.workflow.revise("只是咨询")
        self.assertEqual(self.workflow.state["success"], old)
        self.assertEqual(self.canvas.mutations, 0)
        self.assertEqual(self.workflow.state["continuation_job_id"], NEW_ID)
