"""Pure management request/recovery rules, independent of Qt widgets."""
from __future__ import annotations

from copy import deepcopy
import math
import re
import uuid

from .ai_service import AiServiceError
from .session_store import SessionConflict, SessionStore


def credential_change(action: str, value: str = "") -> dict:
    if action not in {"keep", "replace", "clear"}:
        raise ValueError("凭据操作应为保留、替换或清除")
    if action == "replace":
        if not value.strip() or value.startswith(("***", "•••")):
            raise ValueError("替换凭据时请填写真实新值，不能使用掩码")
        return {"action": action, "value": value}
    return {"action": action}


def configuration_change(config: dict, values: dict, *, intent: str = "save_and_apply",
                         providers: list[dict] | None = None, active_provider: str | None = None,
                         delete_provider_ids: list[str] | None = None, neo4j_credential: dict | None = None,
                         restore_version: int | None = None, request_id: str | None = None) -> dict:
    if not config or not isinstance(config.get("version"), int):
        raise ValueError("请先读取配置")
    body = {"request_id": request_id or SessionStore.new_request_id("config"),
            "base_version": config["version"], "intent": intent}
    if intent == "apply":
        return body  # Applying a saved version must not silently include edits.
    if restore_version is not None:
        if not isinstance(restore_version, int) or not 0 <= restore_version <= config["version"]:
            raise ValueError("恢复版本不在现有历史范围内")
        return {**body, "restore_version": restore_version}
    changes = {}
    for path, value in values.items():
        spec = config["fields"].get(path)
        if not spec:
            raise ValueError(f"服务未声明可编辑字段：{path}")
        kind = spec["type"]
        valid = (type(value) is bool if kind == "boolean" else
                 type(value) is int if kind == "integer" else
                 type(value) in (int, float) and math.isfinite(value) if kind == "number" else
                 isinstance(value, str) if kind == "string" else
                 isinstance(value, list) and all(isinstance(p, str) and p.strip() for p in value))
        if not valid or kind in {"integer", "number"} and not spec["min"] <= value <= spec["max"]:
            raise ValueError(f"字段类型或范围无效：{path}")
        if value != config["values"].get(path):
            changes[path] = value
    body["changes"] = changes
    if providers:
        body["provider_updates"] = deepcopy(providers)
    if delete_provider_ids:
        body["delete_provider_ids"] = list(delete_provider_ids)
    if active_provider:
        if active_provider in (delete_provider_ids or []):
            raise ValueError("当前所选厂商不能同时删除")
        body["active_provider"] = active_provider
    if neo4j_credential and neo4j_credential.get("action") != "keep":
        body["neo4j_credential"] = deepcopy(neo4j_credential)
    return body


def build_request(knowledge: dict) -> dict:
    if knowledge.get("busy"):
        raise ValueError("已有维护占用或待恢复发布，请先完成核验")
    return {"request_id": SessionStore.new_request_id("build"),
            "configuration_version": knowledge["configuration_version"]}


def publication_request(knowledge: dict, build: dict) -> dict:
    if any(row.get("status") in {"queued", "running"} for row in knowledge.get("busy", [])):
        raise ValueError("已有维护作业运行中，请等待结束")
    if (build.get("operation") != "build" or build.get("status") != "succeeded"
            or not build.get("publishable")):
        raise ValueError("需要无错误且可发布的成功构建")
    if (build.get("configuration_version") != knowledge["configuration_version"]
            or build.get("target_fingerprint") != knowledge["target_fingerprint"]):
        raise ValueError("构建配置或目标已变化，请重新构建")
    database = knowledge.get("database") or {}
    if database.get("status") != "checked":
        raise ValueError("必须先读取真实数据库活动代际，不能用未查询代际发布")
    if not re.fullmatch(r"[0-9a-f]{32}", str(build.get("job_id", ""))):
        raise ValueError("构建任务ID无效")
    return {"request_id": SessionStore.new_request_id("publish"),
            "configuration_version": knowledge["configuration_version"],
            "build_job_id": build["job_id"], "target_fingerprint": knowledge["target_fingerprint"],
            "base_generation_id": database.get("generation_id"), "confirm_publish": True}


def mutation_uncertain(error: Exception) -> bool:
    return not isinstance(error, AiServiceError) or error.status is None or error.status >= 500 or error.status < 400


