"""Choose a Word or Graph IR JSON file and generate in KiCad."""

from __future__ import annotations

from pathlib import Path
import json
import os

from PySide6.QtCore import QObject, QSettings, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from .generator import GenerationReport, generate
from .ai_service import DEFAULT_URL, resume_job
from .graph_ir import GraphIR
from .input_source import InputKind, load_graph_input


class GenerationWorker(QObject):
    progress = Signal(str)
    job_state = Signal(object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, path: str, kind: InputKind, endpoint: str, token: str = "",
                 user_text: str = "", run_name: str = "", resume_url: str = "") -> None:
        super().__init__()
        self._path = path
        self._kind = kind
        self._endpoint = endpoint
        self._token = token
        self._user_text = user_text
        self._run_name = run_name
        self._resume_url = resume_url

    @Slot()
    def run(self) -> None:
        try:
            if self._resume_url:
                self.progress.emit("正在继续查询已有 AI 任务…")
                graph = GraphIR.parse(resume_job(self._endpoint, self._resume_url,
                                                 token=self._token,
                                                 on_progress=self.job_state.emit))
            elif self._kind == "json":
                self.progress.emit("正在读取本地 Graph IR JSON…")
                graph = load_graph_input(self._path, self._kind, self._endpoint)
            else:
                self.progress.emit("正在提交 Word 设计任务…")
                graph = load_graph_input(self._path, self._kind, self._endpoint,
                                         token=self._token, user_text=self._user_text,
                                         run_name=self._run_name,
                                         on_progress=self.job_state.emit)
            self.progress.emit(f"已载入：{graph.summary()}")
            for warning in graph.warnings:
                self.progress.emit(f"警告：{warning}")
            self.completed.emit(generate(graph, self.progress.emit))
        except Exception as exc:  # noqa: BLE001 - errors are presented in the UI
            self.failed.emit(str(exc))


class GeneratorWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AI Schematic Generator")
        self.resize(720, 520)
        self._input_kind: InputKind = "word"
        self._thread: QThread | None = None
        self._worker: GenerationWorker | None = None
        self._settings = QSettings("SunvEDA", "AI Schematic Generator")
        self._job_id = ""
        self._last_sequence = 0
        self._last_progress_message = ""
        self._announced_success = False

        central = QWidget(self)
        root = QVBoxLayout(central)

        file_group = QGroupBox("输入文件与 AI 服务")
        file_layout = QGridLayout(file_group)
        self.endpoint_edit = QLineEdit(os.environ.get("KICAD_AI_SERVICE_URL")
                                       or os.environ.get("KICAD_AI_GENERATE_URL")
                                       or self._settings.value("service_url", DEFAULT_URL))
        self.endpoint_edit.setToolTip("AI 服务根地址；插件会提交到 /api/v1/jobs")
        self.token_edit = QLineEdit(os.environ.get("AI_EDA_API_KEY", ""))
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("可选服务访问令牌")
        self.user_text_edit = QLineEdit()
        self.user_text_edit.setPlaceholderText("可选设计补充要求")
        self.run_name_edit = QLineEdit()
        self.run_name_edit.setPlaceholderText("可选任务名称")
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("请选择 Word 或 Graph IR JSON 文件")
        self.word_button = QPushButton("选择 Word…")
        self.json_button = QPushButton("选择 JSON…")
        self.summary_label = QLabel("尚未选择文件")
        self.summary_label.setWordWrap(True)
        file_layout.addWidget(QLabel("服务："), 0, 0)
        file_layout.addWidget(self.endpoint_edit, 0, 1, 1, 3)
        file_layout.addWidget(QLabel("文件："), 1, 0)
        file_layout.addWidget(self.path_edit, 1, 1)
        file_layout.addWidget(self.word_button, 1, 2)
        file_layout.addWidget(self.json_button, 1, 3)
        file_layout.addWidget(self.summary_label, 2, 1, 1, 3)
        file_layout.addWidget(QLabel("令牌："), 3, 0)
        file_layout.addWidget(self.token_edit, 3, 1, 1, 3)
        file_layout.addWidget(QLabel("补充要求："), 4, 0)
        file_layout.addWidget(self.user_text_edit, 4, 1, 1, 3)
        file_layout.addWidget(QLabel("任务名称："), 5, 0)
        file_layout.addWidget(self.run_name_edit, 5, 1, 1, 3)
        root.addWidget(file_group)

        self.status_label = QLabel("状态：尚未提交任务")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("符号匹配、跳过项和生成结果会显示在这里")
        root.addWidget(QLabel("生成日志："))
        root.addWidget(self.log, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        root.addWidget(self.progress)

        buttons = QHBoxLayout()
        self.generate_button = QPushButton("上传并生成到当前原理图")
        self.generate_button.setEnabled(False)
        self.resume_button = QPushButton("继续上次任务")
        self.resume_button.setEnabled(bool(self._settings.value("status_url", "")))
        self.clear_button = QPushButton("清空日志")
        self.close_button = QPushButton("关闭")
        buttons.addStretch(1)
        buttons.addWidget(self.generate_button)
        buttons.addWidget(self.resume_button)
        buttons.addWidget(self.clear_button)
        buttons.addWidget(self.close_button)
        root.addLayout(buttons)

        self.setCentralWidget(central)
        self.word_button.clicked.connect(self.choose_word)
        self.json_button.clicked.connect(self.choose_json)
        self.generate_button.clicked.connect(self.start_generation)
        self.resume_button.clicked.connect(self.resume_generation)
        self.clear_button.clicked.connect(self.log.clear)
        self.close_button.clicked.connect(self.close)

    @Slot()
    def choose_word(self) -> None:
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

    def _select_file(self, filename: str, kind: InputKind) -> None:
        self._input_kind = kind
        self.path_edit.setText(filename)
        self.summary_label.setText(f"{'本地 JSON' if kind == 'json' else 'Word 上传'}：{Path(filename).name}")
        self.endpoint_edit.setEnabled(kind == "word")
        self.generate_button.setText(
            "生成到当前原理图" if kind == "json" else "上传并生成到当前原理图")
        self.generate_button.setEnabled(True)
        self.log.appendPlainText(f"已选择{'本地 JSON' if kind == 'json' else 'Word'}：{filename}")

    @Slot()
    def start_generation(self) -> None:
        if not self.path_edit.text() or self._thread is not None:
            return
        self._start_worker("")

    @Slot()
    def resume_generation(self) -> None:
        if self._thread is not None:
            return
        status_url = self._settings.value("status_url", "")
        if status_url:
            self.endpoint_edit.setText(self._settings.value("service_url", DEFAULT_URL))
            self._start_worker(status_url)

    def _start_worker(self, resume_url: str) -> None:
        self._set_busy(True)
        self._job_id = ""
        self._last_sequence = 0
        self._last_progress_message = ""
        self._announced_success = False
        self.log.appendPlainText("\n开始生成…")
        self._settings.setValue("service_url", self.endpoint_edit.text().strip())
        thread = QThread(self)
        worker = GenerationWorker(
            self.path_edit.text(), self._input_kind, self.endpoint_edit.text().strip(),
            self.token_edit.text().strip(), self.user_text_edit.text(),
            self.run_name_edit.text(), resume_url)
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
        if job_id and job_id != self._job_id:
            self._job_id = job_id
            self._settings.setValue("job_id", job_id)
            self._settings.setValue("status_url", job.get("status_url", ""))
            self._settings.setValue("service_url", self.endpoint_edit.text().strip())
            self._settings.sync()
            self.log.appendPlainText(f"任务 ID：{job_id}")
        status = str(job.get("status", ""))
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
            self.log.appendPlainText("任务成功，正在下载 Graph IR…")
        elif status in {"failed", "needs_attention"}:
            self._settings.remove("status_url")
            self._settings.remove("job_id")

    @Slot(object)
    def generation_completed(self, report: GenerationReport) -> None:
        self._settings.remove("status_url")
        self._settings.remove("job_id")
        self.log.appendPlainText("\n" + report.format())
        QMessageBox.information(self, "生成完成", report.format())

    @Slot(str)
    def generation_failed(self, message: str) -> None:
        self.log.appendPlainText(f"\n生成失败：{message}")
        QMessageBox.critical(self, "生成失败", message)

    @Slot()
    def _worker_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self.word_button.setEnabled(not busy)
        self.json_button.setEnabled(not busy)
        self.endpoint_edit.setEnabled(not busy and self._input_kind == "word")
        self.token_edit.setEnabled(not busy)
        self.user_text_edit.setEnabled(not busy and self._input_kind == "word")
        self.run_name_edit.setEnabled(not busy and self._input_kind == "word")
        self.generate_button.setEnabled(not busy and bool(self.path_edit.text()))
        self.resume_button.setEnabled(not busy and bool(self._settings.value("status_url", "")))
        self.close_button.setEnabled(not busy)
        self.progress.setRange(0, 0 if busy else 1)
        if not busy:
            self.progress.setValue(1)

    def closeEvent(self, event: object) -> None:  # noqa: N802 - Qt virtual name
        if self._thread is not None:
            QMessageBox.information(self, "正在生成", "请等待当前生成任务完成后再关闭窗口。")
            event.ignore()
            return
        event.accept()
