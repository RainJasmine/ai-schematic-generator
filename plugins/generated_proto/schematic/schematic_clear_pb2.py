"""Python descriptors for protocol/schematic_clear.proto (optional host command).

The descriptor is built with protobuf's public descriptor messages, so protoc
is not required on the end user's computer. The host must implement the exact
message/field numbers; merely importing this module adds no KiCad capability.
"""
from google.protobuf import descriptor_pb2, descriptor_pool
from google.protobuf.internal import builder
from common.types import base_types_pb2 as _base_types_pb2

_file = descriptor_pb2.FileDescriptorProto(
    name="schematic/schematic_clear.proto", package="kiapi.schematic.commands", syntax="proto3",
    dependency=["common/types/base_types.proto"])
_request = _file.message_type.add(name="ClearSchematicPage")
_request.field.add(name="document", number=1, type=11, type_name=".kiapi.common.types.DocumentSpecifier", label=1)
_request.field.add(name="request_id", number=2, type=9, label=1)
_response = _file.message_type.add(name="ClearSchematicPageResponse")
for _name, _number, _type in (("success", 1, 8), ("deleted_count", 2, 13), ("message", 3, 9)):
    _response.field.add(name=_name, number=_number, type=_type, label=1)
DESCRIPTOR = descriptor_pool.Default().AddSerializedFile(_file.SerializeToString())
builder.BuildMessageAndEnumDescriptors(DESCRIPTOR, globals())
builder.BuildTopDescriptorsAndMessages(DESCRIPTOR, __name__, globals())
