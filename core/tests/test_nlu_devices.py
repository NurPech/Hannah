"""#387 Schritt 1: die Geräte-Sicht der NLU auf die typisierte Registry."""
import pytest
from hannah_proto.v2 import hannah_pb2 as pb

from hannah import nlu_devices
from hannah.nlu import NLU
from hannah.settings_manager import DEFAULT_NLU_SETTINGS
from hannah.typed_devices import DeviceRegistry, Slot, TypedDevice

C = pb.DeviceClass
K = pb.SlotKind


def device(name, device_class, *kinds, room="wohnzimmer", subtype=pb.DEVICE_SUBTYPE_UNSPECIFIED, device_id=None):
    slots = {pb.SlotKind.Name(k)[len("SLOT_KIND_"):].lower(): Slot(slot_id=pb.SlotKind.Name(k)[len("SLOT_KIND_"):].lower(),
                                                                   kind=k, writable=k == K.SLOT_KIND_ON)
             for k in kinds}
    return TypedDevice(device_id=device_id or f"dev.{room}.{name}", name=name, room=room, device_class=device_class,
                       slots=slots, subtype=subtype)


@pytest.mark.parametrize("dev, expected", [
    (device("Lampe", C.DEVICE_CLASS_LIGHT, K.SLOT_KIND_ON), {"light"}),
    (device("Dose", C.DEVICE_CLASS_SOCKET, K.SLOT_KIND_ON), {"socket"}),
    (device("Rollo", C.DEVICE_CLASS_COVER, K.SLOT_KIND_POSITION), {"blind"}),
    (device("Klima", C.DEVICE_CLASS_CLIMATE, K.SLOT_KIND_ON), {"climate"}),
    (device("Heizung", C.DEVICE_CLASS_THERMOSTAT, K.SLOT_KIND_TARGET_TEMPERATURE, K.SLOT_KIND_TEMPERATURE),
     {"thermostat", "temperature_sensor"}),
    (device("Ventil", C.DEVICE_CLASS_THERMOSTAT, K.SLOT_KIND_TARGET_TEMPERATURE), {"thermostat"}),
    (device("Messfühler", C.DEVICE_CLASS_SENSOR, K.SLOT_KIND_TEMPERATURE, K.SLOT_KIND_HUMIDITY),
     {"temperature_sensor", "humidity_sensor"}),
    (device("Luft", C.DEVICE_CLASS_SENSOR, K.SLOT_KIND_IAQ, K.SLOT_KIND_CO2), {"air_quality_sensor"}),
    (device("Lux", C.DEVICE_CLASS_SENSOR, K.SLOT_KIND_ILLUMINANCE), {"illuminance_sensor"}),
    (device("Szene", C.DEVICE_CLASS_GENERIC_BINARY_SWITCH, K.SLOT_KIND_ON), set()),
])
def test_categories_follow_class_and_slots(dev, expected):
    assert nlu_devices.categories_of(dev) == expected


class TestContactCategories:
    def test_the_subtype_decides(self):
        window = device("Kontakt", C.DEVICE_CLASS_CONTACT, K.SLOT_KIND_OPEN, subtype=pb.DEVICE_SUBTYPE_WINDOW)
        door = device("Kontakt", C.DEVICE_CLASS_CONTACT, K.SLOT_KIND_OPEN, subtype=pb.DEVICE_SUBTYPE_DOOR)

        assert nlu_devices.categories_of(window) == {"window"}
        assert nlu_devices.categories_of(door) == {"door"}

    @pytest.mark.parametrize("name, expected", [
        ("Balkontür", {"door"}), ("Haustuer", {"door"}), ("Fenster Bad", {"window"}), ("Kontakt 3", {"window", "door"}),
    ])
    def test_without_a_subtype_the_name_decides_and_unknown_answers_both(self, name, expected):
        contact = device(name, C.DEVICE_CLASS_CONTACT, K.SLOT_KIND_OPEN)

        assert nlu_devices.categories_of(contact) == expected


def test_the_index_has_a_search_key_per_device_and_is_grouped_by_room():
    registry = DeviceRegistry()
    registry.replace("v2", [
        device("DeckeSeite", C.DEVICE_CLASS_LIGHT, K.SLOT_KIND_ON, room="wohnzimmer", device_id="a"),
        device("Bettlampe", C.DEVICE_CLASS_LIGHT, K.SLOT_KIND_ON, room="schlafzimmer", device_id="b"),
    ])

    index = nlu_devices.build_index(registry)

    assert set(index) == {"wohnzimmer", "schlafzimmer"}
    decke = index["wohnzimmer"]["a"]
    assert (decke.id, decke.name, decke.key, decke.category) == ("a", "DeckeSeite", "decke seite", "light")


class TestTheNluFindsDevicesOfAV2Adapter:
    """Ein Gerät, das nur in der Registry steht (kein v1-Baum), wird trotzdem gefunden."""

    @pytest.fixture
    def nlu(self):
        registry = DeviceRegistry()
        registry.replace("v2", [
            device("Stehlampe", C.DEVICE_CLASS_LIGHT, K.SLOT_KIND_ON, room="wohnzimmer", device_id="lamp-1"),
            device("Balkontür", C.DEVICE_CLASS_CONTACT, K.SLOT_KIND_OPEN, room="wohnzimmer", device_id="door-1",
                   subtype=pb.DEVICE_SUBTYPE_DOOR),
            device("Fenster", C.DEVICE_CLASS_CONTACT, K.SLOT_KIND_OPEN, room="wohnzimmer", device_id="win-1",
                   subtype=pb.DEVICE_SUBTYPE_WINDOW),
            device("Rollladen", C.DEVICE_CLASS_COVER, K.SLOT_KIND_POSITION, room="wohnzimmer", device_id="cover-1"),
        ])
        return NLU(DEFAULT_NLU_SETTINGS, {"wohnzimmer": "Wohnzimmer"}, nlu_devices.build_index(registry))

    def test_a_named_device_resolves_to_its_device_id(self, nlu):
        intent = nlu.parse("schalte die stehlampe im wohnzimmer an")

        assert (intent.name, intent.device, intent.device_id) == ("TurnOn", "Stehlampe", "lamp-1")

    def test_a_blind_command_is_recognized_by_the_class(self, nlu):
        intent = nlu.parse("rollladen im wohnzimmer hoch")

        assert (intent.name, intent.device_id, intent.category_filter, intent.value) == ("SetLevel", "cover-1", "blind", 100)

    def test_windows_and_doors_are_told_apart_by_the_subtype(self, nlu):
        windows = nlu.parse("ist das fenster im wohnzimmer offen")
        doors = nlu.parse("ist die tuer im wohnzimmer offen")

        assert windows.category_filter == "window" and windows.device_id == "win-1"
        assert doors.category_filter == "door"
