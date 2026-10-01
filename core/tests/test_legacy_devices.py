"""#385: Legacy-Klassifikation, v1-Snapshot (State-basiert) → Klasse + Slots."""
from collections import Counter

import pytest
from hannah_proto.v1 import hannah_pb2 as pb1
from hannah_proto.v2 import hannah_pb2 as pb

from hannah import legacy_devices
from hannah.typed_devices import DeviceRegistry

C = pb.DeviceClass
K = pb.SlotKind
BOOLEAN, NUMERIC, ENUM, COLOR, TEXT = (pb1.StateType.BOOLEAN, pb1.StateType.NUMERIC, pb1.StateType.ENUM,
                                       pb1.StateType.COLOR, pb1.StateType.TEXT)

BASE = "javascript.0.virtualDevice"


def state(device: str, suffix: str, value: str, *, key=None, dtype="", state_type=NUMERIC, writable=True,
          room="wohnzimmer", name=None, inverted=None, trust=None, enum_values=None) -> "pb1.AgentDevice":
    """Ein State eines v1-Adapters, `device` ist der Gerätename und zugleich der Ordner."""
    message = pb1.AgentDevice(
        state_id=f"{BASE}.{device}.{suffix}", device_id=f"{BASE}.{device}",
        canonical_key=suffix if key is None else key, room=room, device=name or device,
        device_type=dtype, value=pb1.AgentStateValue(value=value, ack=True), room_names={"de": room.title()},
        state_type=state_type, writable=writable, floor="EG",
    )
    if inverted is not None:
        message.inverted = inverted
    if trust is not None:
        message.required_trust_level = trust
    if enum_values:
        message.enum_values.values.update({v: v for v in enum_values})
    return message


def classify(*states) -> dict:
    """Name → TypedDevice."""
    return {d.name: d for d in legacy_devices.classify_snapshot(states)}


def kinds(device) -> dict:
    return {s.slot_id: s.kind for s in device.slots.values()}


# ------------------------------------------------------------------
# Lichter, Steckdosen, Schalter

class TestSwitchables:
    def test_light_with_brightness_and_color(self):
        devices = classify(
            state("Decke", "on", "true", dtype="light", state_type=BOOLEAN),
            state("Decke", "level", "60", dtype="light"),
            state("Decke", "color", "#FF8000", dtype="light", state_type=COLOR),
        )

        light = devices["Decke"]
        assert light.device_class == C.DEVICE_CLASS_LIGHT
        assert kinds(light) == {"on": K.SLOT_KIND_ON, "brightness": K.SLOT_KIND_BRIGHTNESS, "color": K.SLOT_KIND_COLOR}
        assert light.slots["on"].value is True
        assert light.slots["brightness"].value == 60.0
        assert light.slots["color"].value == 0xFF8000
        assert light.slots["on"].state_id == f"{BASE}.Decke.on"
        assert (light.room, light.floor, light.name) == ("wohnzimmer", "EG", "Decke")

    def test_plain_light_has_no_brightness_slot(self):
        light = classify(state("Stehlampe", "on", "false", dtype="light", state_type=BOOLEAN))["Stehlampe"]

        assert light.device_class == C.DEVICE_CLASS_LIGHT
        assert not light.has_slot(K.SLOT_KIND_BRIGHTNESS)
        assert light.slots["on"].value is False

    def test_read_only_dimmer_state_does_not_make_a_button_a_lamp(self):
        # Hue-Taster: nur lesbarer `simulated_brightness`, nicht schaltbar (#377)
        button = classify(
            state("Taster", "on", "false", dtype="light", state_type=BOOLEAN, writable=False),
            state("Taster", "level", "0", dtype="light", writable=False),
        )["Taster"]

        assert button.device_class == C.DEVICE_CLASS_GENERIC

    def test_socket_by_function_and_measuring_socket(self):
        devices = classify(
            state("Steckdose", "on", "true", dtype="socket", state_type=BOOLEAN),
            state("Messsteckdose", "on", "true", dtype="socket", state_type=BOOLEAN),
            state("Messsteckdose", "power", "42.5", writable=False),
        )

        assert devices["Steckdose"].device_class == C.DEVICE_CLASS_SOCKET
        measuring = devices["Messsteckdose"]
        assert measuring.device_class == C.DEVICE_CLASS_SOCKET
        assert measuring.slots["power"].kind == K.SLOT_KIND_POWER and measuring.slots["power"].value == 42.5

    def test_power_next_to_a_switch_makes_a_socket_even_without_a_type_hint(self):
        device = classify(
            state("Stecker", "on", "true", state_type=BOOLEAN),
            state("Stecker", "power", "5", writable=False),
        )["Stecker"]

        assert device.device_class == C.DEVICE_CLASS_SOCKET

    def test_scene_and_unknown_switches_are_generic_binary_switches(self):
        devices = classify(
            state("Filmabend", "on", "false", dtype="scene", state_type=BOOLEAN),
            state("Alarm aktiv", "on", "true", state_type=BOOLEAN),
        )

        assert devices["Filmabend"].device_class == C.DEVICE_CLASS_GENERIC_BINARY_SWITCH
        assert devices["Alarm aktiv"].device_class == C.DEVICE_CLASS_GENERIC_BINARY_SWITCH

    def test_a_measurement_only_socket_is_a_sensor(self):
        meter = classify(state("Zähler", "power", "310", writable=False))["Zähler"]

        assert meter.device_class == C.DEVICE_CLASS_SENSOR
        assert meter.slots["power"].kind == K.SLOT_KIND_POWER

    def test_writable_trust_level_is_carried_to_the_slot(self):
        lock = classify(state("Haustür", "on", "false", state_type=BOOLEAN, trust=8))["Haustür"]

        assert lock.slots["on"].required_trust_level == 8


