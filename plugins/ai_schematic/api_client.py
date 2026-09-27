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
    """KiCad transport or API error."""


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
        self._client_name = f"com.sunveda.kicad-ai-schematic-{os.getpid()}-{uuid.uuid4().hex[:8]}"
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

    def __enter__(self) -> "KiCadClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def request(self, request_message: Message, response_type: type[ResponseT]) -> ResponseT:
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
        response.ParseFromString(payload)
        if response.status.status != envelope_pb2.AS_OK:
            status_name = envelope_pb2.ApiStatusCode.Name(response.status.status)
            detail = response.status.error_message or status_name
            raise ApiError(f"KiCad API 返回 {status_name}：{detail}")

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
