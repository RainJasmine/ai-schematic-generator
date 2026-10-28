from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from common import envelope_pb2
from common.commands import editor_commands_pb2 as commands
from common.types import base_types_pb2
from schematic import schematic_types_pb2 as sch

from ai_schematic.api_client import ApiCapabilityError, ApiError, ApiTransportError, KiCadClient


class ApiClientTests(unittest.TestCase):
    def test_clear_page_uses_native_command_without_get_items(self):
        from schematic import schematic_clear_pb2 as clear
        client = self.client()
        client.request = Mock(return_value=clear.ClearSchematicPageResponse(success=True, deleted_count=12))
        client.get_items = Mock(side_effect=AssertionError("No object read for native clear"))
        self.assertEqual(client.clear_schematic_page(base_types_pb2.DocumentSpecifier()), 12)
        self.assertIsInstance(client.request.call_args.args[0], clear.ClearSchematicPage)

    def test_clear_page_falls_back_to_complete_uuid_enumeration(self):
        client = self.client()
        client.request = Mock(side_effect=ApiCapabilityError("no clear", code="AS_UNHANDLED"))
        response = commands.GetItemsResponse(status=1)
        for identity in ("old", "manual-added"):
            item = sch.Symbol()
            item.id.value = identity
            response.items.add().Pack(item)
        client.get_items = Mock(return_value=response)
        client.delete_items = Mock()
        document = base_types_pb2.DocumentSpecifier()
        self.assertEqual(client.clear_schematic_page(document), 2)
        client.delete_items.assert_called_once_with(document, ["old", "manual-added"])

    def test_missing_clear_and_enumeration_does_not_guess_owned_subset(self):
        client = self.client()
        client.request = Mock(side_effect=ApiCapabilityError("no clear", code="AS_UNHANDLED"))
        client.get_items = Mock(side_effect=ApiCapabilityError("no GetItems", code="AS_UNHANDLED"))
        client.delete_items = Mock()
        with self.assertRaises(ApiCapabilityError) as error:
            client.clear_schematic_page(base_types_pb2.DocumentSpecifier())
        self.assertEqual(error.exception.code, "KICAD_CLEAR_UNSUPPORTED")
        client.delete_items.assert_not_called()

    def test_incomplete_uuid_list_aborts_before_any_delete(self):
        client = self.client()
        client.request = Mock(side_effect=ApiCapabilityError("no clear", code="AS_UNHANDLED"))
        response = commands.GetItemsResponse(status=1)
        for identity in ("valid", ""):
            item = sch.Symbol()
            item.id.value = identity
            response.items.add().Pack(item)
        client.get_items = Mock(return_value=response)
        client.delete_items = Mock()
        with self.assertRaises(ApiCapabilityError):
            client.clear_schematic_page(base_types_pb2.DocumentSpecifier())
        client.delete_items.assert_not_called()

    def client(self):
        client = object.__new__(KiCadClient)
        client._transport_error = None
        client._token = "test-ipc-token"
        client._client_name = "test-client"
        client._socket = Mock()
        return client

    def test_get_by_id_unhandled_uses_full_read_and_filters(self):
        client = self.client()
        full = commands.GetItemsResponse(status=1)
        for identity in ("wanted", "other"):
            item = sch.Symbol()
            item.id.value = identity
            full.items.add().Pack(item)
        client.request = Mock(side_effect=[ApiCapabilityError("unsupported", code="AS_UNHANDLED"), full])
        result = client.get_items_by_id(base_types_pb2.DocumentSpecifier(), ["wanted"])
        self.assertEqual(len(result.items), 1)
        item = sch.Symbol()
        result.items[0].Unpack(item)
        self.assertEqual(item.id.value, "wanted")
        self.assertIsInstance(client.request.call_args_list[0].args[0], commands.GetItemsById)
        self.assertIsInstance(client.request.call_args_list[1].args[0], commands.GetItems)

    def test_failed_read_business_status_is_not_empty_snapshot(self):
        client = self.client()
        client.request = Mock(return_value=commands.GetItemsResponse(status=2))
        with self.assertRaises(ApiError) as error:
            client.document_snapshot(base_types_pb2.DocumentSpecifier())
        self.assertEqual(error.exception.code, "KICAD_ITEM_REQUEST_FAILED")

    def test_delete_partial_failure_raises_for_transaction_rollback(self):
        client = self.client()
        result = commands.DeleteItemsResponse(status=1)
        result.deleted_items.add(status=commands.IDS_NONEXISTENT).id.value = "wanted"
        client.request = Mock(return_value=result)
        with self.assertRaises(ApiError):
            client.delete_items(base_types_pb2.DocumentSpecifier(), ["wanted"])

    def test_unhandled_is_structured_capability_error(self):
        client = self.client()
        reply = envelope_pb2.ApiResponse()
        reply.status.status = envelope_pb2.AS_UNHANDLED
        reply.status.error_message = "no handler"
        client._socket.recv.return_value = reply.SerializeToString()
        with self.assertRaises(ApiCapabilityError) as error:
            client.request(commands.GetItems(), commands.GetItemsResponse)
        self.assertEqual(error.exception.code, "AS_UNHANDLED")

    def test_transport_failure_is_not_automatically_replayed(self):
        client = self.client()
        client._socket.recv.side_effect = RuntimeError("lost response")
        with self.assertRaises(ApiTransportError):
            client.request(commands.CreateItems(), commands.CreateItemsResponse)
        with self.assertRaises(ApiTransportError):
            client.request(commands.CreateItems(), commands.CreateItemsResponse)
        self.assertEqual(client._socket.send.call_count, 1)

    def test_explicit_reconnect_preserves_transaction_identity_without_replay(self):
        client = self.client()
        client._endpoint, client._timeout_ms = "ipc://fake", 1234
        old_socket = client._socket
        new_socket = Mock()
        client._transport_error = "lost response"
        with patch("ai_schematic.api_client.pynng.Req0", return_value=new_socket):
            client.reconnect_for_verification()
        old_socket.close.assert_called_once()
        new_socket.dial.assert_called_once_with("ipc://fake", block=True)
        new_socket.send.assert_not_called()
        self.assertEqual(client._client_name, "test-client")
        self.assertIsNone(client._transport_error)

    def test_selection_only_ack_does_not_require_typed_payload(self):
        client = self.client()
        reply = envelope_pb2.ApiResponse()
        reply.status.status = envelope_pb2.AS_OK
        client._socket.recv.return_value = reply.SerializeToString()
        client.clear_selection(base_types_pb2.DocumentSpecifier())
        client.add_to_selection(base_types_pb2.DocumentSpecifier(), ["owned-id"])
        self.assertEqual(client._socket.send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
