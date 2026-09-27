"""Choose a Word or Graph IR JSON file and generate in KiCad."""

from __future__ import annotations

from pathlib import Path
import os

from PySide6.QtCore import QObject, QThread, Signal, Slot
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
from .ai_service import DEFAULT_URL
from .input_source import InputKind, load_graph_input


class GenerationWorker(QObject):
    progress = Signal(str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, path: str, kind: InputKind, endpoint: str) -> None:
        super().__init__()
        self._path = path
        self._kind = kind
        self._endpoint = endpoint

    @Slot()
    def run(self) -> None:
        try:
            if self._kind == "json":
                self.progress.emit("正在读取本地 Graph IR JSON…")
            else:
                self.progress.emit("正在上传 Word 并等待 AI 返回 Graph IR…")
            graph = load_graph_input(self._path, self._kind, self._endpoint)
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

        central = QWidget(self)
        root = QVBoxLayout(central)

        file_group = QGroupBox("输入文件与 AI 服务")
        file_layout = QGridLayout(file_group)
        self.endpoint_edit = QLineEdit(os.environ.get("KICAD_AI_GENERATE_URL", DEFAULT_URL))
        self.endpoint_edit.setToolTip("FastAPI POST 地址，上传字段名为 file")
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
        root.addWidget(file_group)

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
        self.clear_button = QPushButton("清空日志")
        self.close_button = QPushButton("关闭")
        buttons.addStretch(1)
        buttons.addWidget(self.generate_button)
        buttons.addWidget(self.clear_button)
        buttons.addWidget(self.close_button)
        root.addLayout(buttons)

        self.setCentralWidget(central)
        self.word_button.clicked.connect(self.choose_word)
        self.json_button.clicked.connect(self.choose_json)
        self.generate_button.clicked.connect(self.start_generation)
        self.clear_button.clicked.connect(self.log.clear)
        self.close_button.clicked.connect(self.close)

    @Slot()
    def choose_word(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Word 文档",
            str(Path.home()),
            "Word files (*.docx *.doc);;All files (*)",
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

        self._set_busy(True)
        self.log.appendPlainText("\n开始生成…")
        thread = QThread(self)
        worker = GenerationWorker(
            self.path_edit.text(), self._input_kind, self.endpoint_edit.text().strip())
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.log.appendPlainText)
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
    def generation_completed(self, report: GenerationReport) -> None:
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
        self.generate_button.setEnabled(not busy and bool(self.path_edit.text()))
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
