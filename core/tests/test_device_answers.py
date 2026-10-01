"""#387 Schritt 3: Abfragen über Geräteklassen und Slots, Antworten aus der typisierten Registry."""
import pytest
from hannah_proto.v2 import hannah_pb2 as pb

from hannah.device_answers import DeviceAnswers
from hannah.nlu import Intent
from hannah.typed_devices import DeviceRegistry, Slot, TypedDevice

C = pb.DeviceClass
K = pb.SlotKind

ROOMS = {"wohnzimmer": "Wohnzimmer", "bad": "Bad"}


def device(device_id, name, device_class, *slots, room="wohnzimmer", subtype=pb.DEVICE_SUBTYPE_UNSPECIFIED):
    return TypedDevice(
        device_id=device_id, name=name, room=room, device_class=device_class, subtype=subtype,
        slots={s.slot_id: s for s in slots},
    )


def slot(kind, value, writable=False, slot_id=None):
    return Slot(slot_id=slot_id or pb.SlotKind.Name(kind)[len("SLOT_KIND_"):].lower(), kind=kind, value=value,
                writable=writable)


def answers(*devices):
    registry = DeviceRegistry()
    registry.replace("v2", devices)
    return DeviceAnswers(registry, lambda: ROOMS)


def query(room=None, device_id=None, category=None, state=None):
    return Intent(name="Query", room=ROOMS.get(room), room_id=room, device_id=device_id,
                  category_filter=category, query_state=state)


class TestTemperatureAndHumidityDoNotCollide:
    """In der v1-Welt hießen beide `current`, die Antwort nannte die Feuchte womöglich "Grad"."""

    @pytest.fixture
    def bad(self):
        return answers(device("b1", "Raumklima", C.DEVICE_CLASS_SENSOR,
                              slot(K.SLOT_KIND_TEMPERATURE, 21.5), slot(K.SLOT_KIND_HUMIDITY, 45.0), room="bad"))

    def test_asked_for_temperature(self, bad):
        assert bad.answer(query(room="bad", category="temperature_sensor")) == "Raumklima im Bad: 21.5 Grad."

    def test_asked_for_humidity(self, bad):
        assert bad.answer(query(room="bad", category="humidity_sensor")) == "Raumklima im Bad: 45 %."


class TestThermostatNamesActualAndTarget:
    def test_with_both_values(self):
        heizung = device("t1", "Heizung", C.DEVICE_CLASS_THERMOSTAT,
                         slot(K.SLOT_KIND_TEMPERATURE, 20.5), slot(K.SLOT_KIND_TARGET_TEMPERATURE, 21.0, writable=True))

        assert answers(heizung).answer(query(device_id="t1", room="wohnzimmer", category="thermostat")) == \
            "Heizung im Wohnzimmer: 20.5 Grad, Soll 21 Grad."

    def test_with_only_a_setpoint(self):
        ventil = device("t2", "Ventil", C.DEVICE_CLASS_THERMOSTAT, slot(K.SLOT_KIND_TARGET_TEMPERATURE, 19.0, writable=True))

        assert answers(ventil).answer(query(device_id="t2", room="wohnzimmer")) == "Ventil im Wohnzimmer: Soll 19 Grad."

    def test_several_thermostats_in_a_room_are_listed(self):
        a = device("t1", "Heizung", C.DEVICE_CLASS_THERMOSTAT,
                   slot(K.SLOT_KIND_TEMPERATURE, 20.0), slot(K.SLOT_KIND_TARGET_TEMPERATURE, 21.0))
        b = device("t2", "Fußboden", C.DEVICE_CLASS_THERMOSTAT,
                   slot(K.SLOT_KIND_TEMPERATURE, 22.0), slot(K.SLOT_KIND_TARGET_TEMPERATURE, 23.0))

        assert answers(a, b).answer(query(room="wohnzimmer", category="thermostat")) == \
            "Im Wohnzimmer: Heizung: 20 Grad, Soll 21 Grad, Fußboden: 22 Grad, Soll 23 Grad."


