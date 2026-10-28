"""RF EDA 原生客户端；控件只在主线程更新，HTTP/IPC 由工作线程执行。"""
from __future__ import annotations
import hashlib
from html import escape
import json
from pathlib import Path
import threading
from PySide6.QtCore import QSettings, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QFileDialog, QGridLayout, QInputDialog, QMainWindow,
    QMessageBox, QPushButton)
from .ai_service import AiServiceError, ServiceClient, WaitingStopped
from .session_store import SessionStore
from .workflow import ClientWorkflow


class ClientTask(QThread):
    message = Signal(str)
    update = Signal(object)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function, self.stop_event = function, threading.Event()
        self.value, self.error = None, None

    def run(self):
        try:
            self.value = self.function(self)
        except Exception as exc:
            self.error = exc


class GeneratorWindow(QMainWindow):
    def __init__(self, *, store=None):
        super().__init__()
        self.setWindowTitle("RF EDA · AI 设计助手")
        self.settings = QSettings("RFEDA", "SchematicAssistant")
        self.store, self.workflow = store or SessionStore(), None
        self.task, self.after_task = None, None
        self.sequences, self.buttons = {}, {}
        self.close_when_idle, self.mutating = False, False
        self.persistence_error = ""
        self.credential_origin = ""
        from .ui_workspace import build_workspace
        build_workspace(self)
        self.endpoint.editingFinished.connect(self.switch_service)
        self.user_text.textChanged.connect(self.save_drafts)
        self.feedback.textChanged.connect(self.save_drafts)
        self.run_name.textChanged.connect(self.save_drafts)
        self.choices.activated.connect(self.choose_suggestion)
        self.switch_service(initial=True)

    def button(self, key, label, callback):
        b = QPushButton(label)
        b.clicked.connect(lambda checked=False: self.invoke(callback))
        self.buttons[key] = b
        return b

    def invoke(self, callback):
        try:
            callback()
        except (AiServiceError, OSError, ValueError) as exc:
            if getattr(exc, "code", "").startswith("SESSION_"):
                self.persistence_error = str(exc)
                self.render_busy()
            self.status.setText("本次操作未完成：" + str(exc))
            QMessageBox.warning(self, "操作未完成", str(exc))

    def add_buttons(self, layout, items):
        row = QGridLayout()
        for index, (key, label, callback) in enumerate(items):
            row.addWidget(self.button(key, label, callback), index // 2, index % 2)
        layout.addLayout(row)

    def project_setting(self, service):
        return "project-" + hashlib.sha256(service.encode()).hexdigest()[:24]

    def switch_service(self, initial=False):
        if self.task:
            return False
        try:
            origin = ServiceClient(self.endpoint.text().strip()).service_url
            if self.credential_origin and self.credential_origin != origin:
                self.token.clear()
            self.credential_origin = origin
            client = ServiceClient(origin, token=self.token.text().strip())
        except (ValueError, AiServiceError) as exc:
            self.status.setText(str(exc))
            self.render_busy()
            return False
        if self.workflow and self.workflow.service == client.service_url:
            self.workflow.client = client
            return not self.persistence_error
        project = str(self.settings.value(self.project_setting(client.service_url), ""))
        try:
            workflow = ClientWorkflow(client, self.store, project_id=project)
        except (AiServiceError, ValueError, OSError) as exc:
            self.workflow = None
            self.persistence_error = str(exc)
            self.status.setText("会话未能读取，已阻止提交：" + str(exc))
            self.render_busy()
            return False
        self.workflow = workflow
        self.persistence_error = ""
        self.settings.setValue("service_url", client.service_url)
        self.render(restore_draft=True)
        return True

    def launch(self, function, message, after=None, *, require_session=True):
        if self.task:
            return
        if require_session and not self.switch_service():
            return
        if require_session and not self.workflow:
            return
        self.status.setText(message)
        self.activity_toggle.setChecked(True)
        self.log.appendPlainText(message)
        self.progress.setRange(0, 0)
        self.progress.setFormat("执行中")
        self.task, self.after_task = ClientTask(function, self), after
        self.task.message.connect(self.log.appendPlainText)
        self.task.update.connect(self.on_update)
        self.task.finished.connect(self.task_finished)
        if self.workflow:
            self.workflow.event, self.workflow.log = self.task.update.emit, self.task.message.emit
        self.render_busy()
        self.refresh_summary()
        self.task.start()

    @Slot()
    def task_finished(self):
        task, after = self.task, self.after_task
        self.task, self.after_task, self.mutating = None, None, False
        if self.workflow:
            self.workflow.event, self.workflow.log = lambda v: None, lambda v: None
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("操作已结束")
        if isinstance(task.error, WaitingStopped):
            self.status.setText("已停止客户端等待，后端继续；可恢复查询")
            self.progress.setFormat("等待已停止")
        elif task.error:
            if getattr(task.error, "uncertain", False) and self.workflow:
                try:
                    self.workflow.save(uncertain=True)
                except (AiServiceError, OSError, ValueError) as exc:
                    self.persistence_error = str(exc)
            if getattr(task.error, "code", "") in ("SESSION_CONFLICT", "SESSION_INVALID"):
                self.persistence_error = str(task.error)
            self.status.setText("本次操作未完成；任务记录已保留，请查看日志")
            self.progress.setFormat("未完成")
            self.log.appendPlainText(str(task.error))
            if not self.close_when_idle:
                QMessageBox.warning(self, "操作未完成", str(task.error))
        elif isinstance(task.value, dict) and task.value.get("delivered"):
            self.progress.setValue(1)
            self.progress.setFormat("已完整导入")
            self.status.setText("原理图已更新")
            self.append_conversation("assistant", "原理图已更新，可继续查看设计结果或提交反馈。")
        elif isinstance(task.value, dict) and task.value.get("ready"):
            self.status.setText("设计结果已生成；点击“更新原理图”应用")
            self.progress.setFormat("结果已生成")
            self.append_conversation("assistant", "设计结果已生成，可查看指标和报告，或继续完善设计。")
        elif isinstance(task.value, dict) and task.value.get("adopted"):
            self.status.setText("旧版图已完成只读核验和会话恢复；未重新运行 AI、未重画图")
            head = task.value.get("head_job_id")
            current_id = self.workflow.state.get("success", {}).get("job", {}).get("job_id")
            if head and head != current_id:
                self.log.appendPlainText("当前画布是历史成功图。服务端最新任务：" + head + "；可使用“按任务 ID 恢复”核验并更新。")
        self.render()
        self.render_busy()
        if task.error is None:
            self.show_terminal(task.value)
            if after:
                self.invoke(lambda: after(task.value))
        self.refresh_summary()
        task.deleteLater()
        if self.close_when_idle:
            self.close()

    def render_busy(self):
        busy = bool(self.task)
        for key, button in self.buttons.items():
            button.setEnabled(not busy or key in ("clear", "close"))
        self.buttons["stop"].setEnabled(busy and not self.mutating)
        self.endpoint.setEnabled(not busy)
        self.token.setEnabled(not busy)
        if busy:
            return
        if self.workflow is None or self.persistence_error:
            for key, button in self.buttons.items():
                button.setEnabled(key in ("close", "clear", "reload", "bind"))
            return
        state = self.workflow.state
        success = state.get("success") or {}
        operation = state.get("operation") or {}
        job = operation.get("job") or {}
        running = job.get("status") in ("queued", "running")
        pending = bool(state.get("pending"))
        idle = not pending and not running
        mismatch = job.get("status") == "succeeded" and job.get("job_id") != success.get("job", {}).get("job_id")
        feedback_ready = bool(success and idle and not state.get("new_design") and not mismatch)
        feedback = self.feedback.toPlainText().strip()
        self.buttons["feedback"].setEnabled(feedback_ready and not state.get("ended") and 0 < len(feedback) <= 12000)
        self.buttons["feedback"].setToolTip("提交修改要求或设计问题")
        for key in ("new_feedback", "composer_new_feedback"):
            self.buttons[key].setEnabled(feedback_ready)
        self.buttons["finish"].setEnabled(bool((success or state.get("local_graph")) and idle))
        text = self.user_text.toPlainText().strip()
        self.buttons["start"].setEnabled(bool(idle and (not success or state.get("new_design")) and (self.path.text() or text) and len(text) <= 20000))
        self.buttons["new"].setEnabled(idle)
        self.buttons["json"].setEnabled(idle)
        has_graph = bool(success.get("graph") or state.get("local_graph"))
        self.buttons["apply"].setText("更新原理图" if has_graph else "清空当前图页")
        self.buttons["apply"].setToolTip(
            "将当前设计结果应用到当前原理图页" if has_graph else
            "清空当前原理图页的全部内容，操作前需确认")
        self.buttons["apply"].setEnabled(bool(idle and not state.get("uncertain")))
        self.buttons["remove_file"].setEnabled(bool(self.path.text()))
        self.buttons["clear_records"].setEnabled(idle and not state.get("uncertain"))
        self.buttons["operation_report"].setEnabled(bool(job.get("report_url")))
        self.buttons["current_report"].setEnabled(bool(success.get("job", {}).get("report_url")))
        self.buttons["requirements"].setEnabled(bool(success))
        self.buttons["export"].setEnabled(bool(success or operation.get("candidate_graph") or state.get("local_graph")))
        self.buttons["verify"].setEnabled(bool(state.get("uncertain")))
        self.buttons["resume_id"].setEnabled(not pending)
        self.composer_changed()

    def render(self, restore_draft=False):
        if self.workflow is None:
            return
        state = self.workflow.state
        target = state.get("target") or {}
        if target:
            self.settings.setValue(self.project_setting(self.workflow.service), self.workflow.project_id)
            self.target_label.setText("上次绘图目标：" + str(target.get("screen") or target.get("screen_filename") or target["key"]))
        else:
            self.target_label.setText("生成后点击“更新原理图”应用到当前图页")
        if state.get("uncertain"):
            self.target_label.setText("上次绘图结果待确认；AI 反馈仍可继续，禁止重复写图")
        if restore_draft:
            self.choices.clear()
            self.selection_label.setText("描述修改目标，或咨询当前设计。")
            for widget, key in ((self.user_text, "user_text"), (self.feedback, "feedback")):
                widget.blockSignals(True)
                widget.setPlainText((state.get("draft") or {}).get(key, ""))
                widget.blockSignals(False)
            self.run_name.blockSignals(True)
            self.run_name.setText((state.get("draft") or {}).get("run_name", ""))
            self.run_name.blockSignals(False)
        self.render_conversation()
        self.input_mode.setCurrentIndex(1 if state.get("success") and not state.get("new_design") else 0)
        self.composer_changed()
        self.refresh_summary()
        if not self.task:
            self.render_busy()

    def save_drafts(self):
        if self.workflow:
            try:
                self.workflow.save(draft={"user_text": self.user_text.toPlainText(), "feedback": self.feedback.toPlainText(), "run_name": self.run_name.text()})
            except (AiServiceError, ValueError, OSError) as exc:
                self.persistence_error = str(exc)
                self.status.setText("草稿未能保存，暂停新提交；请重读会话：" + str(exc))
        self.refresh_summary()
        if not self.task:
            self.render_busy()

    @Slot(object)
    def on_update(self, event):
        if event.get("type") == "target":
            self.render()
            self.save_drafts()
            return
        if event.get("type") == "mutation":
            self.mutating = True
            self.buttons["stop"].setEnabled(False)
            self.status.setText("正在处理当前原理图页，请稍候…")
            return
        job = event.get("job") or {}
        p = job.get("progress") or {}
        details = [job.get("status"), p.get("stage_label") or job.get("stage"), p.get("message")]
        for key, label in (("elapsed_seconds", "执行"), ("stage_elapsed_seconds", "阶段"),
            ("activity_elapsed_seconds", "当前等待"), ("queue_seconds", "排队"), ("heartbeat_age_seconds", "心跳距今")):
            if isinstance(p.get(key), (int, float)):
                details.append(f"{label} {int(p[key])} 秒")
        if job.get("status") == "queued" and isinstance(p.get("queue_position"), int):
            details.append(f"前方等待 {max(0, p['queue_position'] - 1)} 项")
        self.status.setText(" | ".join(str(v) for v in details if v))
        self.status.setToolTip(self.status.text())
        identifier = job.get("job_id", "")
        last = self.sequences.get(identifier, 0)
        first = p.get("first_event_sequence", 0)
        if isinstance(first, int) and first > last + 1:
            self.log.appendPlainText("较早事件已超出服务保留窗口")
        for item in p.get("events") or []:
            if isinstance(item, dict) and isinstance(item.get("sequence"), int) and item["sequence"] > last:
                self.log.appendPlainText(f"{item.get('at', '')} {item.get('message', '')}")
                last = item["sequence"]
        self.sequences[identifier] = last
        self.stage_detail.setText(str(p.get("stage_label") or job.get("stage") or "等待服务事件"))
        self.refresh_summary()

    def choose_file(self, kind="word"):
        if kind == "json":
            return self.import_local_graph()
        path, _ = QFileDialog.getOpenFileName(self, "选择技术协议", str(Path.home()), "Word (*.docx)")
        if path:
            self.path.setText(path)
            self.path.setToolTip(path)
            self.render_busy()
            self.refresh_summary()

    def remove_file(self):
        self.path.clear()
        self.path.setToolTip("")
        self.refresh_summary()

    def import_local_graph(self):
        if not self.switch_service():
            return
        path, _ = QFileDialog.getOpenFileName(self, "导入本地 Graph IR", str(Path.home()), "Graph IR (*.json)")
        if path:
            self.launch(lambda task: self.workflow.create("json", path=path, stop_event=task.stop_event),
                        "正在解析并缓存本地 Graph IR…")

    def start_design(self):
        """一个首轮请求：仅附件、附件加文字、仅文字共享同一入口。"""
        if not self.switch_service():
            return
        path, text, name = self.path.text().strip(), self.user_text.toPlainText().strip(), self.run_name.text().strip()
        if not path and not text:
            QMessageBox.information(self, "需要输入", "请选择技术协议，或填写设计要求；也可以同时提供")
            return
        if len(text) > 20000:
            QMessageBox.information(self, "文字过长", "首轮设计要求最多 20,000 字符")
            return
        kind = "word" if path else "text"
        content = ("附件：" + Path(path).name + "\n" if path else "") + (text or "请根据技术协议生成设计")
        self.append_conversation("user", content)
        self.launch(lambda task: self.workflow.create(kind, path=path,
            user_text=text, run_name=name, stop_event=task.stop_event), "正在提交设计…")

    def send_message(self):
        key = "feedback" if self.input_mode.currentData() == "feedback" else "start"
        if self.buttons[key].isEnabled():
            self.buttons[key].click()

    def composer_changed(self, *_):
        feedback = self.input_mode.currentData() == "feedback"
        self.editor_stack.setCurrentIndex(1 if feedback else 0)
        self.buttons["start"].setVisible(not feedback)
        self.buttons["feedback"].setVisible(feedback)
        self.buttons["word"].setVisible(not feedback)
        self.attachment_row.setVisible(not feedback and bool(self.path.text()))
        self.run_name.setVisible(not feedback)
        self.choices.setVisible(feedback and self.choices.count() > 0)
        self.selection_label.setVisible(feedback)
        self.feedback_toolbar.setVisible(feedback)
        self.input_hint.setText("修改设计 / 咨询方案" if feedback else "仅 Word / Word＋文字 / 仅文字")
        text = self.feedback.toPlainText() if feedback else self.user_text.toPlainText()
        self.char_count.setText(f"{len(text)} / {12000 if feedback else 20000}")

    def navigation_changed(self, index):
        for i, (button, _, _) in enumerate(self.nav_buttons[:3]):
            button.setChecked(i == index)
        self.breadcrumb.setText("RF EDA · AI 设计助手")

    def refresh_summary(self):
        if not hasattr(self, "version_label"):
            return
        state = self.workflow.state if self.workflow else {}
        success = state.get("success") or {}
        job = (state.get("operation") or {}).get("job") or {}
        revision = (success.get("baseline") or {}).get("revision")
        version = f"v{revision}" if revision else ("已生成" if success else "未生成")
        applied = state.get("applied_revision")
        canvas = f"画布 v{applied}" if applied else ("画布有旧绘图记录" if state.get("import") else "画布未更新")
        self.version_label.setText("设计 " + version + " · " + canvas + (" · 已结束" if state.get("ended") else ""))
        self.design_heading.setText("继续设计" if success and not state.get("new_design") else "开始设计")
        kind = "Word＋文字" if self.path.text() and self.user_text.toPlainText().strip() else ("Word 技术协议" if self.path.text() else "文字需求")
        self.input_summary.setText("输入方式：" + kind)
        self.service_summary.setText("AI 服务\n" + (self.credential_origin or self.endpoint.text()).removeprefix("http://").removeprefix("https://"))
        self.task_summary.setText("设计版本：" + version + "\n本轮任务：" + str(job.get("job_id") or "尚未提交") + "\n" + self.status.text())
        if state.get("uncertain"):
            self.stage_detail.setText("图纸事务待核验")
        elif success and job.get("job_id") == success.get("job", {}).get("job_id") and job.get("status") == "succeeded":
            self.stage_detail.setText("设计结果已就绪，可查看报告或更新原理图")
        else:
            progress = job.get("progress") or {}
            self.stage_detail.setText(str(progress.get("stage_label") or job.get("stage") or job.get("status") or "等待开始设计"))
        self.composer_changed()

    def resizeEvent(self, event):
        super().resizeEvent(event)

    def resume(self):
        if not self.switch_service():
            return
        state = self.workflow.state
        identifier = None
        if not state.get("pending") and not state.get("operation", {}).get("job"):
            identifier, ok = QInputDialog.getText(self, "恢复任务", "本服务的 32 位任务 ID")
            if not ok or not identifier.strip():
                return
            identifier = identifier.strip()
        self.launch(lambda task: self.workflow.resume(identifier, stop_event=task.stop_event), "正在恢复原任务…")

    def submit_feedback(self):
        if not self.switch_service():
            return
        text = self.feedback.toPlainText()
        self.append_conversation("user", text)
        self.launch(lambda task: self.workflow.revise(text, stop_event=task.stop_event), "正在提交设计反馈…")

    def resume_by_id(self):
        if not self.switch_service():
            return
        if self.workflow.state.get("pending"):
            QMessageBox.information(self, "原请求尚未确认", "请先继续原冻结请求，不能用其他任务替代")
            return
        identifier, accepted = QInputDialog.getText(self, "按任务 ID 恢复", "输入本服务的 32 位任务 ID：")
        if accepted and identifier.strip():
            self.launch(lambda task: self.workflow.resume(identifier.strip(), stop_event=task.stop_event), "正在核验并恢复指定任务…")

    def read_selection(self):
        def done(value):
            s = value.get("selection") or {}
            self.selection_label.setText(f"节点 {len(s.get('node_ids', []))}，连接 {len(s.get('edge_ids', []))}；"
                + ("本地结构已修改，不能覆盖反馈" if value.get("client_graph_dirty") else "结构与基线一致"))
        self.launch(lambda task: self.workflow.selection(), "正在回读图纸及选中对象…", done)

    def show_terminal(self, value):
        if not isinstance(value, dict) or not value.get("terminal"):
            return
        job, d = value["terminal"], value.get("diagnostic") or {}
        interaction = d.get("interaction_status") or (job.get("summary") or {}).get("interaction_status")
        self.status.setText(f"本轮 {job['status']} · {interaction or '请查看诊断'}；当前成功图保留")
        self.log.appendPlainText(d.get("message") or json.dumps(job.get("error") or job.get("summary"), ensure_ascii=False))
        self.append_conversation("assistant", d.get("message") or (job.get("error") or {}).get("message") or "本轮未发布新图，当前成功图保留")
        self.choices.clear()
        for item in d.get("choices") or d.get("candidates") or []:
            self.choices.addItem(str(item.get("label") or item.get("message") or item) if isinstance(item, dict) else str(item), item)
        self.composer_changed()
        self.refresh_summary()

    def new_feedback(self):
        def done(value):
            self.feedback.clear()
            self.choices.clear()
            self.render()
        self.launch(lambda task: self.workflow.new_feedback(), "正在清除本轮接续与选择目标…", done)

    def finish_round(self):
        if not self.switch_service():
            return
        try:
            self.save_drafts()
            self.workflow.finish()
            self.status.setText("本轮已在本地结束，成果和草稿已保存；新反馈目标可继续")
            self.render()
        except ValueError as exc:
            QMessageBox.information(self, "暂不能结束", str(exc))

    def new_design(self):
        if not self.switch_service():
            return
        if self.workflow.state.get("success") or self.workflow.state.get("import") or self.workflow.state.get("operation"):
            answer = QMessageBox.question(self, "新建设计", "开始新设计将清空当前对话和输入，历史任务保留在版本记录中。是否继续？")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.workflow.arm_new_design()
        self.path.clear()
        for widget in (self.user_text, self.feedback, self.run_name):
            widget.blockSignals(True)
            widget.clear()
            widget.blockSignals(False)
        self.status.setText("已准备新设计，请上传技术协议或描述设计目标")
        self.render()

    def test_connection(self):
        def work(task):
            self.workflow.client.health()
            caps = self.workflow.client.capabilities()
            if str(caps.get("api_version", "")).split(".")[0] != "1" or caps.get("graph_schema") != "ai-eda-graph-ir/v2.0":
                raise ValueError("服务协议不兼容，需要 API 1.x 与 Graph IR v2")
            return caps
        self.launch(work, "正在检测服务和协议能力…", lambda caps: self.status.setText(
            f"服务连接正常 · API {caps.get('api_version')} · {caps.get('execution_mode')}"))

    def download_report(self, current):
        if not self.switch_service():
            return
        job = (self.workflow.state.get("success" if current else "operation") or {}).get("job") or {}
        if not job.get("report_url"):
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存 HTML 报告", str(Path.home() / f"rf-eda-{job['job_id']}.html"), "HTML (*.html)")
        if path:
            if Path(path).suffix.lower() not in (".html", ".htm"):
                path += ".html"
            self.launch(lambda task: self.workflow.client.download_html(job["report_url"], path), "正在下载报告…",
                lambda saved: QDesktopServices.openUrl(QUrl.fromLocalFile(str(saved))))

    def show_document(self, title, value):
        from .management_ui import JsonView
        JsonView(title, value, self).exec()

    def show_requirements(self):
        if not self.switch_service():
            return
        job = self.workflow.state.get("success", {}).get("job")
        if job:
            self.launch(lambda task: self.workflow.client.requirements(job["job_id"]), "正在读取设计指标要求…",
                self.show_requirements_table)

    def show_history(self):
        if not self.switch_service():
            return
        rows = {row["job_id"]: row for row in [*(self.workflow.state.get("history") or []),
            *(self.workflow.state.get("interactions") or [])] if row.get("job_id")}
        if not rows:
            QMessageBox.information(self, "版本记录", "当前服务、图纸没有本地任务记录；可使用任务 ID 恢复查询")
            return
        labels = [f"{row['job_id']} · {row.get('status', 'succeeded')} · {row.get('run_name') or ''}" for row in reversed(list(rows.values()))]
        label, accepted = QInputDialog.getItem(self, "版本记录", "选择要查看的设计版本", labels, 0, False)
        if not accepted:
            return
        identifier = label.split(" · ", 1)[0]
        def done(value):
            self.status.setText("已读取历史任务；当前图、待确认请求及恢复目标保持不变")
            self.show_document("历史任务预览", value)
        self.launch(lambda task: self.workflow.inspect(identifier), "正在只读查询历史任务…", done)

    def export_graph(self):
        if not self.switch_service():
            return
        state = self.workflow.state
        raw = state.get("operation", {}).get("candidate_graph") or state.get("success", {}).get("graph") or state.get("local_graph")
        path, _ = QFileDialog.getSaveFileName(self, "保存 Graph IR", str(Path.home() / "graph-ir.json"), "JSON (*.json)")
        if raw is not None and path:
            Path(path).write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    def open_management(self, page=0):
        if not self.switch_service():
            return
        from .management_ui import ManagementDialog
        dialog = ManagementDialog(self.workflow.service, self.token.text().strip(), self)
        dialog.setStyleSheet(self.styleSheet())
        dialog.tabs.setCurrentIndex(page)
        dialog.exec()

    def show_requirements_table(self, baseline):
        from .requirements_ui import RequirementsDialog
        dialog = RequirementsDialog(baseline, self)
        dialog.setStyleSheet(self.styleSheet())
        dialog.exec()

    def show_interface_help(self):
        QMessageBox.information(self, "原理图操作说明",
            "更新原理图：将当前设计结果应用到活动图页，替换该页原有内容。\n\n"
            "清空当前图页：尚无设计结果时，可单独清空活动图页。\n\n"
            "执行前请保存需要保留的修改；操作完成后可在 KiCad 中撤销。")

    def update_schematic(self):
        if not self.switch_service():
            return
        has_graph = bool((self.workflow.state.get("success") or {}).get("graph")
                         or self.workflow.state.get("local_graph"))
        if not has_graph:
            answer = QMessageBox.question(
                self, "清空当前图页",
                "将删除当前原理图页的全部内容。请先保存需要保留的修改。是否清空？")
            if answer == QMessageBox.StandardButton.Yes:
                self.launch(lambda task: self.workflow.clear_current_page(
                    stop_event=task.stop_event), "正在清空当前原理图页…")
            return
        answer = QMessageBox.question(self, "更新原理图", "将使用当前设计结果替换当前原理图页的全部内容。请先保存需要保留的修改。是否继续？")
        if answer == QMessageBox.StandardButton.Yes:
            self.launch(lambda task: self.workflow.apply_current_graph(stop_event=task.stop_event), "正在更新当前原理图页…")

    def clear_conversation(self):
        if not self.switch_service():
            return
        self.workflow.clear_conversation()
        self.render_conversation()
        self.status.setText("对话已清空并保存；任务和画布未删除")

    def clear_records(self):
        if not self.switch_service():
            return
        answer = QMessageBox.question(self, "清除任务记录", "清除本会话的对话、输入、任务和版本记录；保留当前原理图和服务端文件。是否继续？")
        if answer == QMessageBox.StandardButton.Yes:
            self.workflow.clear_records()
            self.path.clear()
            self.choices.clear()
            self.log.clear()
            self.sequences.clear()
            self.render(restore_draft=True)
            self.status.setText("本会话任务记录已清除；画布和后端文件保留")

    def verify_canvas(self):
        if not self.switch_service():
            return
        answer = QMessageBox.question(self, "处理未确认绘图", "上次原理图操作结果待确认。请先在 KiCad 中检查并结束或撤销该操作，必要时重新打开原理图编辑器。处理完成后，是否允许再次更新原理图？")
        if answer == QMessageBox.StandardButton.Yes:
            self.workflow.permit_full_page_retry(user_confirmed=True)
            self.status.setText("已恢复更新入口，可点击“更新原理图”继续")
            self.render()

    def recover_legacy(self):
        if not self.switch_service():
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择旧版本的 session.json", str(Path.home()), "会话 JSON (*.json)")
        if not path:
            return
        answer = QMessageBox.question(self, "恢复旧版会话", "将从同一服务读取旧成功任务，并核验当前图纸中记录的对象和连接。不会重跑 AI 或重新画图。请确认已切回对应图纸，继续吗？")
        if answer == QMessageBox.StandardButton.Yes:
            self.launch(lambda task: self.workflow.recover_legacy(path, stop_event=task.stop_event), "正在只读核验旧版图纸和任务…")

    def reload_session(self):
        if self.task:
            return
        try:
            origin = ServiceClient(self.endpoint.text().strip()).service_url
            if self.credential_origin and self.credential_origin != origin:
                self.token.clear()
            self.credential_origin = origin
            client = ServiceClient(origin, token=self.token.text().strip())
            project = str(self.settings.value(self.project_setting(client.service_url), ""))
            self.workflow = ClientWorkflow(client, self.store, project_id=project)
            self.persistence_error = ""
            self.render(restore_draft=True)
            self.status.setText("已重读当前服务的会话；请核对任务后恢复")
        except (AiServiceError, OSError, ValueError) as exc:
            self.persistence_error = str(exc)
            self.status.setText("会话仍无法安全读取：" + str(exc))
            self.render_busy()

    def bind_current(self):
        if self.task:
            return
        origin = ServiceClient(self.endpoint.text().strip()).service_url
        if self.credential_origin and self.credential_origin != origin:
            self.token.clear()
        self.credential_origin = origin
        client = ServiceClient(origin, token=self.token.text().strip())
        from .generator import capture_target
        def done(target):
            workflow = ClientWorkflow(client, self.store, project_id=target["key"])
            workflow.save(target=target)
            self.workflow, self.persistence_error = workflow, ""
            self.settings.setValue("service_url", origin)
            self.render(restore_draft=True)
            self.status.setText("已切换到当前图纸的独立会话；原图纸任务与记录保留")
        self.launch(lambda task: capture_target(readback=False), "正在读取当前图纸身份…", done, require_session=False)

    def choose_suggestion(self, index):
        if not self.switch_service():
            return
        choice = self.choices.itemData(index)
        label = self.choices.itemText(index)
        if label:
            self.feedback.setPlainText(label)
        self.status.setText("建议已填入反馈草稿，可编辑后提交")

    def append_conversation(self, role, content):
        if not self.workflow or not str(content).strip():
            return
        rows = list(self.workflow.state.get("conversation") or [])
        rows.append({"role": role, "content": str(content)[:12000]})
        try:
            self.workflow.save(conversation=rows[-100:])
        except (AiServiceError, OSError, ValueError) as exc:
            self.persistence_error = str(exc)
            self.log.appendPlainText("对话记录未保存，请重读会话后核对任务：" + str(exc))
            self.render_busy()
            return
        self.render_conversation()

    def render_conversation(self):
        rows = self.workflow.state.get("conversation") or []
        if not rows:
            self.conversation.setHtml("<div style='padding:18px;color:#dfe7ee'>"
                "<h3>从技术协议或设计目标开始</h3>"
                "<p style='color:#99aabd'>上传 Word，或描述你的设计目标。<br>附件与补充文字会一起用于同一项设计。</p></div>")
            return
        cards = []
        for row in rows[-30:]:
            user = row.get("role") == "user"
            bg, fg = ("#242e24", "#e1ebd8") if user else ("#191f26", "#e0e7ed")
            text = escape(str(row.get("content", ""))).replace("\n", "<br>")
            # QTextDocument 支持 table 背景，避免主题 Highlight 色铺满对话区。
            cards.append(f"<table width='100%' cellspacing='0' cellpadding='12' bgcolor='{bg}'>"
                f"<tr><td style='color:{fg}'><b>{'你' if user else 'RF EDA · 设计助手'}</b>"
                f"<p>{text}</p></td></tr></table><p style='font-size:4px'>&nbsp;</p>")
        self.conversation.setHtml("".join(cards))
        self.conversation.verticalScrollBar().setValue(self.conversation.verticalScrollBar().maximum())

    def stop_waiting(self):
        if self.task and not self.mutating:
            self.task.stop_event.set()
            self.status.setText("正在停止客户端等待，后端继续；当前请求结束后退出")
            self.buttons["stop"].setEnabled(False)

    def closeEvent(self, event):
        self.save_drafts()
        if self.task:
            self.close_when_idle = True
            self.stop_waiting()
            self.status.setText("任务已保存，当前操作结束后关闭；后端任务继续")
            event.ignore()
        else:
            event.accept()
