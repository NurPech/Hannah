"""Haussteuerungsmenü aus Klasse und Slots (hannah.v2)."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from hannah_proto.v2 import hannah_pb2

from hannah_telegram import device_menu
from hannah_telegram.bot import HannahBot, _cb_ctrl

pb = hannah_pb2


def _slot(slot_id, kind, value=None, writable=False, options=(), label=""):
    slot = pb.Slot(slot_id=slot_id, kind=kind, writable=writable, options=list(options), label=label)
    if isinstance(value, bool):
        slot.value.boolean = value
    elif isinstance(value, str):
        slot.value.text = value
    elif value is not None:
        slot.value.number = float(value)
    return slot


def _device(name, device_class, slots, subtype=pb.DEVICE_SUBTYPE_UNSPECIFIED):
    return pb.DeviceInfo(id=f"x.{name}", name=name, device_class=device_class, slots=slots, subtype=subtype)


def _light(on=True, brightness=60):
    return _device("Lampe", pb.DEVICE_CLASS_LIGHT, [
        _slot("on", pb.SLOT_KIND_ON, on, writable=True),
        _slot("brightness", pb.SLOT_KIND_BRIGHTNESS, brightness, writable=True),
        _slot("color", pb.SLOT_KIND_COLOR, writable=True),
        _slot("color_temperature", pb.SLOT_KIND_COLOR_TEMPERATURE, 2700, writable=True),
    ])


class TestIconAndDot:
    def test_icon_follows_the_class(self):
        assert device_menu.device_icon(_light()) == "💡"
        assert device_menu.device_icon(_device("S", pb.DEVICE_CLASS_SOCKET, [])) == "🔌"
        assert device_menu.device_icon(_device("X", pb.DEVICE_CLASS_GENERIC, [])) == "⚙️"

    def test_contact_icon_follows_the_subtype(self):
        door = _device("T", pb.DEVICE_CLASS_CONTACT, [], subtype=pb.DEVICE_SUBTYPE_DOOR)
        window = _device("F", pb.DEVICE_CLASS_CONTACT, [], subtype=pb.DEVICE_SUBTYPE_WINDOW)

        assert (device_menu.device_icon(door), device_menu.device_icon(window)) == ("🚪", "🪟")

    def test_dot_shows_the_on_slot(self):
        assert device_menu.device_dot(_light(on=True)) == "🟢"
        assert device_menu.device_dot(_light(on=False)) == "🔴"

    def test_dot_is_unknown_without_an_on_slot_or_value(self):
        sensor = _device("S", pb.DEVICE_CLASS_SENSOR, [_slot("temperature", pb.SLOT_KIND_TEMPERATURE, 21.0)])
        unknown = _device("L", pb.DEVICE_CLASS_LIGHT, [_slot("on", pb.SLOT_KIND_ON, writable=True)])

        assert device_menu.device_dot(sensor) == "⚫"
        assert device_menu.device_dot(unknown) == "⚫"


class TestStatusText:
    def test_light(self):
        text = device_menu.device_status_text(_light(on=True, brightness=60))

        assert text.splitlines()[0] == "*Lampe* (Licht)"
        assert "Status: 🟢 an" in text and "Helligkeit: 60%" in text and "Farbtemperatur: 2700 K" in text

    def test_thermostat_names_actual_and_target(self):
        dev = _device("Heizung", pb.DEVICE_CLASS_THERMOSTAT, [
            _slot("temperature", pb.SLOT_KIND_TEMPERATURE, 20.5),
            _slot("target_temperature", pb.SLOT_KIND_TARGET_TEMPERATURE, 21, writable=True),
        ])

        text = device_menu.device_status_text(dev)

        assert "Ist: 20.5°" in text and "Soll: 21.0°" in text

    def test_contact_uses_its_subtype(self):
        dev = _device("Bad", pb.DEVICE_CLASS_CONTACT, [_slot("open", pb.SLOT_KIND_OPEN, True)],
                      subtype=pb.DEVICE_SUBTYPE_WINDOW)

        text = device_menu.device_status_text(dev)

        assert "(Fenster)" in text and "Fenster: offen" in text

    def test_air_quality_sensor(self):
        dev = _device("Luft", pb.DEVICE_CLASS_SENSOR, [
            _slot("iaq", pb.SLOT_KIND_IAQ, 92),
            _slot("co2", pb.SLOT_KIND_CO2, 911),
            _slot("voc", pb.SLOT_KIND_VOC, 1.27),
        ])

        text = device_menu.device_status_text(dev)

        assert "Luftqualität: okay" in text and "CO₂: 911.0 ppm" in text and "VOC: 1.27 ppm" in text

    def test_climate_shows_mode_and_fan(self):
        dev = _device("Klima", pb.DEVICE_CLASS_CLIMATE, [
            _slot("on", pb.SLOT_KIND_ON, True, writable=True),
            _slot("mode", pb.SLOT_KIND_MODE, "cool", writable=True),
            _slot("fan_speed", pb.SLOT_KIND_FAN_SPEED, "auto", writable=True),
        ])

        text = device_menu.device_status_text(dev)

        assert "Modus: cool" in text and "Lüfter: auto" in text

    def test_slots_without_a_value_are_left_out(self):
        text = device_menu.device_status_text(_light(on=True, brightness=60))

        assert [line for line in text.splitlines() if line.startswith("Farbe")] == []

    def test_generic_slot_uses_its_label(self):
        dev = _device("X", pb.DEVICE_CLASS_GENERIC, [_slot("a_2", pb.SLOT_KIND_GENERIC_NUMBER, 7, label="Zähler")])

        assert "Zähler: 7" in device_menu.device_status_text(dev)

    def test_underscores_in_slot_names_are_escaped(self):
        # Zigbee2MQTT-Slots heißen link_quality, send_payload, ...; ein Unterstrich ohne Gegenstück
        # (hier fünf) lässt Telegram die ganze Nachricht ablehnen ("can't find end of the entity")
        names = ["link_quality", "send_payload", "brightness_step", "brightness_move", "state_toggle"]
        dev = _device("Flur Keller 1", pb.DEVICE_CLASS_LIGHT, [
            _slot("on", pb.SLOT_KIND_ON, False, writable=True),
            *[_slot(name, pb.SLOT_KIND_GENERIC_NUMBER, 1) for name in names],
        ])

        text = device_menu.device_status_text(dev)

        assert [line for line in text.splitlines() if "_" in line and "\\_" not in line] == []
        assert "link\\_quality: 1" in text and "state\\_toggle: 1" in text

    def test_markdown_characters_in_names_and_values_are_escaped(self):
        dev = _device("Lampe *1* [alt]", pb.DEVICE_CLASS_GENERIC, [
            _slot("effect", pb.SLOT_KIND_GENERIC_TEXT, "a_b `c`"),
        ])

        text = device_menu.device_status_text(dev)

        assert text.splitlines()[0].startswith("*Lampe \\*1\\* \\[alt]*")
        assert "effect: a\\_b \\`c\\`" in text

    def test_escaping_leaves_plain_text_alone(self):
        assert device_menu.escape_markdown("Küche 1: an") == "Küche 1: an"


class TestControlRows:
    def _labels(self, rows):
        return [[label for label, _slot_id, _value in row] for row in rows]

    def test_a_light_gets_switch_dimmer_color_and_color_temperature(self):
        rows = device_menu.control_rows(_light(on=True, brightness=50))

        assert rows[0] == [("⏹ Ausschalten", "on", "false")]
        assert self._labels(rows)[1] == ["·25%", "▶50%", "·75%", "·100%"]
        assert [slot_id for _l, slot_id, _v in rows[2]] == ["color"] * 5
        assert [slot_id for _l, slot_id, _v in rows[3]] == ["color_temperature"] * 3

    def test_an_off_device_offers_to_switch_on(self):
        assert device_menu.control_rows(_light(on=False))[0] == [("✅ Einschalten", "on", "true")]

    def test_a_light_without_brightness_has_no_dimmer(self):
        dev = _device("L", pb.DEVICE_CLASS_LIGHT, [_slot("on", pb.SLOT_KIND_ON, False, writable=True)])

        assert device_menu.control_rows(dev) == [[("✅ Einschalten", "on", "true")]]

    def test_read_only_slots_get_no_buttons(self):
        dev = _device("L", pb.DEVICE_CLASS_LIGHT, [
            _slot("on", pb.SLOT_KIND_ON, False, writable=True),
            _slot("brightness", pb.SLOT_KIND_BRIGHTNESS, 10, writable=False),
        ])

        assert len(device_menu.control_rows(dev)) == 1

    def test_sensors_have_no_controls(self):
        dev = _device("S", pb.DEVICE_CLASS_SENSOR, [_slot("temperature", pb.SLOT_KIND_TEMPERATURE, 21)])

        assert device_menu.control_rows(dev) == []

    def test_a_cover_gets_open_half_and_closed(self):
        dev = _device("R", pb.DEVICE_CLASS_COVER, [_slot("position", pb.SLOT_KIND_POSITION, 30, writable=True)])

        [row] = device_menu.control_rows(dev)

        assert [(label, value) for label, _s, value in row] == [("⬆ Auf", "100"), ("50%", "50"), ("⬇ Zu", "0")]

    def test_climate_offers_the_modes_and_fan_levels_of_the_device(self):
        dev = _device("K", pb.DEVICE_CLASS_CLIMATE, [
            _slot("mode", pb.SLOT_KIND_MODE, "cool", writable=True, options=["cool", "dry"]),
            _slot("fan_speed", pb.SLOT_KIND_FAN_SPEED, "auto", writable=True,
                  options=["auto", "low", "high", "medium"]),
        ])

        rows = device_menu.control_rows(dev)

        assert self._labels(rows) == [["▶cool", "·dry"], ["▶auto", "·low", "·high"], ["·medium"]]

    def test_callback_data_stays_below_the_telegram_limit(self):
        for row in device_menu.control_rows(_light()):
            for _label, slot_id, value in row:
                assert len(_cb_ctrl(99, 99, slot_id, value).encode()) <= 64


class TestSlotValues:
    def _slot_of(self, kind):
        return pb.Slot(slot_id="s", kind=kind)

    def test_typed_by_the_kind_of_the_slot(self):
        assert device_menu.slot_value_from_text(self._slot_of(pb.SLOT_KIND_ON), "true").boolean is True
        assert device_menu.slot_value_from_text(self._slot_of(pb.SLOT_KIND_ON), "false").boolean is False
        assert device_menu.slot_value_from_text(self._slot_of(pb.SLOT_KIND_BRIGHTNESS), "40").number == 40
        assert device_menu.slot_value_from_text(self._slot_of(pb.SLOT_KIND_COLOR), "#0096FF").rgb == 0x0096FF
        assert device_menu.slot_value_from_text(self._slot_of(pb.SLOT_KIND_MODE), "cool").text == "cool"

    @pytest.mark.parametrize("kind, text", [
        (pb.SLOT_KIND_ON, "vielleicht"),
        (pb.SLOT_KIND_COLOR, "rot"),
        (pb.SLOT_KIND_BRIGHTNESS, "hell"),
    ])
    def test_an_unfitting_text_is_refused(self, kind, text):
        with pytest.raises(ValueError):
            device_menu.slot_value_from_text(self._slot_of(kind), text)

    def test_finds_a_slot_by_its_id(self):
        dev = _light()

        assert device_menu.find_slot(dev, "brightness").kind == pb.SLOT_KIND_BRIGHTNESS
        assert device_menu.find_slot(dev, "nope") is None


class TestHausControlCallback:
    """haus:c:{room}:{dev}:{slot}:{value} → ControlDevice on the slot, typed."""

    def _bot_with(self, dev, control_ok=True):
        hannah = MagicMock()
        user = MagicMock(trust_level=9)
        hannah.get_user_by_telegram = AsyncMock(return_value=(True, user))
        hannah.get_devices = AsyncMock(return_value=pb.GetDevicesResponse(
            rooms=[pb.RoomInfo(key="wz", name="Wohnzimmer", devices=[dev])]))
        hannah.control_device = AsyncMock(return_value=(control_ok, "OK" if control_ok else "Nein"))
        return HannahBot(token="t", hannah=hannah), hannah

    def _query(self):
        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        return query

    async def test_sends_the_typed_value_of_the_slot(self):
        bot, hannah = self._bot_with(_light())
        query = self._query()

        await bot._on_haus_callback(query, "42", "haus:c:0:0:brightness:40")

        device_id, slot_id, value, chat_id = hannah.control_device.call_args.args
        assert (device_id, slot_id, chat_id) == ("x.Lampe", "brightness", "42")
        assert value.number == 40
        query.answer.assert_any_call("✅ Befehl gesendet.")

    async def test_a_colour_goes_out_as_rgb(self):
        bot, hannah = self._bot_with(_light())

        await bot._on_haus_callback(self._query(), "42", "haus:c:0:0:color:#FF0000")

        assert hannah.control_device.call_args.args[2].rgb == 0xFF0000

    async def test_a_slot_the_device_no_longer_has_is_refused(self):
        bot, hannah = self._bot_with(_light())
        query = self._query()

        await bot._on_haus_callback(query, "42", "haus:c:0:0:level:40")

        hannah.control_device.assert_not_called()
        assert query.answer.call_args.kwargs.get("show_alert") is True

    async def test_a_refusal_of_core_is_shown(self):
        bot, _hannah = self._bot_with(_light(), control_ok=False)
        query = self._query()

        await bot._on_haus_callback(query, "42", "haus:c:0:0:on:true")

        query.answer.assert_any_call("Fehler: Nein", show_alert=True)
