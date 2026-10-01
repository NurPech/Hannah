"""#385: typisierte Geräte-Registry, gespeist von hannah.v2-Adaptern."""
import logging

from hannah_proto.v2 import hannah_pb2 as pb

from hannah.typed_devices import (
    ORIGIN_V1, ORIGIN_V2, DeviceRegistry, Slot, TypedDevice, slot_id_for_kind, slot_value_from_pb,
)


def _pb_device(device_id="d1", name="Decke", room="wohnzimmer", **kwargs) -> "pb.TypedDevice":
    return pb.TypedDevice(
        device_id=device_id, name=name, room=room, device_class=pb.DEVICE_CLASS_LIGHT, available=True,
        slots=[
            pb.Slot(slot_id="on", kind=pb.SLOT_KIND_ON, value=pb.SlotValue(boolean=True), writable=True),
            pb.Slot(slot_id="brightness", kind=pb.SLOT_KIND_BRIGHTNESS, value=pb.SlotValue(number=60), writable=True,
                    required_trust_level=8),
            pb.Slot(slot_id="color", kind=pb.SLOT_KIND_COLOR, writable=True),
        ],
        **kwargs,
    )


def test_slot_id_for_kind_is_the_lowercase_kind_name():
    assert slot_id_for_kind(pb.SLOT_KIND_TARGET_TEMPERATURE) == "target_temperature"
    assert slot_id_for_kind(pb.SLOT_KIND_ON) == "on"


def test_slot_value_from_pb_reads_the_case_and_none_when_unset():
    assert slot_value_from_pb(pb.SlotValue(boolean=False)) is False
    assert slot_value_from_pb(pb.SlotValue(number=2700)) == 2700
    assert slot_value_from_pb(pb.SlotValue(rgb=0xFF0000)) == 0xFF0000
    assert slot_value_from_pb(pb.SlotValue(text="x")) == "x"
    assert slot_value_from_pb(pb.SlotValue()) is None


class TestTypedSnapshot:
    def test_devices_are_converted_with_slots_and_values(self):
        registry = DeviceRegistry()

        assert registry.handle_typed_snapshot([_pb_device(floor="EG")]) == 1

        device = registry.get("d1")
        assert (device.name, device.room, device.floor, device.origin) == ("Decke", "wohnzimmer", "EG", ORIGIN_V2)
        assert device.device_class == pb.DEVICE_CLASS_LIGHT and device.available
        assert list(device.slots) == ["on", "brightness", "color"]
        assert device.slots["on"].value is True and device.slots["on"].writable
        assert device.slots["brightness"].value == 60.0
        assert device.slots["color"].value is None

    def test_required_trust_level_unset_differs_from_zero(self):
        registry = DeviceRegistry()
        device = _pb_device()
        device.slots.add(slot_id="lock", kind=pb.SLOT_KIND_GENERIC_BOOL, required_trust_level=0)
        registry.handle_typed_snapshot([device])

        slots = registry.get("d1").slots
        assert slots["on"].required_trust_level is None
        assert slots["brightness"].required_trust_level == 8
        assert slots["lock"].required_trust_level == 0

    def test_capability_check_by_slot_kind(self):
        registry = DeviceRegistry()
        registry.handle_typed_snapshot([_pb_device()])

        device = registry.get("d1")
        assert device.has_slot(pb.SLOT_KIND_BRIGHTNESS)
        assert not device.has_slot(pb.SLOT_KIND_COLOR_TEMPERATURE)
        assert device.slot_of_kind(pb.SLOT_KIND_COLOR).slot_id == "color"

    def test_device_without_name_or_room_is_not_kept(self):
        registry = DeviceRegistry()

        count = registry.handle_typed_snapshot([_pb_device("a"), _pb_device("b", room=""), _pb_device("c", name="")])

        assert count == 1
        assert [d.device_id for d in registry.devices()] == ["a"]

    def test_duplicate_device_id_keeps_the_first(self, caplog):
        registry = DeviceRegistry()

        with caplog.at_level(logging.WARNING, logger="hannah.typed_devices"):
            registry.handle_typed_snapshot([_pb_device("a", name="Erst"), _pb_device("a", name="Zweit")])

        assert registry.get("a").name == "Erst"
        assert any("doppelt" in r.getMessage() for r in caplog.records)

    def test_a_new_snapshot_replaces_the_previous_one(self):
        registry = DeviceRegistry()
        registry.handle_typed_snapshot([_pb_device("a"), _pb_device("b")])

        registry.handle_typed_snapshot([_pb_device("b"), _pb_device("c")])

        assert sorted(d.device_id for d in registry.devices()) == ["b", "c"]

    def test_devices_in_room(self):
        registry = DeviceRegistry()
        registry.handle_typed_snapshot([_pb_device("a", room="bad"), _pb_device("b", room="kueche")])

        assert [d.device_id for d in registry.devices_in_room("bad")] == ["a"]


class TestOrigins:
    def test_a_snapshot_only_replaces_devices_of_its_own_origin(self):
        registry = DeviceRegistry()
        registry.replace(ORIGIN_V1, [TypedDevice("legacy", "Alt", "bad")])
        registry.handle_typed_snapshot([_pb_device("typed")])

        registry.handle_typed_snapshot([])

        assert [d.device_id for d in registry.devices()] == ["legacy"]

        registry.replace(ORIGIN_V1, [])
        assert len(registry) == 0

    def test_state_index_follows_the_current_devices(self):
        registry = DeviceRegistry()
        device = TypedDevice("d", "Lampe", "bad", slots={"on": Slot("on", pb.SLOT_KIND_ON, state_id="x.d.on")})
        registry.replace(ORIGIN_V1, [device])
        assert registry.lookup_state("x.d.on") == ("d", "on")

        registry.replace(ORIGIN_V1, [])
        assert registry.lookup_state("x.d.on") is None


class TestSlotUpdates:
    def test_update_sets_the_slot_value(self):
        registry = DeviceRegistry()
        registry.handle_typed_snapshot([_pb_device()])

        registry.handle_slot_update(pb.SlotUpdate(device_id="d1", slot_id="brightness", value=pb.SlotValue(number=25), ack=True))
        registry.handle_slot_update(pb.SlotUpdate(device_id="d1", slot_id="color", value=pb.SlotValue(rgb=0x00FF00)))

        slots = registry.get("d1").slots
        assert slots["brightness"].value == 25
        assert slots["color"].value == 0x00FF00

    def test_update_for_an_unknown_device_or_slot_is_ignored(self):
        registry = DeviceRegistry()
        registry.handle_typed_snapshot([_pb_device()])

        registry.handle_slot_update(pb.SlotUpdate(device_id="nope", slot_id="on", value=pb.SlotValue(boolean=True)))
        registry.handle_slot_update(pb.SlotUpdate(device_id="d1", slot_id="nope", value=pb.SlotValue(boolean=True)))

        assert registry.get("d1").slots["on"].value is True

    def test_availability(self):
        registry = DeviceRegistry()
        registry.handle_typed_snapshot([_pb_device()])

        registry.handle_device_availability(pb.DeviceAvailability(device_id="d1", available=False))
        registry.handle_device_availability(pb.DeviceAvailability(device_id="nope", available=False))

        assert registry.get("d1").available is False