class MaintenanceController:
    """Persist only secret-free maintenance operations; retry uncertain POSTs exactly."""
    namespace = "__maintenance__"

    def __init__(self, client, store: SessionStore | None = None):
        self.client, self.store = client, store or SessionStore()
        self.state = self.store.load(client.root, self.namespace)

    def submit(self, path: str, payload: dict) -> dict:
        if path not in {"/api/v1/admin/knowledge/builds", "/api/v1/admin/knowledge/publications"}:
            raise ValueError("仅允许登记符号库构建和发布请求")
        candidate = {"path": path, "payload": deepcopy(payload)}
        attempt_id, previous_unknown = uuid.uuid4().hex, False
        def claim(state):
            nonlocal previous_unknown
            pending = state.get("pending")
            if pending and pending != candidate:
                raise SessionConflict()
            if not pending and (state.get("job") or {}).get("status") in {"queued", "running"}:
                raise ValueError("已有维护任务运行，请先继续查询")
            previous_unknown = bool(pending and state.get("pending_outcome_unknown"))
            state["pending"] = deepcopy(candidate)
            # Persist BEFORE sending. A crash between send and response therefore
            # never leaves a journal falsely claiming that nothing was submitted.
            state["pending_outcome_unknown"] = True
            state["pending_attempt_id"] = attempt_id
        # Every sender must claim under one OS lock, including retries; a failed
        # earlier disk save can never be bypassed by an in-memory pending object.
        self.state = self.store.mutate(self.client.root, self.namespace, claim)
        try:
            job = self.client.post_json(path, payload, admin=True)
        except Exception as error:
            if not previous_unknown and not mutation_uncertain(error) and getattr(error, "code", "") != "REQUEST_CONFLICT":
                def reject(state):
                    if state.get("pending") == candidate and state.get("pending_attempt_id") == attempt_id:
                        state.pop("pending", None)
                        state.pop("pending_outcome_unknown", None)
                        state.pop("pending_attempt_id", None)
                self.state = self.store.mutate(self.client.root, self.namespace, reject)
            raise
        operation = "build" if path.endswith("/builds") else "publish"
        if (not isinstance(job, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(job.get("job_id", "")))
                or job.get("operation") != operation
                or job.get("status") not in {"queued", "running", "succeeded", "failed", "needs_attention"}):
            raise AiServiceError("维护受理响应不完整；保留原请求以便核对。", code="INVALID_RESPONSE")
        def acknowledge(state):
            if state.get("pending") != candidate:
                if state.get("last_request") == candidate and state.get("last_request_job_id") == job["job_id"]:
                    return
                raise SessionConflict()
            state.pop("pending", None)
            state.pop("pending_outcome_unknown", None)
            state.pop("pending_attempt_id", None)
            state["last_request"], state["last_request_job_id"] = candidate, job["job_id"]
            state["job"] = self._safe_job(job)
        self.state = self.store.mutate(self.client.root, self.namespace, acknowledge)
        return job

    def retry_pending(self) -> dict:
        self.state = self.store.load(self.client.root, self.namespace)
        pending = self.state.get("pending")
        if not pending:
            raise ValueError("没有等待核对的维护提交")
        return self.submit(pending["path"], pending["payload"])

    @staticmethod
    def _safe_job(job: dict) -> dict:
        # requested_by.authorization is an audit label, not an access token, but
        # intentionally excluded with all other metadata that recovery does not need.
        fields = {"job_id", "operation", "status", "configuration_version", "target_fingerprint",
                  "generation_id", "requires_recovery", "report_url", "publishable", "error_code"}
        return {key: deepcopy(value) for key, value in job.items() if key in fields}

    def record_job(self, job: dict) -> None:
        def record(state):
            current = state.get("job") or {}
            if current.get("job_id") and current["job_id"] != job["job_id"]:
                if current.get("status") in {"queued", "running"} or state.get("pending"):
                    raise SessionConflict()
                state.pop("last_request", None)
                state.pop("last_request_job_id", None)
            state["job"] = self._safe_job(job)
        self.state = self.store.mutate(self.client.root, self.namespace, record)

    def poll_once(self) -> dict:
        self.state = self.store.load(self.client.root, self.namespace)
        job_id = (self.state.get("job") or {}).get("job_id")
        if not job_id:
            raise ValueError("没有可恢复的维护任务")
        job = self.client.maintenance(job_id)
        self.record_job(job)
        return job

    def recovery_build_id(self) -> str:
        job = self.state.get("job") or {}
        request = self.state.get("last_request") or {}
        if job.get("operation") != "publish" or not job.get("requires_recovery"):
            raise ValueError("当前维护任务不需要发布核验恢复")
        if self.state.get("last_request_job_id") != job.get("job_id"):
            raise ValueError("此发布缺少本地原请求记录，不能猜测构建ID；请由原客户端核验恢复")
        build_id = (request.get("payload") or {}).get("build_job_id")
        if not build_id:
            raise ValueError("缺少原构建ID，请根据服务端维护记录核对原构建")
        return build_id