# ------------------------------------------------------------------
# Thermostat und Sensoren

class TestClimateAndSensors:
    def test_room_thermostat_with_setpoint_and_actual_value(self):
        room = classify(
            state("Raumtemperatur", "current", "21.5", dtype="temperature_sensor", writable=False),
            state("Raumtemperatur", "expected", "22", dtype="thermostat"),
        )["Raumtemperatur"]

        assert room.device_class == C.DEVICE_CLASS_THERMOSTAT
        assert kinds(room) == {"temperature": K.SLOT_KIND_TEMPERATURE, "target_temperature": K.SLOT_KIND_TARGET_TEMPERATURE}
        assert room.slots["temperature"].value == 21.5
        assert room.slots["target_temperature"].value == 22.0 and room.slots["target_temperature"].writable

    def test_temperature_and_humidity_in_one_channel_no_longer_collide(self):
        # beide tragen beim v1-Adapter den Key `current`: dort gewinnt der zuletzt geschriebene
        device = classify(
            state("Bad", "TEMP", "23.1", key="current", dtype="temperature_sensor", writable=False),
            state("Bad", "HUM", "48", key="current", dtype="humidity_sensor", writable=False),
        )["Bad"]

        assert device.device_class == C.DEVICE_CLASS_SENSOR
        assert device.slots["temperature"].value == 23.1
        assert device.slots["humidity"].value == 48.0

    def test_thermostat_takes_the_humidity_slot(self):
        device = classify(
            state("Heizung", "current", "20", dtype="temperature_sensor", writable=False),
            state("Heizung", "expected", "21", dtype="thermostat"),
            state("Heizung", "HUM", "40", key="current", dtype="humidity_sensor", writable=False),
        )["Heizung"]

        assert device.device_class == C.DEVICE_CLASS_THERMOSTAT
        assert {"temperature", "humidity", "target_temperature"} <= set(device.slots)

    def test_bosch_comfort_and_eco_setpoints_are_not_the_setpoint(self):
        device = classify(
            state("Bosch", "current", "20", dtype="temperature_sensor", writable=False),
            state("Bosch", "setpointTemperature", "21", key="expected", dtype="thermostat"),
            state("Bosch", "setpointTemperatureForLevelComfort", "22", key="expected", dtype="thermostat"),
            state("Bosch", "setpointTemperatureForLevelEco", "17", key="expected", dtype="thermostat"),
        )["Bosch"]

        assert device.device_class == C.DEVICE_CLASS_THERMOSTAT
        target = device.slots["target_temperature"]
        assert target.state_id.endswith(".setpointTemperature") and target.value == 21.0
        # die anderen gehen nicht verloren, sind aber keine Sollwert-Slots
        assert device.slots["setpointTemperatureForLevelComfort"].kind == K.SLOT_KIND_GENERIC_NUMBER
        assert device.slots["setpointTemperatureForLevelEco"].kind == K.SLOT_KIND_GENERIC_NUMBER

    def test_ambiguous_setpoints_choose_nothing(self):
        device = classify(
            state("Zwei", "current", "20", dtype="temperature_sensor", writable=False),
            state("Zwei", "soll_a", "21", key="expected", dtype="thermostat"),
            state("Zwei", "soll_b", "22", key="expected", dtype="thermostat"),
        )["Zwei"]

        assert not device.has_slot(K.SLOT_KIND_TARGET_TEMPERATURE)
        assert device.device_class == C.DEVICE_CLASS_SENSOR
        assert device.slots["soll_a"].kind == K.SLOT_KIND_GENERIC_NUMBER and device.slots["soll_b"].kind == K.SLOT_KIND_GENERIC_NUMBER

    def test_climate_becomes_a_climate_with_mode_and_fan_slots(self):
        device = classify(
            state("Klima", "on", "true", dtype="climate", state_type=BOOLEAN),
            state("Klima", "mode", "cool", dtype="climate", state_type=ENUM, enum_values=["heat", "cool"]),
            state("Klima", "current", "24", dtype="climate", writable=False),
            state("Klima", "expected", "22", dtype="climate"),
            state("Klima", "fanSpeed", "low", dtype="climate", state_type=ENUM, enum_values=["low", "auto"]),
        )["Klima"]

        assert device.device_class == C.DEVICE_CLASS_CLIMATE
        assert device.slots["mode"].kind == K.SLOT_KIND_MODE and device.slots["mode"].value == "cool"
        assert device.slots["mode"].writable and device.slots["mode"].options == ["cool", "heat"]
        assert device.slots["fan_speed"].kind == K.SLOT_KIND_FAN_SPEED and device.slots["fan_speed"].options == ["auto", "low"]
        assert device.slots["target_temperature"].kind == K.SLOT_KIND_TARGET_TEMPERATURE

    def test_a_mode_state_outside_a_climate_device_stays_generic(self):
        device = classify(
            state("Player", "on", "true", state_type=BOOLEAN),
            state("Player", "mode", "shuffle", state_type=ENUM),
        )["Player"]

        assert device.device_class == C.DEVICE_CLASS_GENERIC_BINARY_SWITCH
        assert device.slots["mode"].kind == K.SLOT_KIND_GENERIC_TEXT

    def test_a_climate_without_a_writable_switch_is_a_thermostat(self):
        device = classify(
            state("Klima", "on", "true", dtype="climate", state_type=BOOLEAN, writable=False),
            state("Klima", "expected", "22", dtype="climate"),
        )["Klima"]

        assert device.device_class == C.DEVICE_CLASS_THERMOSTAT

    @pytest.mark.parametrize("dtype, subtype", [
        ("window", pb.DEVICE_SUBTYPE_WINDOW), ("Fenster", pb.DEVICE_SUBTYPE_WINDOW),
        ("door", pb.DEVICE_SUBTYPE_DOOR), ("Tür", pb.DEVICE_SUBTYPE_DOOR),
        ("contact", pb.DEVICE_SUBTYPE_UNSPECIFIED),
    ])
    def test_a_contact_gets_its_subtype_from_the_type_hint(self, dtype, subtype):
        contact = classify(state("Kontakt", "open", "false", dtype=dtype, state_type=BOOLEAN, writable=False))["Kontakt"]

        assert contact.device_class == C.DEVICE_CLASS_CONTACT
        assert contact.subtype == subtype

    def test_only_contacts_carry_a_subtype(self):
        light = classify(state("Fensterlicht", "on", "true", dtype="window light", state_type=BOOLEAN))["Fensterlicht"]

        assert light.subtype == pb.DEVICE_SUBTYPE_UNSPECIFIED

    def test_temperature_sensor_without_setpoint_stays_a_sensor(self):
        sensor = classify(state("Flur", "current", "19", dtype="temperature_sensor", writable=False))["Flur"]

        assert sensor.device_class == C.DEVICE_CLASS_SENSOR
        assert kinds(sensor) == {"temperature": K.SLOT_KIND_TEMPERATURE}

    def test_air_quality_sensor_has_three_measurements(self):
        sensor = classify(
            state("Luft", "iaq", "98", dtype="air_quality_sensor", writable=False),
            state("Luft", "co2_equiv", "650", dtype="air_quality_sensor", writable=False),
            state("Luft", "voc_equiv", "0.9", dtype="air_quality_sensor", writable=False),
        )["Luft"]

        assert sensor.device_class == C.DEVICE_CLASS_SENSOR
        assert kinds(sensor) == {"iaq": K.SLOT_KIND_IAQ, "co2": K.SLOT_KIND_CO2, "voc": K.SLOT_KIND_VOC}

    def test_illuminance_sensor(self):
        sensor = classify(state("Helligkeit", "illuminance", "320", dtype="illuminance_sensor", writable=False))["Helligkeit"]

        assert sensor.device_class == C.DEVICE_CLASS_SENSOR
        assert sensor.slots["illuminance"].kind == K.SLOT_KIND_ILLUMINANCE


