"""Small NNG/Protobuf client for this KiCad checkout's schematic API."""

from __future__ import annotations

import os
import uuid
from typing import TypeVar

from google.protobuf.message import Message
import pynng

from common import envelope_pb2
from common.commands import editor_commands_pb2
from schematic import schematic_commands_pb2


ResponseT = TypeVar("ResponseT", bound=Message)


class ApiError(RuntimeError):
    """A stable code is retained for UI recovery instead of parsing messages."""

    def __init__(self, message: str, *, code: str = "KICAD_API_ERROR") -> None:
        super().__init__(message)
        self.code = code
        self.rollback_verified = False


class ApiCapabilityError(ApiError):
    """The connected editor does not implement a required handler."""


class ApiTransportError(ApiError):
    """The IPC transport failed and the current generation cannot continue."""


class KiCadClient:
    def __init__(self, timeout_ms: int = 60_000) -> None:
        endpoint = os.environ.get("KICAD_API_SOCKET")
        token = os.environ.get("KICAD_API_TOKEN")
        if not endpoint:
            raise ApiError("未收到 KICAD_API_SOCKET；请从 KiCad 原理图编辑器启动插件")
        if not token:
            raise ApiError("未收到 KICAD_API_TOKEN；请检查 KiCad API 服务是否启用")

        self._token = token
        self._client_name = f"org.rfeda.schematic-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._endpoint = endpoint
        self._timeout_ms = timeout_ms
        self._socket = pynng.Req0(send_timeout=timeout_ms, recv_timeout=timeout_ms)
        self._transport_error: str | None = None
        self._symbol_search_cache: dict[tuple[str, bool, int, str], object] = {}
        self._symbol_definition_cache: dict[tuple[str, str], object] = {}
        try:
            self._socket.dial(endpoint, block=True)
        except Exception as exc:
            self._socket.close()
            raise ApiError(f"无法连接 KiCad API：{exc}") from exc

    def close(self) -> None:
        self._socket.close()

    def reconnect_for_verification(self) -> None:
        """Reconnect with the SAME client identity; never replay a mutation.

        Only explicit verification or dropping the existing commit may follow a
        transport failure. Reconnecting is not evidence that a commit succeeded.
        """
        self._socket.close()
        self._socket = pynng.Req0(send_timeout=self._timeout_ms, recv_timeout=self._timeout_ms)
        try:
            self._socket.dial(self._endpoint, block=True)
        except Exception as exc:
            self._socket.close()
            raise ApiTransportError(f"无法重新连接 KiCad 以核验事务：{exc}") from exc
        self._transport_error = None

    def __enter__(self) -> "KiCadClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def request(self, request_message: Message, response_type: type[ResponseT] | None) -> ResponseT | None:
        if self._transport_error:
            raise ApiTransportError(self._transport_error)

        envelope = envelope_pb2.ApiRequest()
        envelope.header.kicad_token = self._token
        envelope.header.client_name = self._client_name
        envelope.message.Pack(request_message)

        try:
            self._socket.send(envelope.SerializeToString())
            payload = self._socket.recv()
        except Exception as exc:
            self._transport_error = f"KiCad API 通信失败：{exc}"
            raise ApiTransportError(self._transport_error) from exc

        response = envelope_pb2.ApiResponse()
        try:
            response.ParseFromString(payload)
        except Exception as exc:
            self._transport_error = "KiCad 返回了无法解析的 IPC 响应；操作结果待核验"
            raise ApiTransportError(self._transport_error) from exc
        if response.status.status != envelope_pb2.AS_OK:
            status_name = envelope_pb2.ApiStatusCode.Name(response.status.status)
            detail = response.status.error_message or status_name
            error_type = ApiCapabilityError if status_name == "AS_UNHANDLED" else ApiError
            raise error_type(f"KiCad API 返回 {status_name}：{detail}", code=status_name)

        if response_type is None:
            return None
        result = response_type()
        if not response.message.Unpack(result):
            raise ApiError(
                f"无法将 API 响应解包为 {response_type.DESCRIPTOR.full_name}"
            )
        return result

    def active_context(self) -> object:
        return self.request(
            schematic_commands_pb2.GetActiveSchematicContext(),
            schematic_commands_pb2.ActiveSchematicContext,
        )

    @staticmethod
    def require_success(response: Message) -> None:
        """Do not accept a response for a missing/changed document as empty."""
        field = response.DESCRIPTOR.fields_by_name.get("status")
        if field is None or field.enum_type is None:
            return
        value = field.enum_type.values_by_number.get(response.status)
        if value is None or value.name != "IRS_OK":
            raise ApiError("KiCad 对象请求未成功：" + (value.name if value else "unknown"),
                           code="KICAD_ITEM_REQUEST_FAILED")

    def get_items(self, document: object) -> object:
        request = editor_commands_pb2.GetItems()
        request.header.document.CopyFrom(document)
        result = self.request(request, editor_commands_pb2.GetItemsResponse)
        self.require_success(result)
        return result

    def get_items_by_id(self, document: object, item_ids: list[str]) -> object:
        """Older/custom hosts sometimes implement GetItems but not GetItemsById."""
        request = editor_commands_pb2.GetItemsById()
        request.header.document.CopyFrom(document)
        for item_id in item_ids:
            request.items.add().value = item_id
        try:
            result = self.request(request, editor_commands_pb2.GetItemsResponse)
            self.require_success(result)
            return result
        except ApiCapabilityError:
            from .snapshot import item_id
            result = self.get_items(document)
            filtered = editor_commands_pb2.GetItemsResponse()
            filtered.header.CopyFrom(result.header)
            filtered.status = result.status
            wanted = set(item_ids)
            for packed in result.items:
                if item_id(packed) in wanted:
                    filtered.items.add().CopyFrom(packed)
            return filtered

    def selection(self, document: object) -> object:
        request = editor_commands_pb2.GetSelection()
        request.header.document.CopyFrom(document)
        return self.request(request, editor_commands_pb2.SelectionResponse)

    get_selection = selection

    def clear_selection(self, document: object) -> None:
        request = editor_commands_pb2.ClearSelection()
        request.header.document.CopyFrom(document)
        self.request(request, None)

    def add_to_selection(self, document: object, item_ids: list[str]) -> None:
        request = editor_commands_pb2.AddToSelection()
        request.header.document.CopyFrom(document)
        for item_id in item_ids:
            request.items.add().value = item_id
        self.request(request, None)

    def document_snapshot(self, document: object) -> str:
        """Legacy diagnostic only; structural checks use snapshot.py instead."""
        response = self.get_items(document)  # checks IRS_OK, never hashes a failed empty read
        return "\n".join(sorted(item.SerializeToString(deterministic=True).hex() for item in response.items))

    def delete_items(self, document: object, item_ids: list[str]) -> None:
        request = editor_commands_pb2.DeleteItems()
        request.header.document.CopyFrom(document)
        for item_id in item_ids:
            request.item_ids.add().value = item_id
        result = self.request(request, editor_commands_pb2.DeleteItemsResponse)
        self.require_success(result)
        if (len(result.deleted_items) != len(item_ids)
                or any(item.status != editor_commands_pb2.IDS_OK for item in result.deleted_items)):
            raise ApiError("KiCad 未能完整删除旧版本对象", code="KICAD_DELETE_FAILED")

    def clear_schematic_page(self, document: object) -> int:
        """清空当前页，必须在本客户端事务内调用；不保存/比较图纸快照。

        首选单个清空命令。旧宿主可用完整对象枚举 + DeleteItems 回退，
        这里只提取 UUID 来删除，不回传画布、参数或连线给 AI。
        """
        from google.protobuf import descriptor_pool, message_factory
        from schematic import schematic_clear_pb2 as clear
        request = clear.ClearSchematicPage(request_id=uuid.uuid4().hex)
        request.document.CopyFrom(document)
        try:
            response = self.request(request, clear.ClearSchematicPageResponse)
        except ApiCapabilityError as exc:
            if exc.code != "AS_UNHANDLED":
                raise
        else:
            if not response.success:
                raise ApiError(response.message or "宿主未完成整页清空", code="KICAD_CLEAR_FAILED")
            return int(response.deleted_count)
        try:
            response = self.get_items(document)
        except ApiCapabilityError as exc:
            raise ApiCapabilityError(
                "宿主缺少清空图页接口，且不能枚举旧对象；请实现 ClearSchematicPage 或完整 GetItems/DeleteItems。原图未清空。",
                code="KICAD_CLEAR_UNSUPPORTED") from exc
        identities = []
        for packed in response.items:
            try:
                cls = message_factory.GetMessageClass(descriptor_pool.Default().FindMessageTypeByName(packed.TypeName()))
                item = cls()
                if not packed.Unpack(item):
                    raise ValueError("无法解码图页对象")
                identity = item.id.value
                if not identity or identity in identities:
                    raise ValueError("对象 UUID 缺失或重复")
                identities.append(identity)
            except (AttributeError, KeyError, ValueError) as exc:
                raise ApiCapabilityError("不能完整枚举当前图页的全部对象，已取消清空。", code="KICAD_CLEAR_UNSUPPORTED") from exc
        if identities:
            self.delete_items(document, identities)
        return len(identities)

    def update_items(self, document: object, items: list[Message]) -> object:
        request = editor_commands_pb2.UpdateItems()
        request.header.document.CopyFrom(document)
        for item in items:
            request.items.add().Pack(item)
        result = self.request(request, editor_commands_pb2.UpdateItemsResponse)
        self.require_success(result)
        if (len(result.updated_items) != len(items)
                or any(item.status.code != editor_commands_pb2.ISC_OK for item in result.updated_items)):
            raise ApiError("KiCad 未能完整更新对象", code="KICAD_UPDATE_FAILED")
        return result

    def search_symbols(
        self,
        query: str,
        exact: bool,
        limit: int = 25,
        library_nickname: str | None = None,
    ) -> object:
        key = (query.casefold(), exact, limit, (library_nickname or "").casefold())
        if key in self._symbol_search_cache:
            return self._symbol_search_cache[key]

        request = schematic_commands_pb2.SearchSymbolLibrary()
        request.query = query
        request.max_results = limit
        if library_nickname:
            request.library_nickname = library_nickname
        request.match_mode = (
            schematic_commands_pb2.SLMM_EXACT
            if exact
            else schematic_commands_pb2.SLMM_SUBSTRING
        )
        response = self.request(request, schematic_commands_pb2.SearchSymbolLibraryResponse)
        self._symbol_search_cache[key] = response
        return response

    def symbol_definition(self, library_id: object) -> object:
        key = (library_id.library_nickname, library_id.entry_name)
        if key in self._symbol_definition_cache:
            return self._symbol_definition_cache[key]

        request = schematic_commands_pb2.GetSymbolLibraryDefinition()
        request.library_id.CopyFrom(library_id)
        response = self.request(request, schematic_commands_pb2.SymbolLibraryDefinition)
        self._symbol_definition_cache[key] = response
        return response

    def begin_commit(self) -> str:
        response = self.request(
            editor_commands_pb2.BeginCommit(), editor_commands_pb2.BeginCommitResponse
        )
        return response.id.value

    def create_items(self, document: object, items: list[Message]) -> object:
        request = editor_commands_pb2.CreateItems()
        request.header.document.CopyFrom(document)
        for item in items:
            request.items.add().Pack(item)
        return self.request(request, editor_commands_pb2.CreateItemsResponse)

    def end_commit(self, commit_id: str, commit: bool, message: str = "") -> None:
        request = editor_commands_pb2.EndCommit()
        request.id.value = commit_id
        request.action = (
            editor_commands_pb2.CMA_COMMIT if commit else editor_commands_pb2.CMA_DROP
        )
        request.message = message
        self.request(request, editor_commands_pb2.EndCommitResponse)
