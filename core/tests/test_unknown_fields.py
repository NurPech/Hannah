from hannah_proto.v2 import hannah_pb2 as pb

from hannah.unknown_fields import collect_unknown_fields

# Feld 20, Varint 7 — keine AgentMessage/TypedDevice-Version kennt Feld 20.
_UNKNOWN_20 = bytes([0xA0, 0x01, 0x07])


def _device_with_unknown(state_id: str) -> bytes:
    return pb.TypedDevice(device_id=state_id).SerializeToString() + _UNKNOWN_20


def test_no_unknown_fields():
    msg = pb.AgentMessage(ack_id=1)
    msg.typed_snapshot.devices.add(device_id="a")
    assert collect_unknown_fields(msg) == {}


def test_unknown_field_in_repeated_nested_message_is_aggregated_per_type():
    msg = pb.AgentMessage(ack_id=1)
    msg.typed_snapshot.devices.add().MergeFromString(_device_with_unknown("a"))
    msg.typed_snapshot.devices.add().MergeFromString(_device_with_unknown("b"))

    assert collect_unknown_fields(msg) == {"hannah.v2.TypedDevice": {20}}


def test_unknown_oneof_payload_shows_up_on_enclosing_message():
    msg = pb.AgentMessage()
    msg.MergeFromString(pb.AgentMessage(ack_id=3).SerializeToString() + _UNKNOWN_20)

    assert collect_unknown_fields(msg) == {"hannah.v2.AgentMessage": {20}}