# ------------------------------------------------------------------
# Kontakte und Rollläden

class TestContactsAndCovers:
    @pytest.mark.parametrize("dtype", ["window", "door", ""])
    def test_read_only_open_state_is_a_contact(self, dtype):
        contact = classify(state("Fenster", "open", "true", dtype=dtype, state_type=BOOLEAN, writable=False))["Fenster"]

        assert contact.device_class == C.DEVICE_CLASS_CONTACT
        assert contact.slots["open"].value is True and not contact.slots["open"].writable

    def test_blind_level_is_the_position(self):
        blind = classify(state("Rollo", "level", "30", dtype="blind"))["Rollo"]

        assert blind.device_class == C.DEVICE_CLASS_COVER
        assert kinds(blind) == {"position": K.SLOT_KIND_POSITION}
        assert blind.slots["position"].value == 30.0 and not blind.slots["position"].inverted

    def test_inverted_blind_is_normalized_to_100_is_open(self):
        blind = classify(state("Markise", "level", "100", dtype="blind", inverted=True))["Markise"]

        assert blind.slots["position"].value == 0.0
        assert blind.slots["position"].inverted

    def test_a_non_blind_level_stays_brightness(self):
        device = classify(state("Dimmer", "on", "true", dtype="light", state_type=BOOLEAN),
                          state("Dimmer", "level", "10", dtype="light"))["Dimmer"]

        assert device.slots["brightness"].kind == K.SLOT_KIND_BRIGHTNESS


