"""Choose a Word or Graph IR JSON file and generate in KiCad."""

from __future__ import annotations

from pathlib import Path
from html import escape
import json
import os
import uuid
from urllib.error import URLError

from PySide6.QtCore import QObject, QSettings, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QProgressBar,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .generator import GenerationReport, generate
from .generator import verify_managed_placement
from .ai_service import (DEFAULT_URL, download_graph, download_html_report,
                         get_capabilities,
                         get_requirements, get_revision_result, get_revision_state,
                         submit_revision, submit_text_job, submit_word_job,
                         wait_for_job)
from .graph_ir import GraphIR
from .session import SessionStore


class GenerationWorker(QObject):
    progress = Signal(str)
    job_state = Signal(object)
    completed = Signal(object)
    failed = Signal(object)

    def __init__(self, mode: str, path: str, endpoint: str, token: str = "",
                 instruction: str = "", run_name: str = "", resume_url: str = "",
                 request_id: str = "", current_job: dict | None = None,
                 placement: dict | None = None, continuation_job_id: str = "") -> None:
        super().__init__()
        self._mode = mode
        self._path = path
        self._endpoint = endpoint
        self._token = token
        self._instruction = instruction
        self._run_name = run_name
        self._resume_url = resume_url
        self._request_id = request_id
        self._current_job = current_job or {}
        self._placement = placement or {}
        self._continuation_job_id = continuation_job_id
        self._post_started = False
        self._accepted = False

    @Slot()
    def run(self) -> None:
        try:
            if self._mode == "json":
                self.progress.emit("正在读取本地 Graph IR JSON…")
                graph = GraphIR.load(self._path)
                self.completed.emit({"job": None,
                                     "report": generate(graph, self.progress.emit,
                                                        replace=self._placement or None,
                                                        require_complete=True),
                                     "diagnostic": None})
                return
            if self._resume_url:
                self.progress.emit("正在继续查询已有 AI 任务…")
                job = wait_for_job(self._endpoint, self._resume_url,
                                   token=self._token, on_progress=self.job_state.emit)
            elif self._mode == "revision":
                self.progress.emit("正在核对当前图纸和服务器版本…")
                base_job_id = self._current_job["job_id"]
                baseline = get_revision_state(self._endpoint, base_job_id, token=self._token)
                if baseline.get("pending_job_id"):
                    raise ValueError(f"已有反馈任务正在运行：{baseline['pending_job_id']}，请先接续该任务")
                if baseline.get("head_job_id") != base_job_id:
                    raise ValueError(f"服务器已有新版 {baseline.get('head_job_id')}，请先接续新版")
                selection = verify_managed_placement(self._placement)
                self._post_started = True
                accepted = submit_revision(
                    self._endpoint, base_job_id, self._instruction,
                    int(baseline["revision"]), baseline["graph_hash"],
                    token=self._token, request_id=self._request_id,
                    selection=selection, continuation_job_id=self._continuation_job_id)
                self._accepted = True
                self.job_state.emit(accepted)
                job = wait_for_job(self._endpoint, accepted["status_url"],
                                   token=self._token, on_progress=self.job_state.emit)
            else:
                self.progress.emit("正在提交设计任务…")
                if self._placement:
                    verify_managed_placement(self._placement)
                if self._mode == "word":
                    self._post_started = True
                    accepted = submit_word_job(
                        self._path, self._endpoint, token=self._token,
                        user_text=self._instruction, run_name=self._run_name,
                        request_id=self._request_id)
                else:
                    self._post_started = True
                    accepted = submit_text_job(
                        self._instruction, self._endpoint, token=self._token,
                        run_name=self._run_name, request_id=self._request_id)
                self._accepted = True
                self.job_state.emit(accepted)
                job = wait_for_job(self._endpoint, accepted["status_url"],
                                   token=self._token, on_progress=self.job_state.emit)
            diagnostic = None
            if job.get("revision_result_url"):
                try:
                    diagnostic = get_revision_result(
                        self._endpoint, job["revision_result_url"], token=self._token)
                except Exception as exc:
                    self.progress.emit(f"反馈诊断暂不可读取：{exc}")
            if self._mode == "inspect":
                self.completed.emit({"job": job, "report": None,
                                     "diagnostic": diagnostic, "inspect": True})
                return
            if job.get("status") != "succeeded":
                self.completed.emit({"job": job, "report": None,
                                     "diagnostic": diagnostic})
                return
            if (self._resume_url and self._current_job.get("job_id") == job.get("job_id")
                    and self._placement):
                verify_managed_placement(self._placement)
                self.completed.emit({"job": job, "report": None,
                                     "diagnostic": diagnostic})
                return
            graph = GraphIR.parse(download_graph(self._endpoint, job["result_url"],
                                                 token=self._token))
            self.progress.emit(f"已载入：{graph.summary()}")
            for warning in graph.warnings:
                self.progress.emit(f"警告：{warning}")
            replacement = self._placement or None
            report = generate(graph, self.progress.emit, replace=replacement,
                              require_complete=True)
            self.completed.emit({"job": job, "report": report,
                                 "diagnostic": diagnostic})
        except Exception as exc:  # noqa: BLE001 - errors are presented in the UI
            self.failed.emit({"message": str(exc),
                              "keep_pending": self._post_started and not self._accepted
                              and isinstance(exc.__cause__, (URLError, TimeoutError, OSError))})


