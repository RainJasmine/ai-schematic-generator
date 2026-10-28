"""无 GUI 的客户端状态机；网络任务、成功图、画布导入分别持久保存。"""
from copy import deepcopy
import re
import threading

from .ai_service import AiServiceError, WaitingStopped
from .graph_ir import GraphIR


class ClientWorkflow:
    def __init__(self, client, store, *, project_id="", canvas=None, event=None, log=None):
        if canvas is None:
            from . import generator as canvas
        self.client, self.store, self.canvas = client, store, canvas
        self.service = client.service_url
        self.project_id = project_id
        self.state = store.load(self.service, project_id)
        self.event = event or (lambda value: None)
        self.log = log or (lambda value: None)
        self.lock = threading.RLock()

    def save(self, **changes):
        with self.lock:
            next_state = deepcopy(self.state)
            next_state.update(deepcopy(changes))
            self.store.save(self.service, self.project_id, next_state)
            self.state = next_state

    @staticmethod
    def check_waiting(stop_event):
        if stop_event and stop_event.is_set():
            raise WaitingStopped()

    @staticmethod
    def require_baseline(value):
        if (not isinstance(value, dict) or type(value.get("revision")) is not int or value["revision"] < 1
                or not re.fullmatch(r"[0-9a-f]{32}", str(value.get("head_job_id", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("graph_hash", "")))):
            raise AiServiceError("服务端修订基准响应不完整，不能确认当前成功版本。", code="INVALID_RESPONSE")
        return value

    def target(self, *, existing=False):
        # 只读目标身份/坐标约定，不发送 GetItems 读取图纸内容。
        target = self.canvas.capture_target(readback=False)
        old = self.state.get("target")
        if existing and old and old["key"] != target["key"]:
            raise ValueError("请切回任务绑定的原理图，再继续操作")
        with self.lock:
            if target["key"] != self.project_id:
                next_state = self.store.load(self.service, target["key"])
                next_state["target"] = target
                self.store.save(self.service, target["key"], next_state)
                self.project_id, self.state = target["key"], next_state
            else:
                self.save(target=target)
        self.event({"type": "target", "target": target})
        if self.state.get("uncertain"):
            raise ValueError("上次画图结果尚未核验，请先检查 KiCad 原图及撤销记录，不能自动重复导入")
        return target

    def accept(self, job, kind, parent=None):
        operation = {"job": job, "kind": kind}
        if parent:
            operation["parent_job_id"] = parent
        self.save(operation=operation, pending=None)

    def submit_pending(self):
        pending = deepcopy(self.state["pending"])
        already_unknown = pending.get("outcome_unknown", False)
        pending["attempts"] = pending.get("attempts", 0) + 1
        pending["outcome_unknown"] = True
        self.save(pending=pending)
        try:
            job = self.client.submit_frozen(pending["request"])
        except AiServiceError as exc:
            # 明确拒绝可修改输入；网络/5xx 结果未知，保留同一 ID 和冻结请求。
            if (not already_unknown and exc.status and 400 <= exc.status < 500
                    and exc.status not in (408, 429) and exc.code != "REQUEST_CONFLICT"):
                self.save(pending=None)
            raise
        self.accept(job, pending["kind"], pending.get("parent_job_id"))
        return job

    def create(self, kind, *, path="", user_text="", run_name="", stop_event=None):
        """生成 AI 结果与绘图分离；本方法不调用任何 KiCad IPC。"""
        self.check_waiting(stop_event)
        if self.state.get("pending") or self.state.get("operation", {}).get("job", {}).get("status") in ("queued", "running"):
            raise ValueError("已有待确认或运行中的任务，请先恢复，避免重复提交")
        if self.state.get("success") and not self.state.get("new_design") and kind != "json":
            raise ValueError("已有后端设计版本，请提交反馈，或明确开始新设计")
        if kind == "json":
            graph = GraphIR.load(path)
            if graph.errors:
                raise ValueError("Graph IR 含阻断错误：" + "; ".join(graph.errors))
            self.save(local_graph=graph.source, success={}, operation={}, continuation_job_id=None, new_design=False)
            return {"ready": True, "local": True}
        frozen = self.store.freeze_create(kind, path=path, user_text=user_text, run_name=run_name)
        self.save(pending={"kind": "create", "request": frozen}, ended=False)
        self.check_waiting(stop_event)
        return self.follow(self.submit_pending(), None, stop_event)

    def resume(self, job_id=None, *, stop_event=None):
        self.check_waiting(stop_event)
        if self.state.get("pending"):
            if job_id:
                raise ValueError("原请求仍待确认，请先恢复原冻结请求，不能用其他任务 ID 替代")
            job = self.submit_pending()
        else:
            operation = self.state.get("operation") or {}
            identifier = job_id or operation.get("job", {}).get("job_id")
            if not identifier:
                raise ValueError("没有可恢复任务，请填写服务任务 ID")
            job = self.client.get_job(identifier)
            if identifier != operation.get("job", {}).get("job_id"):
                self.accept(job, "recovered")
        return self.follow(job, None, stop_event)

    def follow(self, job, target, stop_event):
        def update(row):
            operation = deepcopy(self.state.get("operation") or {})
            operation["job"] = row
            self.save(operation=operation)
            self.event({"type": "job", "job": row})
        update(job)
        job = self.client.follow_job(job.get("status_url") or job["job_id"], on_progress=update,
                                     stop_event=stop_event)
        if stop_event and stop_event.is_set():
            raise WaitingStopped()
        operation = deepcopy(self.state.get("operation") or {})
        operation["job"] = job
        self.record_interaction(job)
        if job["status"] != "succeeded":
            diagnostic = {}
            if operation.get("kind") == "revision":
                try:
                    diagnostic = self.client.revision_result(job["job_id"])
                except AiServiceError as exc:
                    self.log("本轮诊断暂不可用：" + str(exc))
            operation["diagnostic"] = diagnostic
            self.save(operation=operation,
                      continuation_job_id=job["job_id"] if diagnostic else None)
            return {"terminal": job, "diagnostic": diagnostic}
        baseline = None
        baseline_error = None
        try:
            baseline = self.require_baseline(self.client.revision_state(job["job_id"]))
        except AiServiceError as exc:
            baseline_error = exc
            self.log("当前版本基准尚未核验，只保留图文件，暂不导入：" + str(exc))
        if baseline and baseline.get("head_job_id") and baseline["head_job_id"] != job["job_id"]:
            raise ValueError("该任务是历史成功版本，只允许预览。请恢复最新成功任务 " + baseline["head_job_id"])
        raw = self.client.graph(job.get("result_url") or job["job_id"])
        graph = GraphIR.parse(raw)
        operation.update(candidate_graph=raw, baseline=baseline)
        self.save(operation=operation)
        if graph.errors:
            raise AiServiceError("Graph IR 尚未满足交付条件：" + "; ".join(graph.errors), code="GRAPH_INVALID")
        if baseline_error:
            raise AiServiceError("成功图已缓存，但无法确认服务端当前 head；请恢复原任务重新核验后再导入。",
                                 code="BASELINE_NOT_VERIFIED") from baseline_error
        history = list(self.state.get("history") or [])
        if not any(item.get("job_id") == job["job_id"] for item in history):
            history.append({"job_id": job["job_id"], "revision": (baseline or {}).get("revision"),
                            "run_name": job.get("run_name"), "report_url": job.get("report_url")})
        self.save(success={"job": job, "graph": raw, "baseline": baseline},
                  operation={"job": job, "kind": operation.get("kind")}, history=history[-100:],
                  ended=False, continuation_job_id=None, new_design=False, local_graph=None)
        return {"ready": True, "job": job}

    def apply_current_graph(self, *, stop_event=None):
        """唯一写图入口：用户点击后才定位当前页并整页替换。"""
        self.check_waiting(stop_event)
        if self.state.get("pending") or self.state.get("operation", {}).get("job", {}).get("status") in ("queued", "running"):
            raise ValueError("请先等待当前设计任务完成或恢复原请求")
        if self.state.get("uncertain"):
            raise ValueError("上次绘图结果尚未确认，请先处理未确认操作，不能自动重复清图")
        success = self.state.get("success") or {}
        raw = success.get("graph") or self.state.get("local_graph")
        if not raw:
            raise ValueError("没有可更新的完整 Graph IR，请先生成或导入")
        if success:
            baseline = self.require_baseline(self.client.revision_state(success["job"]["job_id"]))
            if baseline["head_job_id"] != success["job"]["job_id"]:
                raise ValueError("服务端已有新版本，请先恢复最新设计结果")
        graph = GraphIR.parse(raw)
        target = self.canvas.capture_target(readback=False)
        self.save(target=target)
        result = self.import_graph(graph, target, stop_event)
        self.save(applied_job_id=success.get("job", {}).get("job_id"), applied_graph_hash=result["report"].get("graph_hash"),
                  applied_revision=(success.get("baseline") or {}).get("revision"))
        return result

    def clear_current_page(self, *, stop_event=None):
        """User-requested clear with no AI graph; retain a journal for unknown IPC results."""
        self.check_waiting(stop_event)
        if (self.state.get("pending") or self.state.get("uncertain")
                or self.state.get("operation", {}).get("job", {}).get("status") in ("queued", "running")):
            raise ValueError("请先处理运行中或结果待确认的操作，再清空当前图页")
        target = self.canvas.capture_target(readback=False)
        self.save(target=target)
        checkpoint_written = False

        def checkpoint(value):
            nonlocal checkpoint_written
            self.save(uncertain=True, mutation_checkpoint=value)
            checkpoint_written = True
            self.event({"type": "mutation"})

        try:
            deleted = self.canvas.clear_current_page(target, self.log,
                                                     on_checkpoint=checkpoint)
        except Exception as exc:
            if checkpoint_written and (getattr(exc, "rollback_verified", False)
                                       or getattr(exc, "rollback_confirmed", False)):
                self.save(uncertain=False, mutation_checkpoint=None)
            raise
        self.save(uncertain=False, mutation_checkpoint=None, **{"import": {},
                  "applied_job_id": None, "applied_graph_hash": None,
                  "applied_revision": None})
        return {"cleared": True, "deleted_items_count": deleted}

    def import_graph(self, graph, target, stop_event):
        if self.state.get("uncertain"):
            raise ValueError("上次图纸事务尚未核验，不能重复导入")
        if stop_event and stop_event.is_set():
            raise WaitingStopped()
        checkpoint_written = False
        def checkpoint(value):
            nonlocal checkpoint_written
            self.save(uncertain=True, mutation_checkpoint=value)
            checkpoint_written = True
            self.event({"type": "mutation"})
        try:
            report = self.canvas.generate(graph, self.log, target=target,
                                          previous=self.state.get("import"), on_checkpoint=checkpoint,
                                          replace_all=True)
        except Exception as exc:
            if checkpoint_written and (getattr(exc, "rollback_verified", False) or getattr(exc, "rollback_confirmed", False)):
                self.save(uncertain=False, mutation_checkpoint=None)
            raise
        self.save(**{"import": report.to_dict(), "uncertain": False, "mutation_checkpoint": None})
        self.log(report.format())
        return {"delivered": True, "report": report.to_dict()}

    def selection(self):
        previous = self.state.get("import")
        if not previous:
            raise ValueError("需要当前图的导入记录和可信快照")
        return self.canvas.prepare_feedback(previous)

    def revise(self, instruction, *, stop_event=None):
        """反馈只引用后端设计基准；既不读图，也不要求已应用到画布。"""
        instruction = instruction.strip()
        if not instruction or len(instruction) > 12000:
            raise ValueError("反馈要求为 1～12000 字符")
        self.check_waiting(stop_event)
        if self.state.get("pending"):
            raise ValueError("上次提交结果待确认，请先恢复")
        success = self.state.get("success") or {}
        if not success:
            raise ValueError("需要后端成功设计版本")
        operation = self.state.get("operation", {}).get("job") or {}
        if operation.get("status") in ("queued", "running"):
            raise ValueError("当前操作尚未完成，请先恢复原任务")
        if self.state.get("ended"):
            raise ValueError("本轮已结束，请先选择继续反馈")
        caps = self.client.capabilities()
        if "backend_only" not in (caps.get("revision") or {}).get("canvas_policies", []):
            raise AiServiceError("后端尚未声明仅后端反馈协议；请重启包含 canvas_policy 支持的新版 AI 服务。", code="BACKEND_FEEDBACK_UNSUPPORTED")
        parent = success["job"]["job_id"]
        baseline = self.require_baseline(self.client.revision_state(parent))
        if baseline.get("pending_job_id"):
            raise ValueError("已有反馈运行中，请恢复任务 " + baseline["pending_job_id"])
        if baseline["head_job_id"] != parent:
            raise ValueError("服务端已有新版本，请先恢复任务 " + baseline["head_job_id"])
        payload = {"request_id": self.store.new_request_id("revision"), "base_revision": baseline["revision"],
                   "base_graph_hash": baseline["graph_hash"], "instruction": instruction,
                   "canvas_policy": "backend_only", "selection": {"node_ids": [], "edge_ids": []}}
        if self.state.get("continuation_job_id"):
            payload["continuation_job_id"] = self.state["continuation_job_id"]
        frozen = self.store.freeze_revision(parent, payload)
        self.save(pending={"kind": "revision", "parent_job_id": parent, "request": frozen}, ended=False)
        self.check_waiting(stop_event)
        return self.follow(self.submit_pending(), None, stop_event)

    def finish(self):
        if not self.state.get("success") and not self.state.get("local_graph"):
            raise ValueError("需要已生成的设计结果")
        if self.state.get("pending") or self.state.get("operation", {}).get("job", {}).get("status") in ("queued", "running"):
            raise ValueError("仍有任务未确认，暂不能结束本轮")
        self.save(ended=True, new_design=False)

    def arm_new_design(self):
        job = self.state.get("operation", {}).get("job") or {}
        if self.state.get("pending") or job.get("status") in ("queued", "running"):
            raise ValueError("已有待确认或运行中的操作，请先恢复，不能丢弃跟踪")
        if job:
            self.record_interaction(job)
        self.save(new_design=True, operation={}, success={}, local_graph=None,
                  continuation_job_id=None, ended=False, conversation=[], draft={})

    def clear_conversation(self):
        self.save(conversation=[])

    def permit_full_page_retry(self, *, user_confirmed=False):
        """用户处理旧事务后，显式允许重绘；不声称已读取或核验原图。"""
        if not user_confirmed:
            raise ValueError("需用户先在 KiCad 处理旧操作，并确认允许重新整页更新")
        if not self.state.get("uncertain"):
            return
        self.save(manual_recovery={"previous_checkpoint": self.state.get("mutation_checkpoint"),
            "reason": "user_authorized_full_page_retry", "canvas_verified": False},
            uncertain=False, mutation_checkpoint=None)

    def clear_records(self):
        """清本地任务展示，保留绘图收据；不调用服务器删除或 KiCad 清图。"""
        job = self.state.get("operation", {}).get("job") or {}
        if self.state.get("pending") or job.get("status") in ("queued", "running") or self.state.get("uncertain"):
            raise ValueError("待确认请求、运行任务或未确认绘图不能丢弃，请先完成恢复")
        self.save(success={}, operation={}, pending=None, local_graph=None,
                  history=[], interactions=[], conversation=[], draft={},
                  continuation_job_id=None, new_design=True, ended=False,
                  applied_job_id=None, applied_revision=None, applied_graph_hash=None)

    def record_interaction(self, job):
        rows = list(self.state.get("interactions") or [])
        row = {"job_id": job["job_id"], "status": job.get("status"), "run_name": job.get("run_name"),
               "interaction_status": (job.get("summary") or {}).get("interaction_status"),
               "report_url": job.get("report_url")}
        rows = [r for r in rows if r.get("job_id") != row["job_id"]]
        rows.append(row)
        self.save(interactions=rows[-100:])

    def inspect(self, job_id):
        """历史预览永不改变 operation、pending、success 或画布。"""
        job = self.client.get_job(job_id)
        diagnostic = None
        if job.get("revision_result_url") and job.get("status") not in ("queued", "running"):
            diagnostic = self.client.revision_result(job["job_id"])
        return {"job": job, "diagnostic": diagnostic}

    def new_feedback(self):
        self.save(continuation_job_id=None, ended=False, new_design=False)

    def highlight(self, choice):
        # 候选只填入反馈草稿；本版不要求画布与后端同步，不进行画布定位。
        return None

    def recover_legacy(self, path, *, stop_event=None):
        from .session_store import read_legacy
        from .ai_service import _service_root
        legacy = read_legacy(path)
        service = legacy.get("current_service_url") or legacy.get("service_url")
        if not service or _service_root(service) != self.service:
            raise ValueError("旧会话属于其他服务，请先切换到其服务地址并重新填写访问令牌")
        if legacy.get("pending"):
            raise ValueError("旧会话有结果未确认的请求且缺少完整冻结载荷，请先核对原请求；不能自动重放")
        job = legacy.get("current_job") or {}
        if not job.get("job_id") or not legacy.get("placement"):
            raise ValueError("旧会话缺少成功任务或对象映射，只能按任务 ID 查询，不能自动认领图纸对象")
        target = self.target()
        active_job = self.state.get("operation", {}).get("job") or {}
        if (self.state.get("import") or self.state.get("pending")
                or active_job.get("status") in ("queued", "running", "succeeded")):
            raise ValueError("当前会话已有导入记录或待确认请求，不能覆盖；请先恢复当前会话")
        self.check_waiting(stop_event)
        job = self.client.get_job(job["job_id"])
        if job.get("status") != "succeeded":
            raise ValueError("旧会话的当前任务不是成功版本")
        graph = GraphIR.parse(self.client.graph(job.get("result_url") or job["job_id"]))
        self.check_waiting(stop_event)
        report = self.canvas.adopt_legacy_placement(legacy["placement"], graph)
        if report.target["key"] != target["key"]:
            raise ValueError("认领时活动图纸发生变化，请切回原图纸")
        baseline = self.require_baseline(self.client.revision_state(job["job_id"]))
        history = list(self.state.get("history") or [])
        if not any(row.get("job_id") == job["job_id"] for row in history):
            history.append({"job_id": job["job_id"], "revision": baseline.get("revision"), "run_name": job.get("run_name")})
        self.save(success={"job": job, "graph": graph.source, "baseline": baseline}, operation={"job": job, "kind": "legacy"},
                  **{"import": report.to_dict(), "target": report.target, "uncertain": False,
                     "history": history[-100:],
                     "continuation_job_id": None, "new_design": False})
        return {"adopted": True, "report": report.to_dict(), "head_job_id": baseline.get("head_job_id")}

    def verify_canvas(self):
        if not self.state.get("uncertain"):
            return {"verified": True, "message": "当前没有待核验事务"}
        result = self.canvas.verify_uncertain(self.state.get("mutation_checkpoint") or {}, self.state.get("import"))
        if not result.get("verified"):
            raise ValueError(result.get("message") or "尚不能证明图纸已恢复，保留待核验状态")
        self.save(uncertain=False, mutation_checkpoint=None)
        return result