# ------------------------------------------------------------------
# Farben (ioBroker.hannah#210)

class TestColors:
    def test_two_color_states_are_not_told_apart_known_adapter_limit(self):
        # ioBroker.hannah#210: der v1-Adapter legt auch die Farbtemperatur auf `color`. Core
        # rät nicht, welcher der beiden States RGB trägt.
        light = classify(
            state("Bett", "on", "true", dtype="light", state_type=BOOLEAN),
            state("Bett", "rgb", "#0000FF", key="color", dtype="light", state_type=COLOR),
            state("Bett", "colortemp", "2700", key="color", dtype="light", state_type=COLOR),
        )["Bett"]

        assert light.device_class == C.DEVICE_CLASS_LIGHT
        assert not light.has_slot(K.SLOT_KIND_COLOR) and not light.has_slot(K.SLOT_KIND_COLOR_TEMPERATURE)
        assert set(light.slots) == {"on", "rgb", "colortemp"}

    def test_unnamed_color_duplicates_choose_nothing_but_keep_the_states(self):
        light = classify(
            state("Lampe", "on", "true", dtype="light", state_type=BOOLEAN),
            state("Lampe", "c1", "#FF0000", key="color", dtype="light", state_type=COLOR),
            state("Lampe", "c2", "#00FF00", key="color", dtype="light", state_type=COLOR),
        )["Lampe"]

        assert light.device_class == C.DEVICE_CLASS_LIGHT
        assert not light.has_slot(K.SLOT_KIND_COLOR)
        assert light.slots["c1"].kind == K.SLOT_KIND_GENERIC_TEXT and light.slots["c2"].kind == K.SLOT_KIND_GENERIC_TEXT


