"""Optional offscreen Qt checks; all clients, sessions and settings are fakes.

Run with the plugin's PySide6 dependency installed. No user registry/settings,
HTTP, IPC, browser or real document is used. Without Qt these checks skip.
"""
from __future__ import annotations

from copy import deepcopy
import os
import sys
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:
    HAVE_QT = False

from ai_schematic.ai_service import AiServiceError, _service_root


A, B = "http://127.0.0.1:8000", "http://127.0.0.1:8001"
OLD_ID, NEW_ID = "1" * 32, "2" * 32


def saved_job(identifier):
    return {"job_id": identifier, "status": "succeeded", "report_url": f"/api/v1/jobs/{identifier}/report"}


class MemorySettings:
    values = {}

    def __init__(self, *args):
        pass

    def value(self, key, default=None):
        return deepcopy(self.values.get(key, default))

    def setValue(self, key, value):
        self.values[key] = deepcopy(value)


class MemoryStore:
    def __init__(self):
        self.states, self.fail = {}, False
        self.invalid = set()

    def load(self, service, project=""):
        if (service, project) in self.invalid:
            raise AiServiceError("fake corrupt session", code="SESSION_INVALID")
        return deepcopy(self.states.get((service, project), {}))

    def save(self, service, project, state):
        if self.fail:
            raise OSError("fake disk full")
        self.states[(service, project)] = deepcopy(state)


class UIClient:
    requests = []

    def __init__(self, service, token="", **kwargs):
        self.service_url, self.token = _service_root(service), token

    def download_html(self, url, path):
        self.requests.append((self.service_url, self.token, "report", url))
        return Path(path)

    def requirements(self, identifier):
        self.requests.append((self.service_url, self.token, "requirements", identifier))
        return {"items": []}