class TestClimate:
    @pytest.fixture
    def klima(self):
        return device("k1", "Klimaanlage", C.DEVICE_CLASS_CLIMATE,
                      slot(K.SLOT_KIND_ON, True, True), slot(K.SLOT_KIND_MODE, "cool", True),
                      slot(K.SLOT_KIND_TEMPERATURE, 24.5), slot(K.SLOT_KIND_TARGET_TEMPERATURE, 22.0, True),
                      slot(K.SLOT_KIND_FAN_SPEED, "low", True))

    def test_the_status_names_mode_temperatures_and_fan(self, klima):
        assert answers(klima).answer(query(device_id="k1", room="wohnzimmer")) == \
            "Klimaanlage im Wohnzimmer: an, Modus Kühlen, 24.5°C, Soll 22.0°C, Lüfter niedrig."

    def test_asked_for_the_temperature_it_answers_with_actual_and_target(self, klima):
        assert answers(klima).answer(query(device_id="k1", room="wohnzimmer", category="temperature_sensor")) == \
            "Klimaanlage im Wohnzimmer: 24.5 Grad, Soll 22 Grad."

    def test_the_room_question_finds_it_among_the_temperature_devices(self, klima):
        assert answers(klima).answer(query(room="wohnzimmer", category="temperature_sensor")) == \
            "Klimaanlage im Wohnzimmer: 24.5 Grad, Soll 22 Grad."


class TestContactsOfAV2Adapter:
    @pytest.fixture
    def house(self):
        return answers(
            device("w1", "Fenster", C.DEVICE_CLASS_CONTACT, slot(K.SLOT_KIND_OPEN, True),
                   subtype=pb.DEVICE_SUBTYPE_WINDOW),
            device("d1", "Balkontür", C.DEVICE_CLASS_CONTACT, slot(K.SLOT_KIND_OPEN, False),
                   subtype=pb.DEVICE_SUBTYPE_DOOR),
        )

    def test_windows_and_doors_are_asked_apart(self, house):
        assert house.answer(query(room="wohnzimmer", category="window")) == "Fenster im Wohnzimmer: offen."
        assert house.answer(query(room="wohnzimmer", category="door")) == "Balkontür im Wohnzimmer: geschlossen."

    def test_a_question_for_a_category_nobody_has_says_so(self, house):
        assert house.answer(query(room="wohnzimmer", category="light")) == "Ich kenne keine Lichter im Wohnzimmer."

    def test_an_unknown_room_says_so(self, house):
        assert house.answer(query(room="bad")) == "Ich kenne keine Geräte im Bad."


class TestBlindsAreAnsweredInTheCanonicalScale:
    def test_the_position_is_the_one_the_adapter_normalized(self):
        rollo = device("c1", "Markise", C.DEVICE_CLASS_COVER, slot(K.SLOT_KIND_POSITION, 70.0, writable=True))

        assert answers(rollo).answer(query(device_id="c1", room="wohnzimmer")) == "Markise im Wohnzimmer: 70 %."


def test_an_unknown_device_id_gives_no_answer():
    assert answers().answer(query(device_id="gibt-es-nicht")) is None


def test_a_global_question_about_lights_names_the_rooms():
    licht = device("l1", "Decke", C.DEVICE_CLASS_LIGHT, slot(K.SLOT_KIND_ON, True, True))
    bad = device("l2", "Spiegel", C.DEVICE_CLASS_LIGHT, slot(K.SLOT_KIND_ON, True, True), room="bad")
    aus = device("l3", "Stehlampe", C.DEVICE_CLASS_LIGHT, slot(K.SLOT_KIND_ON, False, True))

    assert answers(licht, bad, aus).answer(query(category="light", state="on")) == "Eingeschaltete Lichter in: Bad, Wohnzimmer."