# ------------------------------------------------------------------
# Werte, Gruppierung, Pflichtfelder

class TestValuesAndGrouping:
    def test_numeric_color_value_becomes_rgb(self):
        light = classify(
            state("Bosch", "on", "true", dtype="light", state_type=BOOLEAN),
            state("Bosch", "color", "16711680", dtype="light", state_type=COLOR),
        )["Bosch"]

        assert light.slots["color"].value == 0xFF0000

    @pytest.mark.parametrize("raw", ["", "null"])
    def test_unknown_values_are_none(self, raw):
        light = classify(state("Lampe", "on", raw, dtype="light", state_type=BOOLEAN))["Lampe"]

        assert light.slots["on"].value is None

    def test_a_state_without_room_or_device_id_is_not_a_device(self):
        devices = legacy_devices.classify_snapshot([
            state("Wetter", "temp", "12", room=""),
            pb1.AgentDevice(state_id="a.b.on", room="bad", device="Alt", value=pb1.AgentStateValue(value="true")),
            state("Lampe", "on", "true", dtype="light", state_type=BOOLEAN),
        ])

        assert [d.name for d in devices] == ["Lampe"]

    def test_an_unresolved_state_falls_back_to_its_suffix_as_generic_slot(self):
        device = classify(
            state("Lampe", "on", "true", dtype="light", state_type=BOOLEAN),
            state("Lampe", "WORKING", "false", key="", state_type=BOOLEAN, writable=False),
        )["Lampe"]

        assert device.slots["WORKING"].kind == K.SLOT_KIND_GENERIC_BOOL and device.slots["WORKING"].value is False

    def test_separate_devices_in_one_room_stay_separate(self):
        devices = classify(
            state("Decke", "on", "true", dtype="light", state_type=BOOLEAN),
            state("Stehlampe", "on", "true", dtype="light", state_type=BOOLEAN),
        )

        assert set(devices) == {"Decke", "Stehlampe"}

    def test_floor_is_taken_from_the_first_state_that_has_one(self):
        first = state("Decke", "on", "true", dtype="light", state_type=BOOLEAN)
        first.floor = ""
        second = state("Decke", "level", "5", dtype="light")
        second.floor = "OG"

        [device] = legacy_devices.classify_snapshot([first, second])

        assert device.floor == "OG"


# ------------------------------------------------------------------
# Registry und Live-Updates

class TestRegistryAndLiveUpdates:
    def _registry(self, *states) -> DeviceRegistry:
        registry = DeviceRegistry()
        legacy_devices.load_snapshot(registry, list(states))
        return registry

    def test_snapshot_fills_the_registry_and_replaces_the_previous_one(self):
        registry = self._registry(state("Decke", "on", "true", dtype="light", state_type=BOOLEAN))
        assert [d.name for d in registry.devices()] == ["Decke"]

        legacy_devices.load_snapshot(registry, [state("Bad", "on", "true", dtype="light", state_type=BOOLEAN)])

        assert [d.name for d in registry.devices()] == ["Bad"]

    def test_live_update_changes_the_slot_value(self):
        registry = self._registry(
            state("Decke", "on", "true", dtype="light", state_type=BOOLEAN),
            state("Decke", "level", "60", dtype="light"),
        )

        assert legacy_devices.apply_state_update(registry, f"{BASE}.Decke.on", "false")
        assert legacy_devices.apply_state_update(registry, f"{BASE}.Decke.level", "25")

        slots = registry.get(f"{BASE}.Decke").slots
        assert slots["on"].value is False
        assert slots["brightness"].value == 25.0

    def test_live_update_normalizes_like_the_snapshot(self):
        registry = self._registry(
            state("Markise", "level", "100", dtype="blind", inverted=True),
            state("Lampe", "on", "true", dtype="light", state_type=BOOLEAN),
            state("Lampe", "color", "#000000", dtype="light", state_type=COLOR),
        )

        legacy_devices.apply_state_update(registry, f"{BASE}.Markise.level", "20")
        legacy_devices.apply_state_update(registry, f"{BASE}.Lampe.color", "#FF0000")

        assert registry.get(f"{BASE}.Markise").slots["position"].value == 80.0
        assert registry.get(f"{BASE}.Lampe").slots["color"].value == 0xFF0000

    def test_live_update_of_an_unknown_state_is_not_handled(self):
        registry = self._registry(state("Decke", "on", "true", dtype="light", state_type=BOOLEAN))

        assert legacy_devices.apply_state_update(registry, "0_userdata.0.feeded", "true") is False