class UIWorkflow:
    def __init__(self, client, store, *, project_id=""):
        self.client, self.store = client, store
        self.service, self.project_id = client.service_url, project_id
        self.state = store.load(self.service, project_id)
        self.event, self.log = lambda value: None, lambda value: None

    def save(self, **changes):
        state = deepcopy(self.state)
        state.update(deepcopy(changes))
        self.store.save(self.service, self.project_id, state)
        self.state = state

    def inspect(self, identifier):
        return {"job": saved_job(identifier), "diagnostic": None}

    def finish(self):
        self.save(ended=True)

    def arm_new_design(self):
        status = (self.state.get("operation") or {}).get("job", {}).get("status")
        if self.state.get("pending") or self.state.get("uncertain") or status in ("queued", "running"):
            raise ValueError("An unfinished request cannot be discarded")
        self.save(new_design=True, operation={}, success={}, local_graph=None, continuation_job_id=None,
                  ended=False, conversation=[], draft={})

    def clear_conversation(self):
        self.save(conversation=[])

    def clear_records(self):
        self.save(conversation=[], draft={}, history=[], interactions=[], success={}, operation={}, local_graph=None, new_design=True)

    def apply_current_graph(self, *, stop_event=None):
        UIClient.requests.append((self.service, self.client.token, "apply"))

    def clear_current_page(self, *, stop_event=None):
        UIClient.requests.append((self.service, self.client.token, "clear_page"))

    def revise(self, instruction, *, stop_event=None):
        UIClient.requests.append((self.service, self.client.token, "revision", instruction))
        return None

    def create(self, kind, **kwargs):
        kwargs.pop("stop_event", None)
        UIClient.requests.append((self.service, self.client.token, "create", kind, kwargs))
        return None

    def highlight(self, choice):
        UIClient.requests.append((self.service, self.client.token, "highlight", deepcopy(choice)))
        return None


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed in this test environment")
class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from ai_schematic import ui
        cls.ui = ui

    def setUp(self):
        MemorySettings.values, UIClient.requests = {}, []
        self.store = MemoryStore()
        self.patches = [patch.object(self.ui, "QSettings", MemorySettings),
                        patch.object(self.ui, "ClientWorkflow", UIWorkflow),
                        patch.object(self.ui, "ServiceClient", UIClient),
                        patch.object(self.ui.QMessageBox, "warning", return_value=None),
                        patch.object(self.ui.QMessageBox, "information", return_value=None),
                        patch.object(self.ui.QMessageBox, "question", return_value=self.ui.QMessageBox.StandardButton.Yes),
                        patch.object(self.ui.QDesktopServices, "openUrl", return_value=True),
                        patch.dict(os.environ, {"KICAD_AI_SERVICE_URL": A, "AI_EDA_API_KEY": ""})]
        for p in self.patches:
            p.start()
        self.window = self.ui.GeneratorWindow(store=self.store)

    def tearDown(self):
        self.window.task = None
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        for p in reversed(self.patches):
            p.stop()

    def seed(self, service, identifier):
        state = {"success": {"job": saved_job(identifier), "graph": {"graph_id": identifier}},
                 "operation": {"job": saved_job(identifier)},
                 "import": {"revision_supported": True},
                 "history": [{"job_id": identifier, "status": "succeeded"}]}
        self.store.states[(service, "")] = deepcopy(state)
        if self.window.workflow.service == service:
            self.window.workflow.state = deepcopy(state)
        return state

    def run_sync(self, function, message, after=None, *, require_session=True):
        if require_session and not self.window.switch_service():
            return
        value = function(SimpleNamespace(stop_event=threading.Event()))
        if after:
            self.window.invoke(lambda: after(value))

    def test_service_switch_clears_access_token_and_old_choices(self):
        self.seed(A, OLD_ID)
        self.window.token.setText("token-for-A")
        self.window.choices.addItem("A candidate")
        self.window.endpoint.setText(B)
        self.assertTrue(self.window.switch_service())
        self.assertEqual(self.window.workflow.service, B)
        self.assertEqual(self.window.workflow.client.token, "")
        self.assertEqual(self.window.token.text(), "")
        self.assertEqual(self.window.choices.count(), 0)
        self.assertNotIn(OLD_ID, self.window.target_label.text())

    def test_reload_of_other_service_does_not_reuse_previous_service_token(self):
        self.window.token.setText("token-for-A")
        self.window.endpoint.setText(B)
        self.window.reload_session()
        self.assertEqual(self.window.workflow.service, B)
        self.assertEqual(self.window.workflow.client.token, "")
        self.assertEqual(self.window.token.text(), "")

    def test_report_and_requirements_resolve_job_after_service_switch(self):
        self.seed(A, OLD_ID)
        self.seed(B, NEW_ID)
        self.window.token.setText("token-for-A")
        self.window.endpoint.setText(B)  # no editingFinished signal is emitted
        with patch.object(self.window, "launch", side_effect=self.run_sync), \
             patch.object(self.ui.QFileDialog, "getSaveFileName", return_value=("not-written.html", "")), \
             patch.object(self.window, "show_requirements_table", return_value=None):
            self.window.download_report(True)
            self.window.show_requirements()
        self.assertEqual(UIClient.requests, [
            (B, "", "report", f"/api/v1/jobs/{NEW_ID}/report"),
            (B, "", "requirements", NEW_ID),
        ])

    def test_invalid_startup_address_disables_actions_without_workflow(self):
        with patch.dict(os.environ, {"KICAD_AI_SERVICE_URL": "invalid-address"}):
            invalid = self.ui.GeneratorWindow(store=self.store)
        try:
            self.assertIsNone(invalid.workflow)
            for name in ("start", "feedback", "finish", "export"):
                self.assertFalse(invalid.buttons[name].isEnabled(), name)
        finally:
            invalid.close()
            invalid.deleteLater()

    def test_finish_remains_available_when_host_cannot_support_feedback(self):
        self.seed(A, OLD_ID)
        self.window.workflow.state["import"]["revision_supported"] = False
        self.window.render_busy()
        self.assertTrue(self.window.buttons["finish"].isEnabled())
        self.assertFalse(self.window.buttons["feedback"].isEnabled())

    def test_task_failure_cannot_leave_successful_progress(self):
        self.window.status.setText("succeeded")
        self.window.progress.setValue(1)
        deleted = []
        self.window.task = SimpleNamespace(error=AiServiceError("disconnected", code="CONNECTION_FAILED"),
                                          value=None, deleteLater=lambda: deleted.append(True))
        self.window.task_finished()
        self.assertEqual(self.window.progress.value(), 0)
        self.assertNotEqual(self.window.status.text(), "succeeded")
        self.assertEqual(deleted, [True])

    def test_history_preview_does_not_replace_pending_or_success(self):
        self.seed(A, OLD_ID)
        self.window.workflow.state["pending"] = {"request": {"request_id": "unconfirmed"}}
        before = deepcopy(self.window.workflow.state)
        label = f"{OLD_ID} · succeeded · preview"
        with patch.object(self.ui.QInputDialog, "getItem", return_value=(label, True)), \
             patch.object(self.window, "launch", side_effect=self.run_sync), \
             patch.object(self.window, "show_document", return_value=None):
            self.window.show_history()
        self.assertEqual(self.window.workflow.state, before)

    def test_draft_persistence_failure_blocks_new_submission(self):
        before = deepcopy(self.window.workflow.state)
        self.store.fail = True
        self.window.user_text.setPlainText("unsaved draft")
        self.assertTrue(self.window.persistence_error)
        self.assertFalse(self.window.buttons["start"].isEnabled())
        self.assertEqual(self.window.workflow.state, before)
        self.store.fail = False

    def test_new_design_starts_with_empty_conversation_and_inputs(self):
        self.window.user_text.setPlainText("keep old requirement")
        self.window.feedback.setPlainText("keep old feedback")
        self.window.run_name.setText("keep old task name")
        before = deepcopy(self.window.workflow.state["draft"])
        self.window.new_design()
        self.assertEqual(self.window.workflow.state["draft"], {})
        self.assertEqual(self.window.workflow.state["conversation"], [])
        self.assertTrue(self.window.workflow.state["new_design"])

    def test_feedback_resolves_service_before_reading_draft_or_writing_conversation(self):
        self.seed(A, OLD_ID)
        state_b = self.seed(B, NEW_ID)
        state_b["draft"] = {"feedback": "feedback for B"}
        self.store.states[(B, "")] = state_b
        self.window.feedback.setPlainText("draft from A")
        before_a = deepcopy(self.store.states[(A, "")])
        self.window.endpoint.setText(B)
        with patch.object(self.window, "launch", side_effect=self.run_sync):
            self.window.submit_feedback()
        self.assertEqual(self.store.states[(A, "")], before_a)
        self.assertEqual(UIClient.requests[-1], (B, "", "revision", "feedback for B"))

    def test_candidate_activation_only_fills_draft_and_highlights(self):
        self.seed(A, OLD_ID)
        choice = {"node_id": "N", "label": "inspect node N"}
        self.window.choices.addItem(choice["label"], choice)
        with patch.object(self.window, "launch", side_effect=self.run_sync):
            self.window.choices.activated.emit(0)
        self.assertEqual(self.window.feedback.toPlainText(), choice["label"])
        self.assertEqual(UIClient.requests, [])

    def test_conversation_cache_failure_does_not_interrupt_worker_cleanup(self):
        self.seed(A, OLD_ID)
        self.store.fail = True
        deleted = []
        self.window.task = SimpleNamespace(error=None, value={"delivered": True},
                                          deleteLater=lambda: deleted.append(True))
        self.window.task_finished()
        self.assertTrue(self.window.persistence_error)
        self.assertEqual(deleted, [True])
        self.assertIsNone(self.window.task)
        self.store.fail = False

    def test_three_tabs_and_escaped_conversation(self):
        self.assertEqual(self.window.tabs.count(), 3)
        self.assertFalse(self.window.tabs.tabBar().isHidden())
        self.window.append_conversation("user", "<img src='https://invalid.example/secret'>")
        self.assertIn("&lt;img", self.window.conversation.toHtml())
        self.assertIn("RF EDA", self.window.windowTitle())

    def test_clear_chat_is_persistent_and_keeps_task(self):
        self.seed(A, OLD_ID)
        self.window.append_conversation("user", "old conversation marker")
        self.window.clear_conversation()
        self.assertEqual(self.window.workflow.state["conversation"], [])
        self.assertTrue(self.window.workflow.state["success"])
        reopened = self.ui.GeneratorWindow(store=self.store)
        try:
            self.assertNotIn("old conversation marker", reopened.conversation.toPlainText())
        finally:
            reopened.close()
            reopened.deleteLater()

    def test_clear_task_records_also_clears_conversation_and_draft(self):
        self.seed(A, OLD_ID)
        self.window.user_text.setPlainText("old text")
        self.window.append_conversation("user", "old task message")
        self.window.clear_records()
        self.assertFalse(self.window.workflow.state["history"])
        self.assertFalse(self.window.workflow.state["success"])
        self.assertEqual(self.window.user_text.toPlainText(), "")
        self.assertNotIn("old task message", self.window.conversation.toPlainText())
        self.assertTrue(self.window.workflow.state["import"])

    def test_only_update_button_invokes_apply_and_cancel_does_not(self):
        self.seed(A, OLD_ID)
        self.window.render()
        with patch.object(self.window, "launch", side_effect=self.run_sync):
            with patch.object(self.ui.QMessageBox, "question", return_value=self.ui.QMessageBox.StandardButton.No):
                self.window.buttons["apply"].click()
            self.assertEqual(UIClient.requests, [])
            self.window.buttons["apply"].click()
        self.assertEqual(UIClient.requests, [(A, "", "apply")])

    def test_default_button_clears_page_without_a_graph(self):
        self.assertTrue(self.window.buttons["apply"].isEnabled())
        self.assertEqual(self.window.buttons["apply"].text(), "清空当前图页")
        with patch.object(self.window, "launch", side_effect=self.run_sync):
            self.window.buttons["apply"].click()
        self.assertEqual(UIClient.requests, [(A, "", "clear_page")])

    def test_generated_result_enables_manual_update_without_automatic_canvas_call(self):
        self.seed(A, OLD_ID)
        self.window.workflow.state.pop("import")
        self.window.task = SimpleNamespace(error=None, value={"ready": True}, deleteLater=lambda: None)
        with patch.object(self.window.workflow, "apply_current_graph") as apply:
            self.window.task_finished()
        apply.assert_not_called()
        self.assertTrue(self.window.buttons["apply"].isEnabled())
        self.assertIn("更新原理图", self.window.status.text())
        self.assertEqual(self.window.buttons["requirements"].text(), "设计指标要求")

    def test_default_window_is_a_compact_auxiliary_panel(self):
        self.assertEqual((self.window.width(), self.window.height()), (500, 740))
        self.window.show()
        self.window.resize(420, 680)
        self.app.processEvents()
        self.assertTrue(self.window.sidebar.isHidden())
        self.assertTrue(self.window.context_panel.isHidden())
        self.assertFalse(self.window.tabs.tabBar().isHidden())
        self.assertNotIn("RF RF", self.window.breadcrumb.text())
        self.assertLessEqual(self.window.minimumSizeHint().width(), 420)

    def test_feedback_is_enabled_without_canvas_snapshot(self):
        self.seed(A, OLD_ID)
        self.window.workflow.state["import"].update(revision_supported=False, readback_verified=False)
        self.window.feedback.setPlainText("检查我刚替换的器件")
        self.window.render()
        self.assertTrue(self.window.buttons["feedback"].isEnabled())
        self.assertIn("后端", self.window.selection_label.text())
        self.assertTrue(self.window.buttons["interface_help"].isEnabled())
        self.assertTrue(self.window.buttons["finish"].isEnabled())

    def test_one_start_entry_supports_word_text_and_combined_input(self):
        for path, text, kind in (("agreement.docx", "", "word"),
                                 ("agreement.docx", "保留主链路，增加本振支路", "word"),
                                 ("", "设计 S 波段低噪声接收链路", "text")):
            with self.subTest(kind=kind, text=text):
                self.window.path.setText(path)
                self.window.user_text.setPlainText(text)
                self.window.run_name.setText("接收机设计")
                self.assertTrue(self.window.buttons["start"].isEnabled())
                before = len(UIClient.requests)
                with patch.object(self.window, "launch", side_effect=self.run_sync):
                    self.window.buttons["start"].click()
                self.assertEqual(len(UIClient.requests), before + 1)
                self.assertEqual(UIClient.requests[-1][2:], ("create", kind,
                    {"path": path, "user_text": text, "run_name": "接收机设计"}))
                if path:
                    self.assertIn(path, self.window.conversation.toPlainText())
        self.assertNotIn("text", self.window.buttons)

    def test_remove_attachment_retains_text_and_uses_text_job(self):
        self.window.path.setText("agreement.docx")
        self.window.user_text.setPlainText("只用这段需求")
        self.window.remove_file()
        self.assertFalse(self.window.path.text())
        self.assertEqual(self.window.user_text.toPlainText(), "只用这段需求")
        with patch.object(self.window, "launch", side_effect=self.run_sync):
            self.window.send_message()
        self.assertEqual(UIClient.requests[-1][3], "text")

    def test_empty_or_over_limit_input_does_not_submit(self):
        self.assertFalse(self.window.buttons["start"].isEnabled())
        self.window.user_text.setPlainText("x" * 20001)
        self.assertFalse(self.window.buttons["start"].isEnabled())
        with patch.object(self.window, "launch") as launch:
            self.window.start_design()
        launch.assert_not_called()

    def test_feedback_mode_preserves_first_round_draft_and_shortcut_routes_feedback(self):
        self.window.user_text.setPlainText("first-round draft")
        self.seed(A, OLD_ID)
        self.window.render()
        self.assertEqual(self.window.input_mode.currentData(), "feedback")
        self.window.feedback.setPlainText("请调整本振支路")
        with patch.object(self.window, "launch", side_effect=self.run_sync):
            self.window.send_message()
        self.assertEqual(UIClient.requests[-1][2:], ("revision", "请调整本振支路"))
        self.assertEqual(self.window.user_text.toPlainText(), "first-round draft")
        self.assertTrue(self.window.buttons["start"].isHidden())

    def test_pending_request_disables_unified_send_and_local_import(self):
        self.window.path.setText("agreement.docx")
        self.window.workflow.state["pending"] = {"request": {"request_id": "original"}}
        self.window.render_busy()
        self.assertFalse(self.window.buttons["start"].isEnabled())
        self.assertFalse(self.window.buttons["json"].isEnabled())

    def test_sidebar_navigation_and_narrow_window_keep_artifacts_accessible(self):
        self.seed(A, OLD_ID)
        self.window.render()
        self.window.show()
        self.window.resize(800, 760)
        self.app.processEvents()
        self.assertTrue(self.window.context_panel.isHidden())
        self.window.tabs.setCurrentIndex(1)
        self.assertEqual(self.window.tabs.currentIndex(), 1)
        self.assertTrue(self.window.buttons["current_report"].isVisible())
        self.assertTrue(self.window.buttons["requirements"].isEnabled())
        self.assertLessEqual(self.window.minimumSizeHint().width(), 800)

    def test_close_stops_waiting_but_waits_for_active_canvas_transaction(self):
        event = SimpleNamespace(ignore=lambda: None, accept=lambda: self.fail("should wait for worker"))
        task = SimpleNamespace(stop_event=threading.Event())
        self.window.task, self.window.mutating = task, True
        self.window.closeEvent(event)
        self.assertFalse(task.stop_event.is_set())
        self.assertTrue(self.window.close_when_idle)
        self.window.mutating = False
        self.window.closeEvent(event)
        self.assertTrue(task.stop_event.is_set())
        self.window.task = None

    def test_bind_current_switches_project_without_touching_old_pending_request(self):
        old = {"pending": {"request": {"request_id": "original-unknown"}},
               "draft": {"feedback": "keep old draft"}}
        self.store.states[(A, "old-sheet")] = deepcopy(old)
        self.window.workflow = UIWorkflow(UIClient(A), self.store, project_id="old-sheet")
        target = {"key": "new-sheet", "screen": "new.kicad_sch"}
        module = SimpleNamespace(capture_target=lambda **kwargs: deepcopy(target))
        with patch.dict(sys.modules, {"ai_schematic.generator": module}), \
             patch.object(self.window, "launch", side_effect=self.run_sync):
            self.window.bind_current()
        self.assertEqual(self.window.workflow.project_id, "new-sheet")
        self.assertEqual(self.window.workflow.state["target"], target)
        self.assertEqual(self.store.states[(A, "old-sheet")], old)
        self.assertEqual(UIClient.requests, [])

    def test_bind_current_can_escape_corrupt_old_project_without_overwriting_it(self):
        corrupt = {"unreadable-old-record": "must remain"}
        self.store.states[(A, "old-sheet")] = deepcopy(corrupt)
        self.store.invalid.add((A, "old-sheet"))
        MemorySettings.values[self.window.project_setting(A)] = "old-sheet"
        self.window.workflow = None
        self.assertFalse(self.window.switch_service())
        self.assertTrue(self.window.persistence_error)
        target = {"key": "new-sheet", "screen": "new.kicad_sch"}
        module = SimpleNamespace(capture_target=lambda **kwargs: deepcopy(target))
        with patch.dict(sys.modules, {"ai_schematic.generator": module}), \
             patch.object(self.window, "launch", side_effect=self.run_sync):
            self.window.bind_current()
        self.assertEqual(self.window.workflow.project_id, "new-sheet")
        self.assertEqual(self.window.persistence_error, "")
        self.assertEqual(self.store.states[(A, "old-sheet")], corrupt)
        self.assertEqual(UIClient.requests, [])

    def test_bind_to_same_corrupt_project_leaves_it_blocked_and_unchanged(self):
        corrupt = {"unreadable-old-record": "must remain"}
        self.store.states[(A, "old-sheet")] = deepcopy(corrupt)
        self.store.invalid.add((A, "old-sheet"))
        self.window.workflow = None
        self.window.persistence_error = "SESSION_INVALID"
        target = {"key": "old-sheet", "screen": "old.kicad_sch"}
        module = SimpleNamespace(capture_target=lambda **kwargs: deepcopy(target))
        with patch.dict(sys.modules, {"ai_schematic.generator": module}), \
             patch.object(self.window, "launch", side_effect=self.run_sync):
            self.window.bind_current()
        self.assertIsNone(self.window.workflow)
        self.assertTrue(self.window.persistence_error)
        self.assertEqual(self.store.states[(A, "old-sheet")], corrupt)
        self.assertEqual(UIClient.requests, [])


if __name__ == "__main__":
    unittest.main()
