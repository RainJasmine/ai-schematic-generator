"""Native management pages. All HTTP runs on an owned worker thread.

Credentials/drafts remain in memory; only secret-free maintenance request IDs,
payloads and task references are persisted for uncertain-response recovery.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from .ai_service import AiServiceError, ServiceClient
from .management_controller import (
    MaintenanceController, build_request, configuration_change, credential_change,
    mutation_uncertain, publication_request,
)


class JsonView(QDialog):
    def __init__(self, title, value, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(850, 650)
        layout = QVBoxLayout(self)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class _CallWorker(QObject):
    completed = Signal(object)
    failed = Signal(object)

    def __init__(self, operation):
        super().__init__()
        self.operation = operation

    @Slot()
    def run(self):
        try:
            self.completed.emit(self.operation())
        except Exception as exc:
            self.failed.emit(exc)


class _ProviderDialog(QDialog):
    def __init__(self, provider=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑厂商" if provider else "新增厂商")
        self.resize(600, 340)
        form = QFormLayout(self)
        self.inputs = {}
        provider = provider or {}
        for key, title in (("id", "厂商ID"), ("display_name", "显示名称"),
                           ("base_url", "API根地址"), ("model_name", "模型名称")):
            edit = QLineEdit(str(provider.get(key, "")))
            if key == "id":
                edit.setReadOnly(bool(provider))
            self.inputs[key] = edit
            form.addRow(title, edit)
        form.addRow("协议", QLabel("openai-compatible"))
        self.action = QComboBox()
        for title, value in (("保留已存KEY", "keep"), ("替换为新KEY", "replace"), ("清除KEY", "clear")):
            self.action.addItem(title, value)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("真实KEY不回显；替换时填写新值")
        if provider.get("credential", {}).get("action") == "replace":
            self.action.setCurrentIndex(1)
            self.key.setText(provider["credential"]["value"])
        elif provider.get("credential", {}).get("action") == "clear":
            self.action.setCurrentIndex(2)
        self.key.textEdited.connect(lambda value: self.action.setCurrentIndex(1) if value else None)
        form.addRow("KEY操作", self.action)
        form.addRow("新的模型KEY", self.key)
        self.message = QLabel()
        self.message.setWordWrap(True)
        form.addRow(self.message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _accept(self):
        try:
            value = {key: edit.text().strip() for key, edit in self.inputs.items()}
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value["id"]):
                raise ValueError("厂商ID限1～64位字母、数字、下划线或短横线")
            if not value["display_name"] or not value["base_url"] or not value["model_name"]:
                raise ValueError("请填写显示名称、API地址和模型名称")
            value.update(protocol="openai-compatible", credential=credential_change(self.action.currentData(), self.key.text()))
            self.value = value
            self.accept()
        except ValueError as exc:
            self.message.setText(str(exc))


class ManagementDialog(QDialog):
    def __init__(self, service_url, token="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("RF EDA · 配置与符号库")
        self.resize(940, 820)
        self._service_url, self._token = service_url, token
        self._thread = self._worker = None
        self._config = self._knowledge = None
        self._fields, self._providers, self._deletions = {}, {}, []
        self._config_pending = None  # May contain a KEY: memory only, never SessionStore.
        self._config_outcome_unknown = False
        self._config_attempt_prior_unknown = False
        self._queued_publish = None
        self._closed = False
        self._controller = MaintenanceController(ServiceClient(service_url, token))
        root = QVBoxLayout(self)
        root.addWidget(QLabel(f"服务：{self._controller.client.root}"))
        access = QHBoxLayout()
        access.addWidget(QLabel("独立管理令牌："))
        self.admin_key = QLineEdit()
        self.admin_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.admin_key.setPlaceholderText("未配置令牌时仅允许本机回环管理；不保存")
        access.addWidget(self.admin_key)
        refresh = QPushButton("读取配置与库状态")
        refresh.clicked.connect(self._load)
        access.addWidget(refresh)
        root.addLayout(access)
        self.status = QLabel("请读取配置；所有路径均属于服务端电脑。")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self._make_config_tab()
        self._make_knowledge_tab()
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.reject)
        root.addWidget(self.close_button)
        self._buttons = self.findChildren(QPushButton)
        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        self._update_buttons()

    def _client(self):
        return ServiceClient(self._service_url, self._token, self.admin_key.text().strip())

    def _make_config_tab(self):
        page, layout = QWidget(), QVBoxLayout()
        page.setLayout(layout)
        self.config_status = QLabel("尚未读取")
        self.config_status.setWordWrap(True)
        layout.addWidget(self.config_status)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.field_widget = QWidget()
        self.field_form = QFormLayout(self.field_widget)
        scroll.setWidget(self.field_widget)
        layout.addWidget(scroll, 1)
        providers = QGroupBox("API厂商与KEY（仅在内存编辑）")
        box = QVBoxLayout(providers)
        row = QHBoxLayout()
        self.provider_combo = QComboBox()
        row.addWidget(QLabel("使用厂商"))
        row.addWidget(self.provider_combo, 1)
        for title, action in (("新增", lambda: self._edit_provider(False)),
                              ("编辑", lambda: self._edit_provider(True)), ("删除", self._delete_provider)):
            button = QPushButton(title)
            button.setProperty("requires_config", True)
            button.clicked.connect(action)
            row.addWidget(button)
        box.addLayout(row)
        self.provider_status = QLabel("变更暂存于本窗口，保存后才更新服务端。")
        self.provider_status.setWordWrap(True)
        box.addWidget(self.provider_status)
        layout.addWidget(providers)
        secrets = QHBoxLayout()
        secrets.addWidget(QLabel("Neo4j密码"))
        self.neo_action = QComboBox()
        for title, value in (("保留", "keep"), ("替换", "replace"), ("清除", "clear")):
            self.neo_action.addItem(title, value)
        self.neo_password = QLineEdit()
        self.neo_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.neo_password.setPlaceholderText("留空保留；替换时填写新密码")
        self.neo_password.textEdited.connect(lambda value: self.neo_action.setCurrentIndex(1) if value else None)
        secrets.addWidget(self.neo_action)
        secrets.addWidget(self.neo_password, 1)
        layout.addLayout(secrets)
        actions = QHBoxLayout()
        for title, action in (("静态校验/差异", self._validate), ("仅保存", lambda: self._save_config("save")),
                              ("保存并应用", lambda: self._save_config("save_and_apply")),
                              ("应用已保存版本", lambda: self._save_config("apply")), ("模型测试", self._test_model)):
            button = QPushButton(title)
            button.setProperty("requires_config", True)
            button.clicked.connect(action)
            actions.addWidget(button)
        layout.addLayout(actions)
        history = QHBoxLayout()
        self.restore_version = QSpinBox()
        self.restore_version.setRange(0, 0)
        history.addWidget(QLabel("从历史配置创建新版本："))
        history.addWidget(self.restore_version)
        restore = QPushButton("恢复并应用")
        restore.setProperty("requires_config", True)
        restore.clicked.connect(self._restore)
        history.addWidget(restore)
        self.retry_config = QPushButton("核对/重试上次保存")
        self.retry_config.clicked.connect(self._retry_config)
        history.addWidget(self.retry_config)
        layout.addLayout(history)
        self.tabs.addTab(page, "配置与模型")

    def _make_knowledge_tab(self):
        page, layout = QWidget(), QVBoxLayout()
        page.setLayout(layout)
        info = QLabel("在配置页设置服务端 .kicad_sym 路径、递归和Neo4j目标。先暂存构建，再明确确认发布；构建不写生产数据库。")
        info.setWordWrap(True)
        layout.addWidget(info)
        self.knowledge_status = QLabel("尚未读取")
        self.knowledge_status.setWordWrap(True)
        layout.addWidget(self.knowledge_status)
        self.build_combo = QComboBox()
        self.build_combo.setEditable(True)
        self.build_combo.setPlaceholderText("选择成功构建，或输入完整构建任务ID")
        layout.addWidget(self.build_combo)
        row = QHBoxLayout()
        for name, title, action in (("refresh_knowledge", "查询实际数据库代际", self._refresh_knowledge),
                                    ("build_button", "暂存构建", self._build),
                                    ("publish_button", "确认发布", lambda: self._prepare_publish(False)),
                                    ("recover_button", "核验恢复原发布", lambda: self._prepare_publish(True)),
                                    ("retry_maintenance", "核对上次提交", self._retry_maintenance),
                                    ("report_button", "构建报告", self._maintenance_report)):
            button = QPushButton(title)
            button.clicked.connect(action)
            setattr(self, name, button)
            row.addWidget(button)
        layout.addLayout(row)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output, 1)
        self.tabs.addTab(page, "符号库与Neo4j")

    def _launch(self, title, operation, completed, *, kind="read"):
        if self._thread is not None:
            return
        self.status.setText(title)
        self._complete_callback = completed
        self._active_kind = kind
        thread, worker = QThread(self), _CallWorker(operation)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._completed)
        worker.failed.connect(self._failed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.completed.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(self._finished)
        thread.finished.connect(thread.deleteLater)
        self._thread, self._worker = thread, worker
        self._update_buttons()
        thread.start()

    @Slot(object)
    def _completed(self, result):
        self.status.setText("请求已完成，请查看实际结果。")
        try:
            self._complete_callback(result)
        except Exception as error:
            self.status.setText(f"本地结果处理未完成：{error}。请查询原任务或配置版本，勿盲目重复提交。")

    @Slot(object)
    def _failed(self, error):
        self.status.setText(f"未完成：{error}")
        if isinstance(error, AiServiceError) and error.code == "CONFIG_CONFLICT":
            self.status.setText("配置版本已变化，请重新读取并人工核对编辑，不能静默覆盖。")
        if (self._active_kind == "configuration-save" and self._config_pending and not self._config_attempt_prior_unknown
                and not mutation_uncertain(error) and getattr(error, "code", "") != "REQUEST_CONFLICT"):
            self._config_pending = None
            self._config_outcome_unknown = False
        elif self._active_kind == "configuration-save" and self._config_pending and self._config_attempt_prior_unknown:
            self.status.setText(f"本次核对未完成：{error}。此前保存结果仍未知，已保留原request_id和内存草稿。")

    @Slot()
    def _finished(self):
        self._thread = self._worker = None
        self._update_buttons()
        self._dispatch_publish()

    def _dispatch_publish(self):
        if self._closed or self._thread is not None or not self._queued_publish:
            return
        body, self._queued_publish = self._queued_publish, None
        self._submit_maintenance("/api/v1/admin/knowledge/publications", body)

    def _update_buttons(self):
        idle = self._thread is None
        for button in self._buttons:
            button.setEnabled(idle and (not button.property("requires_config") or bool(self._config)))
        self.admin_key.setEnabled(idle)
        self.tabs.setEnabled(idle)
        self.close_button.setEnabled(idle)
        self.retry_config.setEnabled(idle and self._config_pending is not None)
        pending = bool(self._controller.state.get("pending"))
        self.retry_maintenance.setEnabled(idle and pending)
        self.build_button.setEnabled(idle and bool(self._knowledge) and not pending and not self._knowledge.get("busy"))
        self.publish_button.setEnabled(idle and bool(self._knowledge) and not pending and not self._knowledge.get("busy"))
        job = self._controller.state.get("job") or {}
        self.recover_button.setEnabled(idle and not pending and bool(job.get("requires_recovery")))
        self.report_button.setEnabled(idle and bool(job.get("report_url")))

    def _load(self):
        client = self._client()
        def operation():
            config, knowledge = client.configuration(), client.knowledge()
            return config, knowledge
        self._launch("读取脱敏配置与符号库状态…", operation, self._loaded)

    def _loaded(self, values):
        self._render_config(values[0])
        self._render_knowledge(values[1])

    def _render_config(self, config):
        self._config = config
        self._providers, self._deletions = {}, []
        self.neo_password.clear()
        self.neo_action.setCurrentIndex(0)
        self.config_status.setText(f"保存 v{config['version']} · 生效 v{config['effective_version']} · "
                                  f"{'有待应用修改' if config.get('pending_application') else '应用于后续新任务'}。受管配置不回写源文件。"
                                  + (" " + config['source_error']['message'] if config.get('source_error') else ""))
        self.restore_version.setRange(0, config["version"])
        while self.field_form.rowCount():
            self.field_form.removeRow(0)
        self._fields = {}
        for path, spec in config["fields"].items():
            value = config["values"].get(path, spec.get("default"))
            kind = spec["type"]
            if kind == "boolean":
                editor = QCheckBox()
                editor.setChecked(value)
            elif kind == "integer":
                editor = QSpinBox()
                editor.setRange(spec["min"], spec["max"])
                editor.setValue(value)
            elif kind == "number":
                editor = QDoubleSpinBox()
                editor.setDecimals(4)
                editor.setRange(spec["min"], spec["max"])
                editor.setValue(value)
            elif kind == "paths":
                editor = QPlainTextEdit("\n".join(value))
                editor.setMaximumHeight(90)
                editor.setPlaceholderText("每行一个服务端符号库目录")
            else:
                editor = QLineEdit(str(value))
            editor.setToolTip(f"来源：{config.get('sources', {}).get(path, '')}\n{spec.get('description', '')}")
            self._fields[path] = (kind, editor)
            self.field_form.addRow(path, editor)
        self._refresh_provider_list(config.get("active_provider"))

    def _refresh_provider_list(self, active=None):
        if not self._config:
            return
        current = active or self.provider_combo.currentData()
        self.provider_combo.clear()
        merged = {p["id"]: p for p in self._config["providers"]}
        merged.update(self._providers)
        for identifier, provider in merged.items():
            if identifier not in self._deletions:
                suffix = "（已保存使用）" if identifier == self._config.get("active_provider") else ""
                self.provider_combo.addItem(f"{provider['display_name']} · {identifier}{suffix}", identifier)
        index = self.provider_combo.findData(current)
        self.provider_combo.setCurrentIndex(max(index, 0))
        self.provider_status.setText(f"待保存厂商编辑 {len(self._providers)} 项，删除 {len(self._deletions)} 项；KEY不回显、不写本地缓存。")

    def _edit_provider(self, existing):
        if not self._config:
            self.status.setText("请先读取配置。")
            return
        identifier = self.provider_combo.currentData()
        value = self._providers.get(identifier) or next((p for p in self._config["providers"] if p["id"] == identifier), None)
        dialog = _ProviderDialog(value if existing else None, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        provider = deepcopy(dialog.value) if accepted else None
        dialog.key.clear()
        dialog.value = None
        dialog.deleteLater()
        if accepted:
            if not existing and (provider["id"] in self._providers or any(p["id"] == provider["id"] for p in self._config["providers"])):
                self.status.setText("厂商ID已存在，请使用编辑。")
                return
            self._providers[provider["id"]] = provider
            self._refresh_provider_list(provider["id"])

    def _delete_provider(self):
        if not self._config:
            return
        identifier = self.provider_combo.currentData()
        if identifier == self._config.get("active_provider"):
            self.status.setText("请先保存切换到其他厂商，再删除当前已保存使用的厂商。")
            return
        if not identifier:
            return
        self._providers.pop(identifier, None)
        if any(p["id"] == identifier for p in self._config["providers"]):
            self._deletions.append(identifier)
        self._refresh_provider_list()

    def _body(self, intent="save_and_apply", restore=None):
        if intent == "apply" or restore is not None:
            return configuration_change(self._config, {}, intent=intent, restore_version=restore)
        values = {}
        for path, (kind, editor) in self._fields.items():
            values[path] = (editor.isChecked() if kind == "boolean" else editor.value() if kind in {"integer", "number"}
                            else [line.strip() for line in editor.toPlainText().splitlines() if line.strip()] if kind == "paths"
                            else editor.text())
        return configuration_change(self._config, values, intent=intent, providers=list(self._providers.values()),
            active_provider=self.provider_combo.currentData(), delete_provider_ids=self._deletions,
            neo4j_credential=credential_change(self.neo_action.currentData(), self.neo_password.text()), restore_version=restore)

    def _validate(self):
        try:
            body, client = self._body(), self._client()
        except ValueError as exc:
            self.status.setText(str(exc)); return
        self._launch("正在静态校验，不调用模型…", lambda: client.validate_configuration(body),
                     lambda value: JsonView("静态校验与脱敏差异（不证明已连接）", value, self).exec())

    def _save_config(self, intent, restore=None):
        if self._config_pending:
            self.status.setText("上次保存结果尚未确认，请先核对/重试原请求。")
            return
        try:
            self._config_pending = self._body(intent, restore)
        except ValueError as exc:
            self.status.setText(str(exc)); return
        self._retry_config()

    def _retry_config(self):
        if not self._config_pending:
            return
        client, body = self._client(), deepcopy(self._config_pending)
        self._config_attempt_prior_unknown = self._config_outcome_unknown
        self._config_outcome_unknown = True
        def operation():
            result = client.save_configuration(body)
            try:
                return {"result": result, "configuration": client.configuration(), "knowledge": client.knowledge()}
            except Exception as error:
                # The mutation was ACKNOWLEDGED. A follow-up GET failure must not
                # pretend that the saved configuration itself failed or is unknown.
                return {"result": result, "refresh_error": str(error)}
        def completed(value):
            self._config_pending = None
            self._config_outcome_unknown = False
            self._providers.clear()
            self.neo_password.clear()
            if value.get("configuration"):
                self._render_config(value["configuration"])
                self._render_knowledge(value["knowledge"])
            else:
                self._config = self._knowledge = None
            result = value["result"]
            self.status.setText(f"保存 v{result['version']} · 生效 v{result['effective_version']}；已受理设计仍使用原快照。"
                                + (" 后续读取失败，请重新读取配置与库状态。" if value.get("refresh_error") else ""))
        self._launch("保存/应用配置；响应不确定时保留原请求核对…", operation, completed, kind="configuration-save")

    def _restore(self):
        version = self.restore_version.value()
        if QMessageBox.question(self, "恢复配置", f"以历史 v{version} 创建并应用一个新版本？历史记录保留，仅影响后续任务。",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._save_config("save_and_apply", restore=version)

    def _test_model(self):
        if QMessageBox.question(self, "确认模型测试费用", "将对当前草稿厂商发送一次真实短请求，可能产生费用；不保存、不切换当前配置。是否继续？",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            body = {"draft": self._body(), "provider_id": self.provider_combo.currentData(), "acknowledge_cost": True}
            client = self._client()
        except ValueError as exc:
            self.status.setText(str(exc)); return
        self._launch("正在执行一次模型测试…", lambda: client.test_model(body),
                     lambda value: JsonView("模型测试结果（检查status及实际请求分项）", value, self).exec())

    def _refresh_knowledge(self):
        client = self._client()
        self._launch("读取实际数据库代际…", lambda: client.knowledge(refresh_database=True), self._render_knowledge)

    def _render_knowledge(self, knowledge):
        self._knowledge = knowledge
        target, database = knowledge.get("target", {}), knowledge.get("database", {})
        self.knowledge_status.setText(f"配置 v{knowledge['configuration_version']}（保存 v{knowledge['saved_configuration_version']}）\n"
            f"目标：{target.get('url')} / {target.get('database')}\n本地代际：{knowledge.get('local_generation_id') or '未发布'}\n"
            f"数据库：{database.get('status')} · {database.get('generation_id') or '无代际/未查询'}\n"
            f"来源：" + "；".join(source["path"] for source in knowledge.get("sources", []))
            + ("\n维护占用或发布待恢复；新设计及配置变更可能被阻止。" if knowledge.get("busy") else ""))
        current = self.build_combo.currentData()
        self.build_combo.clear()
        for job in knowledge.get("jobs", []):
            if job.get("operation") == "build" and job.get("status") == "succeeded" and job.get("publishable"):
                self.build_combo.addItem(f"{job['job_id']} · {job.get('generation_id', '')}", job["job_id"])
        index = self.build_combo.findData(current)
        if index >= 0:
            self.build_combo.setCurrentIndex(index)
        local_job = self._controller.state.get("job") or {}
        if local_job.get("status") not in {"queued", "running"} and not self._controller.state.get("pending"):
            active = next((job for job in knowledge.get("jobs", []) if job["status"] in {"queued", "running"}), None)
            if active:
                self._controller.record_job(active)
        self.output.setPlainText(json.dumps(knowledge, ensure_ascii=False, indent=2))

    def _build(self):
        try:
            body = build_request(self._knowledge or {})
        except (ValueError, KeyError) as exc:
            self.status.setText(str(exc)); return
        self._submit_maintenance("/api/v1/admin/knowledge/builds", body)

    def _submit_maintenance(self, path, body):
        self._controller.client = self._client()
        self._launch("维护请求提交中；响应丢失时按原ID核对…", lambda: self._controller.submit(path, body), self._maintenance_received)

    def _retry_maintenance(self):
        self._controller.client = self._client()
        self._launch("核对上次维护提交，保留原request_id…", self._controller.retry_pending, self._maintenance_received)

    def _maintenance_received(self, job):
        self._controller.record_job(job)
        self.output.setPlainText(json.dumps(job, ensure_ascii=False, indent=2))
        self.status.setText(f"维护 {job['job_id']} · {job['status']}；JSON/数据库/核验状态分别查看。")
        if job.get("operation") == "build" and job.get("status") == "succeeded" and job.get("publishable"):
            index = self.build_combo.findData(job["job_id"])
            if index < 0:
                self.build_combo.addItem(f"{job['job_id']} · {job.get('generation_id', '')}", job["job_id"])
                index = self.build_combo.count() - 1
            self.build_combo.setCurrentIndex(index)
        if self._knowledge:
            self._knowledge["busy"] = ([{"job_id": job["job_id"], "status": job["status"]}]
                                       if job["status"] in {"queued", "running"} or job.get("requires_recovery") else [])

    def _poll(self):
        job = self._controller.state.get("job") or {}
        if self._thread is None and not self._queued_publish and job.get("status") in {"queued", "running"}:
            self._controller.client = self._client()
            self._launch("查询维护进度…", self._controller.poll_once, self._maintenance_received)

    def _prepare_publish(self, recovery):
        if self._controller.state.get("pending"):
            self.status.setText("请先核对上次维护提交。")
            return
        try:
            if recovery:
                build_id = self._controller.recovery_build_id()
            else:
                if (self._knowledge or {}).get("busy"):
                    raise ValueError("存在维护占用，请使用原发布的核验恢复流程")
                match = re.search(r"[0-9a-f]{32}", self.build_combo.currentText())
                if not match:
                    raise ValueError("请选择或填写完整成功构建ID")
                build_id = match.group(0)
        except ValueError as exc:
            self.status.setText(str(exc)); return
        client = self._client()
        def operation():
            knowledge = client.knowledge(refresh_database=True)
            build = client.maintenance(build_id)
            body = publication_request(knowledge, build)
            return knowledge, build, body
        def confirm(value):
            knowledge, build, body = value
            target = knowledge["target"]
            message = (f"目标：{target['url']} / {target['database']}\n"
                       f"实际代际：{knowledge['database'].get('generation_id') or '空'}\n"
                       f"待发布代际：{build.get('generation_id')}\n构建：{build['job_id']}\n"
                       + ("将显式核验同一构建并完成原发布。" if recovery else "将写入并核验Neo4j，再切换后续任务JSON绑定。")
                       + "\n不执行全库清空。是否确认？")
            if QMessageBox.question(self, "确认核验恢复" if recovery else "确认发布", message,
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                # The current worker has completed; start only after its QThread finishes.
                self._queued_publish = body
                QTimer.singleShot(0, self._dispatch_publish)
        self._launch("重新核对构建、目标与真实数据库代际…", operation, confirm)

    def _maintenance_report(self):
        job = self._controller.state.get("job") or {}
        if not job.get("report_url"):
            return
        client = self._client()
        self._launch("读取构建报告JSON…", lambda: client.get_json(job["report_url"], admin=True),
                     lambda value: JsonView("符号库构建报告", value, self).exec())

    def _prepare_close(self):
        if self._closed:
            return True
        if self._thread is not None:
            self.status.setText("请等待当前HTTP请求完成后关闭；维护任务已保存，可关闭后稍后恢复。")
            return False
        if self._config_pending and QMessageBox.question(self, "保存结果尚未确认", "配置保存可能已完成。关闭将清除内存中的原请求和KEY；下次请先读取服务端版本，勿盲目新建保存。是否关闭？") != QMessageBox.StandardButton.Yes:
            return False
        self._closed = True
        self._queued_publish = None
        self._timer.stop()
        self.admin_key.clear()
        self.neo_password.clear()
        self._providers.clear()
        self._config_pending = None
        self._config_outcome_unknown = False
        self._token = ""
        self._complete_callback = None
        self._controller.client = ServiceClient(self._service_url)
        return True

    def reject(self):
        if self._prepare_close():
            super().reject()

    def closeEvent(self, event):
        if self._prepare_close():
            super().closeEvent(event)
        else:
            event.ignore()