# ------------------------------------------------------------------
# Verteilung gegen den Echtdaten-Export (#377)

def _census_snapshot() -> list:
    states = []
    for i in range(35):   # 35 Lichter: mit Farbe, nur dimmbar, nur schaltbar
        name = f"Licht{i}"
        states.append(state(name, "on", "true", dtype="light", state_type=BOOLEAN))
        if i < 26:
            states.append(state(name, "level", "50", dtype="light"))
            states.append(state(name, "color", "#FFFFFF", dtype="light", state_type=COLOR))
        elif i < 30:
            states.append(state(name, "level", "50", dtype="light"))
    for i in range(6):    # 6 Steckdosen (über Function + State-Name `on` gefunden)
        states.append(state(f"Steckdose{i}", "on", "true", dtype="socket", state_type=BOOLEAN))
        if i < 2:
            states.append(state(f"Steckdose{i}", "power", "3", writable=False))
    for i in range(4):    # 4 Raumthermostate mit Ist und Soll, bisher `temperature_sensor`
        states.append(state(f"Thermostat{i}", "current", "21", dtype="temperature_sensor", writable=False))
        states.append(state(f"Thermostat{i}", "expected", "22", dtype="thermostat"))
    for i in range(2):    # 2 Temperatursensoren ohne Sollwert
        states.append(state(f"Temperatur{i}", "current", "19", dtype="temperature_sensor", writable=False))
    for i in range(2):    # 2 Luftqualitäts-Sensoren (Typ-Override)
        for key in ("iaq", "co2_equiv", "voc_equiv"):
            states.append(state(f"Luft{i}", key, "1", dtype="air_quality_sensor", writable=False))
    for i in range(2):    # 2 "Szenen" (Function), für Hannah normale Schalter
        states.append(state(f"Szene{i}", "on", "false", dtype="scene", state_type=BOOLEAN))
    for i in range(13):   # 13 Kontakte
        states.append(state(f"Fenster{i}", "open", "false", dtype="window" if i % 2 else "door",
                            state_type=BOOLEAN, writable=False))
    return states


def test_classification_of_the_real_data_census():
    devices = legacy_devices.classify_snapshot(_census_snapshot())

    assert Counter(C.Name(d.device_class) for d in devices) == {
        "DEVICE_CLASS_LIGHT": 35,
        "DEVICE_CLASS_SOCKET": 6,
        "DEVICE_CLASS_THERMOSTAT": 4,
        "DEVICE_CLASS_SENSOR": 4,                    # 2 Temperatursensoren + 2 Luftqualität
        "DEVICE_CLASS_GENERIC_BINARY_SWITCH": 2,
        "DEVICE_CLASS_CONTACT": 13,
    }
    lights = [d for d in devices if d.device_class == C.DEVICE_CLASS_LIGHT]
    assert sum(d.has_slot(K.SLOT_KIND_COLOR) for d in lights) == 26
    assert sum(d.has_slot(K.SLOT_KIND_BRIGHTNESS) for d in lights) == 30
    assert all(d.has_slot(K.SLOT_KIND_TARGET_TEMPERATURE) and d.has_slot(K.SLOT_KIND_TEMPERATURE)
               for d in devices if d.device_class == C.DEVICE_CLASS_THERMOSTAT)