class ReportDownloadWorker(QObject):
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, service_url: str, report_url: str, destination: str,
                 token: str) -> None:
        super().__init__()
        self._service_url = service_url
        self._report_url = report_url
        self._destination = destination
        self._token = token

    @Slot()
    def run(self) -> None:
        try:
            path = download_html_report(self._service_url, self._report_url,
                                        self._destination, token=self._token)
            self.completed.emit(str(path))
        except Exception as exc:  # noqa: BLE001 - errors are presented in the UI
            self.failed.emit(str(exc))


class ConnectionWorker(QObject):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, endpoint: str, token: str) -> None:
        super().__init__()
        self._endpoint = endpoint
        self._token = token

    @Slot()
    def run(self) -> None:
        try:
            self.completed.emit(get_capabilities(self._endpoint, token=self._token))
        except Exception as exc:  # noqa: BLE001 - shown in UI
            self.failed.emit(str(exc))


class RequirementsWorker(QObject):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, endpoint: str, requirements_url: str, token: str) -> None:
        super().__init__()
        self._endpoint = endpoint
        self._requirements_url = requirements_url
        self._token = token

    @Slot()
    def run(self) -> None:
        try:
            self.completed.emit(get_requirements(self._endpoint, self._requirements_url,
                                                 token=self._token))
        except Exception as exc:  # noqa: BLE001 - shown in UI
            self.failed.emit(str(exc))


class GeneratorWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AI 原理图设计助手")
        self.resize(940, 680)
        self._input_kind = "text"
        self._thread: QThread | None = None
        self._worker: GenerationWorker | None = None
        self._report_thread: QThread | None = None
        self._report_worker: ReportDownloadWorker | None = None
        self._connection_thread: QThread | None = None
        self._connection_worker: ConnectionWorker | None = None
        self._requirements_thread: QThread | None = None
        self._requirements_worker: RequirementsWorker | None = None
        self._settings = QSettings("SunvEDA", "AI Schematic Generator")
        self._store = SessionStore()
        try:
            self._state = self._store.load()
        except (OSError, ValueError, json.JSONDecodeError):
            self._state = {}
        self._state.setdefault("current_job", None)
        self._state.setdefault("last_job", None)
        self._state.setdefault("placement", None)
        self._state.setdefault("pending", None)
        self._state.setdefault("continuation_job_id", "")
        self._state.setdefault("history", [])
        self._state.setdefault("conversation", [])
        self._state.setdefault("current_service_url", self._state.get("service_url", ""))
        self._state.setdefault("last_service_url", self._state.get("service_url", ""))
        self._job_id = ""
        self._report_url = (self._state.get("last_job") or {}).get("report_url") or self._settings.value("report_url", "")
        self._last_sequence = 0
        self._last_progress_message = ""
        self._announced_success = False

        central = QWidget(self)
        root = QVBoxLayout(central)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        design_page = QWidget()
        design_layout = QVBoxLayout(design_page)
        heading = QHBoxLayout()
        title = QLabel("设计助手")
        title.setStyleSheet("font-size: 18px; font-weight: 600")
        heading.addWidget(title)
        heading.addStretch(1)
        self.current_label = QLabel("当前图：尚未生成")
        self.current_label.setWordWrap(True)
        heading.addWidget(self.current_label)
        design_layout.addLayout(heading)

        self.conversation_view = QTextBrowser()
        self.conversation_view.setOpenExternalLinks(False)
        design_layout.addWidget(self.conversation_view, 1)
        self.composer_group = QGroupBox("开始设计")
        composer = QVBoxLayout(self.composer_group)
        self.composer_hint = QLabel("输入设计要求，也可以附加 Word 技术协议。")
        self.composer_hint.setWordWrap(True)
        composer.addWidget(self.composer_hint)
        self.user_text_edit = QPlainTextEdit()
        self.user_text_edit.setPlaceholderText("描述链路结构、技术指标或需要 AI 回答的问题…")
        self.user_text_edit.setMaximumHeight(110)
        composer.addWidget(self.user_text_edit)
        attachment = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("未附加 Word；可直接发送纯文字")
        self.word_button = QPushButton("附加 Word…")
        self.text_button = QPushButton("移除附件")
        self.json_button = QPushButton("导入本地 JSON…")
        attachment.addWidget(self.path_edit, 1)
        attachment.addWidget(self.word_button)
        attachment.addWidget(self.text_button)
        attachment.addWidget(self.json_button)
        composer.addLayout(attachment)
        self.summary_label = QLabel("纯文字设计")
        self.summary_label.setWordWrap(True)
        composer.addWidget(self.summary_label)
        send_row = QHBoxLayout()
        self.run_name_edit = QLineEdit()
        self.run_name_edit.setPlaceholderText("可选任务名称")
        send_row.addWidget(self.run_name_edit, 1)
        self.generate_button = QPushButton("开始设计")
        self.generate_button.setEnabled(False)
        send_row.addWidget(self.generate_button)
        composer.addLayout(send_row)
        design_layout.addWidget(self.composer_group)
        navigation = QHBoxLayout()
        self.new_design_button = QPushButton("新建设计")
        self.new_target_button = QPushButton("新反馈目标")
        navigation.addWidget(self.new_design_button)
        navigation.addWidget(self.new_target_button)
        navigation.addStretch(1)
        design_layout.addLayout(navigation)
        self.tabs.addTab(design_page, "设计助手")

        tasks_page = QWidget()
        tasks_layout = QVBoxLayout(tasks_page)
        self.last_label = QLabel("最近任务：尚未提交")
        self.last_label.setWordWrap(True)
        tasks_layout.addWidget(self.last_label)
        self.status_label = QLabel("状态：尚未提交任务")
        self.status_label.setWordWrap(True)
        tasks_layout.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        tasks_layout.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("任务事件、符号匹配和导入结果显示在这里")
        tasks_layout.addWidget(self.log, 1)
        task_actions = QHBoxLayout()
        self.resume_button = QPushButton("继续上次任务")
        self.report_button = QPushButton("本轮 HTML 报告")
        self.current_report_button = QPushButton("当前图报告")
        self.requirements_button = QPushButton("当前需求")
        self.history_button = QPushButton("版本记录")
        for button in (self.resume_button, self.report_button,
                       self.current_report_button, self.requirements_button,
                       self.history_button):
            task_actions.addWidget(button)
        tasks_layout.addLayout(task_actions)
        self.tabs.addTab(tasks_page, "任务与版本")

        connection_page = QWidget()
        connection_layout = QVBoxLayout(connection_page)
        file_group = QGroupBox("AI 服务连接")
        file_layout = QGridLayout(file_group)
        self.endpoint_edit = QLineEdit(os.environ.get("KICAD_AI_SERVICE_URL")
                                       or os.environ.get("KICAD_AI_GENERATE_URL")
                                       or self._state.get("service_url")
                                       or self._settings.value("service_url", DEFAULT_URL))
        self.endpoint_edit.setToolTip("AI 服务根地址；插件会提交到 /api/v1/jobs")
        self.connection_button = QPushButton("检测连接")
        self.token_edit = QLineEdit(os.environ.get("AI_EDA_API_KEY", ""))
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("可选服务访问令牌")
        file_layout.addWidget(QLabel("服务："), 0, 0)
        file_layout.addWidget(self.endpoint_edit, 0, 1, 1, 2)
        file_layout.addWidget(self.connection_button, 0, 3)
        file_layout.addWidget(QLabel("令牌："), 1, 0)
        file_layout.addWidget(self.token_edit, 1, 1, 1, 3)
        connection_layout.addWidget(file_group)
        connection_layout.addWidget(QLabel("服务地址和令牌用于上传、轮询及下载。令牌不会写入本地会话文件。"))
        connection_layout.addStretch(1)
        self.tabs.addTab(connection_page, "服务连接")

        footer = QHBoxLayout()
        self.clear_button = QPushButton("清空日志")
        self.close_button = QPushButton("关闭")
        footer.addStretch(1)
        footer.addWidget(self.clear_button)
        footer.addWidget(self.close_button)
        root.addLayout(footer)

        self.setCentralWidget(central)
        self.word_button.clicked.connect(self.choose_word)
        self.connection_button.clicked.connect(self.check_connection)
        self.text_button.clicked.connect(self.choose_text)
        self.json_button.clicked.connect(self.choose_json)
        self.generate_button.clicked.connect(self.start_generation)
        self.resume_button.clicked.connect(self.resume_generation)
        self.report_button.clicked.connect(self.start_report_download)
        self.current_report_button.clicked.connect(self.start_current_report_download)
        self.new_design_button.clicked.connect(self.start_new_design)
        self.new_target_button.clicked.connect(self.clear_feedback_target)
        self.requirements_button.clicked.connect(self.show_current_requirements)
        self.history_button.clicked.connect(self.show_history)
        self.clear_button.clicked.connect(self.log.clear)
        self.close_button.clicked.connect(self.close)
        self.user_text_edit.textChanged.connect(self._update_controls)
        self._render_conversation()
        self._update_controls()

    @Slot()
    def choose_word(self) -> None:
        if self._state.get("current_job") and not self._state.get("new_design"):
            self.start_new_design()
            if not self._state.get("new_design"):
                return
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Word 文档",
            str(Path.home()),
            "Word files (*.docx);;All files (*)",
        )
        if not filename:
            return
        self._select_file(filename, "word")

    @Slot()
    def choose_json(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Graph IR JSON",
            str(Path.home()),
            "JSON files (*.json);;All files (*)",
        )
        if filename:
            self._select_file(filename, "json")

    @Slot()
    def choose_text(self) -> None:
        self._input_kind = "text"
        self.path_edit.clear()
        self.summary_label.setText("未附加文件；将发送纯文字")
        self.user_text_edit.setFocus()
        self._update_controls()

    @Slot()
    def check_connection(self) -> None:
        if self._connection_thread is not None or self._thread is not None:
            return
        thread = QThread(self)
        worker = ConnectionWorker(self.endpoint_edit.text().strip(),
                                  self.token_edit.text().strip())
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self.connection_checked)
        worker.failed.connect(self.connection_failed)
        worker.completed.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._connection_finished)
        thread.finished.connect(thread.deleteLater)
        self._connection_thread = thread
        self._connection_worker = worker
        self._set_busy(False)
        thread.start()

    @Slot(object)
    def connection_checked(self, capabilities: dict) -> None:
        mode = capabilities.get("execution_mode", "未知")
        limit = capabilities.get("max_upload_bytes")
        summary = (f"服务已连接：API {capabilities.get('api_version', '未知')}，"
                   f"Graph IR {capabilities.get('graph_schema', '未知')}，模式 {mode}")
        if isinstance(limit, int):
            summary += f"，Word 上限 {limit // 1048576} MiB"
        self.log.appendPlainText(summary)
        if mode == "offline_fixture":
            self.log.appendPlainText("当前为隔离测试模式，不代表真实 AI 设计已通过。")
        self.status_label.setText(summary)

    @Slot(str)
    def connection_failed(self, message: str) -> None:
        self.log.appendPlainText(f"连接检测失败：{message}")
        QMessageBox.warning(self, "连接检测失败", message)

    @Slot()
    def _connection_finished(self) -> None:
        self._connection_thread = None
        self._connection_worker = None
        self._update_controls()

    def _select_file(self, filename: str, kind: str) -> None:
        self._input_kind = kind
        self.path_edit.setText(filename)
        self.summary_label.setText(f"{'本地 JSON' if kind == 'json' else 'Word 上传'}：{Path(filename).name}")
        self._update_controls()
        self.log.appendPlainText(f"已选择{'本地 JSON' if kind == 'json' else 'Word'}：{filename}")

    def _append_conversation(self, role: str, message: str) -> None:
        rows = self._state.setdefault("conversation", [])
        rows.append({"role": role, "message": message[:12000]})
        del rows[:-100]
        self._render_conversation()

    def _render_conversation(self) -> None:
        rows = self._state.get("conversation") or []
        if not rows:
            self.conversation_view.setHtml(
                "<div style='margin:24px;color:#666'>"
                "<h3>从设计要求开始</h3>"
                "<p>输入链路需求或附加 Word。生成成功后，可在这里提问、补充或要求修改。</p>"
                "</div>")
            return
        cards = []
        for row in rows[-40:]:
            is_user = row.get("role") == "user"
            name = "你" if is_user else "设计助手"
            color = "#e8f2ff" if is_user else "#f2f4f5"
            body = escape(str(row.get("message", ""))).replace("\n", "<br>")
            cards.append(f"<div style='background:{color};padding:12px;margin:10px 6px;"
                         f"border-radius:8px'><b>{name}</b><p>{body}</p></div>")
        self.conversation_view.setHtml("".join(cards))
        self.conversation_view.verticalScrollBar().setValue(
            self.conversation_view.verticalScrollBar().maximum())

    @Slot()
    def start_generation(self) -> None:
        if self._thread is not None or self._report_thread is not None:
            return
        current = self._state.get("current_job")
        mode = "revision" if current and not self._state.get("new_design") and self._input_kind != "json" else self._input_kind
        if (mode == "revision" and self._state.get("current_service_url")
                and self.endpoint_edit.text().strip() != self._state["current_service_url"]):
            QMessageBox.warning(self, "服务地址不一致", "当前成功图属于其他服务，请切回原服务再提交反馈。")
            return
        instruction = self.user_text_edit.toPlainText().strip()
        if mode in {"text", "revision"} and not instruction:
            QMessageBox.warning(self, "缺少设计要求", "请先输入设计要求或反馈内容。")
            return
        if mode in {"word", "json"} and not self.path_edit.text():
            QMessageBox.warning(self, "缺少文件", "请先选择输入文件。")
            return
        if mode == "revision" and not (self._state.get("placement") or {}).get("document_fingerprint"):
            QMessageBox.warning(self, "无法安全反馈", "当前成功图没有可核验的图纸快照，无法安全更新原理图。")
            return
        self._start_worker("", mode=mode, instruction=instruction)

    @Slot()
    def resume_generation(self) -> None:
        if self._thread is not None:
            return
        last_job = self._state.get("last_job") or {}
        pending = self._state.get("pending")
        status_url = (last_job.get("status_url") or self._settings.value("status_url", "")) if not pending or last_job.get("status") in {"queued", "running"} else ""
        saved_resume = bool(status_url)
        if not status_url:
            job_id, accepted = QInputDialog.getText(self, "接续任务", "任务 ID（32 位小写十六进制）：")
            if accepted and len(job_id.strip()) == 32 and all(c in "0123456789abcdef" for c in job_id.strip()):
                status_url = f"/api/v1/jobs/{job_id.strip()}"
        if status_url:
            if saved_resume:
                self.endpoint_edit.setText(self._state.get("last_service_url") or self._settings.value("service_url", DEFAULT_URL))
            self._start_worker(status_url, mode="resume")

    def _start_worker(self, resume_url: str, *, mode: str,
                      instruction: str = "") -> None:
        self._set_busy(True)
        self._job_id = ""
        self._last_sequence = 0
        self._last_progress_message = ""
        self._announced_success = False
        self.log.appendPlainText("\n开始处理设计任务…")
        self._settings.setValue("service_url", self.endpoint_edit.text().strip())
        self._state["service_url"] = self.endpoint_edit.text().strip()
        request_id = uuid.uuid4().hex if not resume_url and mode != "json" else ""
        if request_id:
            self._state["pending"] = {"request_id": request_id, "mode": mode,
                                      "instruction": instruction,
                                      "path": self.path_edit.text(),
                                      "run_name": self.run_name_edit.text()}
        message_added = False
        if not resume_url:
            description = (f"附件：{Path(self.path_edit.text()).name}\n" if mode in {"word", "json"}
                           else "") + (instruction or "请按附件生成原理图")
            self._append_conversation("user", description)
            message_added = True
        try:
            self._save_state()
        except (OSError, ValueError) as exc:
            if message_added:
                self._state["conversation"].pop()
                self._render_conversation()
            self._set_busy(False)
            QMessageBox.warning(self, "无法保存任务状态", str(exc))
            return
        thread = QThread(self)
        worker = GenerationWorker(
            mode, self.path_edit.text(), self.endpoint_edit.text().strip(),
            self.token_edit.text().strip(), instruction,
            self.run_name_edit.text(), resume_url, request_id,
            self._state.get("current_job"), self._state.get("placement"),
            self._state.get("continuation_job_id", ""))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.log.appendPlainText)
        worker.job_state.connect(self.on_job_state)
        worker.completed.connect(self.generation_completed)
        worker.failed.connect(self.generation_failed)
        worker.completed.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._worker_finished)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        thread.start()

    @Slot(object)
    def on_job_state(self, job: dict) -> None:
        job_id = str(job.get("job_id", ""))
        if job_id:
            self._state["last_job"] = job
            self._state["last_service_url"] = self.endpoint_edit.text().strip()
            self.last_label.setText(f"最近任务：{job_id} · {job.get('status', '未知')}")
        if job_id and job_id != self._job_id:
            if job_id != self._settings.value("job_id", ""):
                self._report_url = ""
                self._settings.remove("report_url")
            self._job_id = job_id
            self._settings.setValue("job_id", job_id)
            self._settings.setValue("status_url", job.get("status_url", ""))
            self._settings.setValue("service_url", self.endpoint_edit.text().strip())
            self._settings.sync()
            self.log.appendPlainText(f"任务 ID：{job_id}")
        status = str(job.get("status", ""))
        if status in {"succeeded", "needs_attention", "failed"}:
            report_url = job.get("report_url")
            self._report_url = report_url if isinstance(report_url, str) else ""
            if self._report_url:
                self._settings.setValue("report_url", self._report_url)
                self._settings.sync()
                self.log.appendPlainText("HTML 设计报告已可下载")
            else:
                self._settings.remove("report_url")
        progress = job.get("progress") or {}
        if not isinstance(progress, dict):
            progress = {}
        stage = progress.get("stage_label") or job.get("stage") or ""
        message = progress.get("message") or ""
        details = [f"状态：{status}", str(stage), str(message)]
        subtask = progress.get("subtask")
        if isinstance(subtask, dict) and subtask.get("index") and subtask.get("total"):
            details.append(f"子任务 {subtask['index']}/{subtask['total']}")
        if isinstance(progress.get("elapsed_seconds"), (int, float)):
            details.append(f"已执行 {int(progress['elapsed_seconds'])} 秒")
        if isinstance(progress.get("queue_seconds"), (int, float)) and status == "queued":
            details.append(f"已排队 {int(progress['queue_seconds'])} 秒")
        if isinstance(progress.get("activity_elapsed_seconds"), (int, float)):
            details.append(f"当前等待 {int(progress['activity_elapsed_seconds'])} 秒")
        self.status_label.setText(" | ".join(part for part in details if part))
        first = progress.get("first_event_sequence")
        if isinstance(first, int) and first > self._last_sequence + 1:
            self.log.appendPlainText("部分较早的进度记录已超出服务保留窗口")
            self._last_sequence = first - 1
        had_new_event = False
        for event in progress.get("events") or []:
            if not isinstance(event, dict):
                continue
            sequence = event.get("sequence")
            if isinstance(sequence, int) and sequence > self._last_sequence:
                self.log.appendPlainText(str(event.get("message", "")))
                self._last_sequence = sequence
                had_new_event = True
        if message and message != self._last_progress_message:
            if not had_new_event:
                self.log.appendPlainText(f"{stage}：{message}")
            self._last_progress_message = message
        if status == "succeeded" and not self._announced_success:
            self._announced_success = True
            summary = job.get("summary") or {}
            if isinstance(summary, dict):
                for key in ("contract_warnings", "conversion_warnings"):
                    for warning in summary.get(key) or []:
                        self.log.appendPlainText("设计警告：" + json.dumps(warning, ensure_ascii=False))
            if self._worker is not None and self._worker._mode == "inspect":
                self.log.appendPlainText("任务成功；本次仅查看历史状态，不修改当前图。")
            else:
                self.log.appendPlainText("任务成功，正在下载 Graph IR…")
        elif status in {"failed", "needs_attention"}:
            self._settings.remove("status_url")
        try:
            self._save_state()
        except (OSError, ValueError) as exc:
            self.log.appendPlainText(f"任务状态未能保存：{exc}")

    @Slot()
    def start_report_download(self) -> None:
        self._start_report_download_for(self._state.get("last_job") or {},
                                        self._state.get("last_service_url", ""))

    @Slot()
    def start_current_report_download(self) -> None:
        current = self._state.get("current_job") or {}
        if not current.get("report_url"):
            return
        self._start_report_download_for(current, self._state.get("current_service_url", ""))

    def _start_report_download_for(self, job: dict, service_url: str) -> None:
        if self._report_thread is not None or self._thread is not None:
            return
        report_url = job.get("report_url")
        if not report_url:
            return
        download_dir = Path.home() / "Downloads"
        if not download_dir.is_dir():
            download_dir = Path.home()
        suggested = download_dir / f"ai-eda-report-{job.get('job_id', 'job')}.html"
        destination, _ = QFileDialog.getSaveFileName(
            self, "保存 HTML 设计报告", str(suggested), "HTML 文件 (*.html *.htm)")
        if not destination:
            return
        if Path(destination).suffix.lower() not in {".html", ".htm"}:
            destination += ".html"
        self.log.appendPlainText(f"正在下载 HTML 报告：{destination}")
        thread = QThread(self)
        worker = ReportDownloadWorker(service_url or self.endpoint_edit.text().strip(),
                                      report_url, destination, self.token_edit.text().strip())
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self.report_downloaded)
        worker.failed.connect(self.report_download_failed)
        worker.completed.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._report_worker_finished)
        thread.finished.connect(thread.deleteLater)
        self._report_thread = thread
        self._report_worker = worker
        self._set_busy(False)
        thread.start()

    @Slot()
    def start_new_design(self) -> None:
        if self._thread is not None:
            return
        if self._state.get("current_job"):
            if not (self._state.get("placement") or {}).get("document_fingerprint"):
                QMessageBox.warning(self, "无法安全替换", "当前图没有可核验的插件对象记录，请在新的空白原理图中开始设计。")
                return
            choice = QMessageBox.question(
                self, "新建设计", "新任务成功后将替换本插件上次生成的图。旧图上的本地修改会阻止替换。继续吗？")
            if choice != QMessageBox.StandardButton.Yes:
                return
        self._state["new_design"] = True
        self._state["continuation_job_id"] = ""
        self._input_kind = "text"
        self.path_edit.clear()
        self.summary_label.setText("新设计：输入文字要求，或附加 Word")
        self.user_text_edit.clear()
        self._append_conversation("assistant", "已开始新的设计。请描述目标链路，或附加 Word 技术协议。")
        self._save_state()
        self._update_controls()

    @Slot()
    def clear_feedback_target(self) -> None:
        self._state["continuation_job_id"] = ""
        self._save_state()
        self.log.appendPlainText("已清除本轮接续目标；当前成功图保持不变。")
        self._update_controls()

    @Slot()
    def show_current_requirements(self) -> None:
        current = self._state.get("current_job") or {}
        url = current.get("requirements_url")
        if not url or self._requirements_thread is not None:
            return
        thread = QThread(self)
        worker = RequirementsWorker(self._state.get("current_service_url") or
                                    self.endpoint_edit.text().strip(), url,
                                    self.token_edit.text().strip())
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self.requirements_loaded)
        worker.failed.connect(self.requirements_failed)
        worker.completed.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._requirements_finished)
        thread.finished.connect(thread.deleteLater)
        self._requirements_thread = thread
        self._requirements_worker = worker
        self._set_busy(False)
        thread.start()

    @Slot(object)
    def requirements_loaded(self, requirements: dict) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("当前已发布需求")
        dialog.resize(640, 480)
        layout = QVBoxLayout(dialog)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(json.dumps(requirements, ensure_ascii=False, indent=2))
        layout.addWidget(view)
        close = QPushButton("关闭")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    @Slot(str)
    def requirements_failed(self, message: str) -> None:
        QMessageBox.warning(self, "读取需求失败", message)

    @Slot()
    def _requirements_finished(self) -> None:
        self._requirements_thread = None
        self._requirements_worker = None
        self._update_controls()

    @Slot()
    def show_history(self) -> None:
        history = self._state.get("history") or []
        if not history:
            QMessageBox.information(self, "版本记录", "本地没有已保存的任务记录。")
            return
        labels = [f"{row.get('job_id')} · {row.get('status')} · {row.get('run_name', '')}"
                  for row in reversed(history) if row.get("job_id")]
        selected, accepted = QInputDialog.getItem(self, "版本与记录", "选择任务接续：",
                                                  labels, 0, False)
        if not accepted or not selected:
            return
        job_id = selected.split(" · ", 1)[0]
        row = next(item for item in history if item.get("job_id") == job_id)
        self.endpoint_edit.setText(row.get("service_url") or self._state.get("service_url") or DEFAULT_URL)
        self._start_worker(f"/api/v1/jobs/{job_id}", mode="inspect")

    @Slot(str)
    def report_downloaded(self, path: str) -> None:
        self.log.appendPlainText(f"HTML 报告已保存：{path}")
        try:
            os.startfile(path)
        except OSError as exc:
            QMessageBox.warning(self, "打开报告失败", f"报告已下载到 {path}\n{exc}")

    @Slot(str)
    def report_download_failed(self, message: str) -> None:
        self.log.appendPlainText(f"HTML 报告下载失败：{message}")
        QMessageBox.warning(self, "报告下载失败", message)

    @Slot()
    def _report_worker_finished(self) -> None:
        self._report_thread = None
        self._report_worker = None
        self._set_busy(self._thread is not None)

    @Slot(object)
    def generation_completed(self, outcome: dict) -> None:
        self._settings.remove("status_url")
        job = outcome.get("job")
        report: GenerationReport | None = outcome.get("report")
        diagnostic = outcome.get("diagnostic")
        if isinstance(job, dict):
            if diagnostic:
                job["diagnostic"] = diagnostic
            self._state["last_job"] = job
            history = self._state.setdefault("history", [])
            entry = next((item for item in history if item.get("job_id") == job.get("job_id")), None)
            if entry is None:
                entry = {"job_id": job.get("job_id")}
                history.append(entry)
            entry.update({"status": job.get("status"), "run_name": job.get("run_name"),
                          "revision_info": job.get("revision_info"),
                          "service_url": self.endpoint_edit.text().strip()})
        self._state["pending"] = None
        if report is not None:
            self.log.appendPlainText("\n" + report.format())
            self._append_conversation("assistant", "原理图已生成并应用到当前图纸。\n" + report.format())
            placement = {
                "document_token": report.document_token,
                "document_fingerprint": report.document_fingerprint,
                "item_ids": report.item_ids,
                "item_fingerprints": report.item_fingerprints,
                "node_item_ids": report.node_item_ids,
                "edge_item_ids": report.edge_item_ids,
            }
            if isinstance(job, dict) and job.get("status") == "succeeded":
                self._state["current_job"] = job
                self._state["current_service_url"] = self.endpoint_edit.text().strip()
                self._state["placement"] = placement
                self._state["continuation_job_id"] = ""
                self._state["new_design"] = False
                self._input_kind = "text"
                self.path_edit.clear()
                self.user_text_edit.clear()
                self.summary_label.setText("可以继续提问，或描述希望修改的部分")
            elif job is None:
                self._state["current_job"] = None
                self._state["placement"] = placement
                self._state["continuation_job_id"] = ""
                self._state["new_design"] = False
            QMessageBox.information(self, "生成完成", report.format())
        elif isinstance(job, dict) and outcome.get("inspect"):
            self.log.appendPlainText(f"已查询历史任务 {job.get('job_id')}：{job.get('status')}；当前图未改变。")
        elif isinstance(job, dict):
            status = job.get("status")
            interaction = (job.get("summary") or {}).get("interaction_status")
            message = (diagnostic or {}).get("message") or (job.get("error") or {}).get("message") or "本轮未发布新图"
            self.log.appendPlainText(f"本轮结果：{status} / {interaction or '—'}：{message}")
            self._append_conversation("assistant", f"{message}\n当前成功图保持不变。")
            if isinstance(diagnostic, dict):
                for choice in diagnostic.get("choices") or []:
                    if isinstance(choice, dict):
                        self.log.appendPlainText(f"可选补充：{choice.get('label', '')}")
                for risk in diagnostic.get("remaining_risks") or []:
                    self.log.appendPlainText(f"剩余风险：{risk}")
                if diagnostic.get("engineering_assessment"):
                    self.log.appendPlainText(f"工程判断：{diagnostic['engineering_assessment']}")
            if interaction in {"answered", "awaiting_input", "revision_failed", "budget_exhausted"} and diagnostic:
                self._state["continuation_job_id"] = job.get("job_id", "")
            QMessageBox.information(self, "本轮设计结果", message + "\n当前成功图保持不变。")
        try:
            self._save_state()
        except (OSError, ValueError) as exc:
            self.log.appendPlainText(f"设计状态未能保存：{exc}")
        self._update_controls()

    @Slot(object)
    def generation_failed(self, failure: dict) -> None:
        message = failure.get("message", "任务失败")
        if not failure.get("keep_pending"):
            self._state["pending"] = None
        else:
            pending = self._state.get("pending") or {}
            message += ("\n请求结果尚不确定，已保留请求 ID："
                        + pending.get("request_id", "")
                        + "。请先按任务 ID 查询服务端记录，不要重新提交。")
        self.log.appendPlainText(f"\n生成失败：{message}")
        self._append_conversation("assistant", f"本轮未完成：{message}")
        try:
            self._save_state()
        except (OSError, ValueError) as exc:
            self.log.appendPlainText(f"设计状态未能保存：{exc}")
        QMessageBox.critical(self, "生成失败", message)

    @Slot()
    def _worker_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._set_busy(False)

    def _save_state(self) -> None:
        self._store.save(self._state)

    @Slot()
    def _update_controls(self) -> None:
        self._set_busy(self._thread is not None)

    def _set_busy(self, busy: bool) -> None:
        idle = (not busy and self._report_thread is None
                and self._connection_thread is None and self._requirements_thread is None)
        self.word_button.setEnabled(idle)
        self.text_button.setEnabled(idle)
        self.json_button.setEnabled(idle)
        self.endpoint_edit.setEnabled(idle)
        self.token_edit.setEnabled(idle)
        self.user_text_edit.setEnabled(idle)
        self.run_name_edit.setEnabled(idle and self._input_kind != "json")
        current = bool(self._state.get("current_job")) and not self._state.get("new_design")
        self.generate_button.setText("提交补充" if current and self._state.get("continuation_job_id")
                                     and self._input_kind != "json" else
                                     "发送反馈" if current and self._input_kind != "json" else
                                     "生成本地 JSON" if self._input_kind == "json" else "开始设计")
        self.composer_group.setTitle("继续讨论当前原理图" if current else "开始设计")
        self.composer_hint.setText(
            "描述要修改的链路或提出问题。仅成功生成的新版本会替换当前图。" if current else
            "输入设计要求，也可以附加 Word 技术协议。")
        current_job = self._state.get("current_job") or {}
        current_id = str(current_job.get("job_id", ""))
        self.current_label.setText("当前图：" + (current_id[:8] + "…（已应用）" if current else
                                       "新建设计中" if self._state.get("new_design") else "尚未生成"))
        last_job = self._state.get("last_job") or {}
        last_id = str(last_job.get("job_id", ""))
        self.last_label.setText("最近任务：" + (f"{last_id} · {last_job.get('status', '未知')}"
                                              if last_id else "尚未提交"))
        self.text_button.setEnabled(idle and bool(self.path_edit.text()))
        has_input = bool(self.user_text_edit.toPlainText().strip()) or bool(self.path_edit.text())
        if current and self._input_kind != "json":
            has_input = bool(self.user_text_edit.toPlainText().strip())
        self.generate_button.setEnabled(idle and has_input and not self._state.get("pending"))
        self.resume_button.setEnabled(idle)
        self.report_button.setEnabled(idle and bool(self._report_url))
        self.current_report_button.setEnabled(idle and bool((self._state.get("current_job") or {}).get("report_url")))
        self.new_design_button.setEnabled(idle)
        self.new_target_button.setEnabled(idle and bool(self._state.get("continuation_job_id")))
        self.requirements_button.setEnabled(idle and bool((self._state.get("current_job") or {}).get("requirements_url")))
        self.history_button.setEnabled(idle and bool(self._state.get("history")))
        self.close_button.setEnabled(idle)
        self.connection_button.setEnabled(idle)
        self.progress.setRange(0, 0 if busy else 1)
        if not busy:
            self.progress.setValue(1)

    def closeEvent(self, event: object) -> None:  # noqa: N802 - Qt virtual name
        if (self._thread is not None or self._report_thread is not None
                or self._connection_thread is not None):
            QMessageBox.information(self, "正在处理", "请等待当前任务完成后再关闭窗口。")
            event.ignore()
            return
        if self._requirements_thread is not None:
            QMessageBox.information(self, "正在处理", "请等待当前任务完成后再关闭窗口。")
            event.ignore()
            return
        event.accept()
