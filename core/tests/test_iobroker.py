import pytest
from hannah.iobroker import _camel_to_words, _iaq_label, IoBrokerClient, Device
from hannah_proto.hannah_pb2 import AgentDevice, AgentStateValue


class TestCamelToWords:
    def test_camel_case(self):
        assert _camel_to_words("DeckeSeite") == "decke seite"

    def test_underscore(self):
        assert _camel_to_words("Zimmer_Sued") == "zimmer süd"

    def test_number_suffix(self):
        assert _camel_to_words("Deckenlampe_Spot1") == "deckenlampe spot 1"

    def test_umlaut_ae(self):
        assert _camel_to_words("BueroRene") == "büro rene"

    def test_single_word(self):
        assert _camel_to_words("Wohnzimmer") == "wohnzimmer"

    def test_sued_to_sued(self):
        assert _camel_to_words("Sued") == "süd"

    def test_multiple_uppercase(self):
        assert _camel_to_words("EG") == "e g"


class TestParsePayload:
    def test_true(self):
        assert IoBrokerClient._parse_payload("true") is True

    def test_true_mixed_case(self):
        assert IoBrokerClient._parse_payload("True") is True

    def test_false(self):
        assert IoBrokerClient._parse_payload("false") is False

    def test_integer(self):
        result = IoBrokerClient._parse_payload("42")
        assert result == 42
        assert isinstance(result, int)

    def test_negative_integer(self):
        assert IoBrokerClient._parse_payload("-5") == -5

    def test_float(self):
        assert IoBrokerClient._parse_payload("3.14") == pytest.approx(3.14)

    def test_string(self):
        assert IoBrokerClient._parse_payload("hello") == "hello"

    def test_whitespace_stripped(self):
        assert IoBrokerClient._parse_payload("  42  ") == 42

    def test_empty_string(self):
        assert IoBrokerClient._parse_payload("") == ""


class TestIaqLabel:
    def test_good(self):
        assert _iaq_label(50) == "gut"

    def test_okay(self):
        assert _iaq_label(75) == "okay"
        assert _iaq_label(100) == "okay"

    def test_slightly_polluted(self):
        assert _iaq_label(101) == "leicht belastet"
        assert _iaq_label(150) == "leicht belastet"

    def test_bad(self):
        assert _iaq_label(151) == "schlecht"
        assert _iaq_label(286) == "schlecht"


class TestDescribeCategoryAirQuality:
    @pytest.fixture
    def client(self):
        return IoBrokerClient({"host": "localhost", "port": 8093})

    def _device(self, **current):
        return Device(
            id="hannah.0.satellites.sensors.kueche-esp",
            name="Kueche",
            key="kueche",
            room="kueche",
            room_display_name="Küche",
            floor="EG",
            category="air_quality_sensor",
            current=current,
        )

    def test_full_reading(self, client):
        dev = self._device(iaq=286.0, co2_equiv=1654.0, voc_equiv=6.85)
        result = client._describe_category("air_quality_sensor", [dev], "Küche")
        assert "schlecht" in result
        assert "1654.0 ppm" in result
        assert "6.8 ppm" in result or "6.9 ppm" in result

    def test_uncalibrated_defaults(self, client):
        dev = self._device(iaq=50.0, co2_equiv=500.0, voc_equiv=0.5)
        result = client._describe_category("air_quality_sensor", [dev], "Küche")
        assert "gut" in result

    def test_unknown_category_returns_none(self, client):
        assert client._describe_category("does_not_exist", [], "Küche") is None


class TestDescribeCategoryHumidity:
    @pytest.fixture
    def client(self):
        return IoBrokerClient({"host": "localhost", "port": 8093})

    def _device(self, **current):
        return Device(
            id="javascript.0.virtualDevice.Luftfeuchtigkeit.OG.Schlafzimmer.Raumfeuchte",
            name="Raumfeuchte",
            key="raumfeuchte",
            room="schlafzimmer",
            room_display_name="Schlafzimmer",
            floor="OG",
            category="humidity_sensor",
            current=current,
        )

    def test_reading(self, client):
        dev = self._device(current=54.3)
        result = client._describe_category("humidity_sensor", [dev], "Schlafzimmer")
        assert "54.3 %" in result


class TestSocketPowerQuery:
    """#121 — "socket" hat als einzige _CATEGORY_STATES-Kategorie zusätzlich einen
    regulären on/off-Schaltzustand. Die Watt-Antwort darf normale an/aus-Abfragen
    nicht kapern (Regression: naive Umsetzung ließ z.B. "ist die Steckdose an?"
    nur noch die Watt-Zahl beantworten), greift nur bei qs=="power"."""

    @pytest.fixture
    def client(self):
        return IoBrokerClient({"host": "localhost", "port": 8093})

    def _device(self, **current):
        return Device(
            id="javascript.0.virtualDevice.Stecker.OG.Schlafzimmer.Computer",
            name="PC",
            key="pc",
            room="schlafzimmer",
            room_display_name="Schlafzimmer",
            floor="OG",
            category="socket",
            current=current,
        )

    def test_describe_category_reports_watt(self, client):
        dev = self._device(power=42.0)
        result = client._describe_category("socket", [dev], "Schlafzimmer")
        assert "42.0 Watt" in result

    def test_category_query_applies_only_for_power(self, client):
        assert client._category_query_applies("socket", "power") is True
        assert client._category_query_applies("socket", "on") is False
        assert client._category_query_applies("socket", None) is False

    def test_other_categories_unaffected_by_guard(self, client):
        """Kategorien ohne konkurrierenden on/off-Zustand greifen weiter unabhängig von qs."""
        assert client._category_query_applies("temperature_sensor", None) is True
        assert client._category_query_applies("temperature_sensor", "on") is True
        assert client._category_query_applies("does_not_exist", "power") is False

    def test_describe_device_power_query_returns_watt(self, client):
        dev = self._device(on=True, power=42.0)
        result = client._describe_device(dev, "power")
        assert "42.0 Watt" in result

    def test_describe_device_on_query_returns_status_not_watt(self, client):
        """Regression-Guard: 'ist die Steckdose an?' (qs='on') darf nicht in die
        Watt-Kategorie-Antwort abbiegen."""
        dev = self._device(on=True, power=42.0)
        result = client._describe_device(dev, "on")
        assert "an" in result
        assert "Watt" not in result

    def test_describe_device_default_still_mentions_power(self, client):
        """Der bestehende on/off-Fallback hängt Watt weiterhin optional in Klammern an."""
        dev = self._device(on=True, power=42.0)
        result = client._describe_device(dev, None)
        assert "an" in result
        assert "42.0 W" in result or "42 W" in result


class TestHandleDeviceSnapshotCategoryMerge:
    """#133 — ein Geschwister-State ohne erkennbare Kategorie (z.B. ein power-Meter-State
    ohne passende Role/Funktion, resolveType() liefert '') darf die Kategorie eines
    bereits erkannten Geschwister-States (z.B. der 'on'-State eines Steckdosen-Geräts,
    resolveType() liefert 'socket') nicht überschreiben — erster nicht-leerer Wert gewinnt,
    unabhängig von der Verarbeitungsreihenfolge im Snapshot."""

    def _device_msg(self, state: str, device_type: str) -> AgentDevice:
        return AgentDevice(
            state_id=f"javascript.0.virtualDevice.Stecker.OG.Schlafzimmer.Computer.{state}",
            room="schlafzimmer",
            device="Computer",
            device_type=device_type,
            value=AgentStateValue(value="false", ack=True),
            room_names={"de": "Schlafzimmer"},
        )

    def test_category_less_state_does_not_blank_out_a_resolved_sibling(self):
        client = IoBrokerClient({"host": "localhost", "port": 8093})
        # power (kategorielos) zuerst, on (socket) danach
        client.handle_device_snapshot([
            self._device_msg("power", ""),
            self._device_msg("on", "socket"),
        ])

        dev = client._devices_by_id["javascript.0.virtualDevice.Stecker.OG.Schlafzimmer.Computer"]
        assert dev.category == "socket"

    def test_resolved_category_survives_regardless_of_order(self):
        client = IoBrokerClient({"host": "localhost", "port": 8093})
        # on (socket) zuerst, power (kategorielos) danach
        client.handle_device_snapshot([
            self._device_msg("on", "socket"),
            self._device_msg("power", ""),
        ])

        dev = client._devices_by_id["javascript.0.virtualDevice.Stecker.OG.Schlafzimmer.Computer"]
        assert dev.category == "socket"

    def test_first_non_empty_category_wins_on_conflict(self):
        """Kein Flip-Flop bei widersprüchlichen nicht-leeren Kategorien zwischen
        Geschwister-States — der erste nicht-leere Wert bleibt bestehen."""
        client = IoBrokerClient({"host": "localhost", "port": 8093})
        client.handle_device_snapshot([
            self._device_msg("on", "socket"),
            self._device_msg("power", "temperature_sensor"),
        ])

        dev = client._devices_by_id["javascript.0.virtualDevice.Stecker.OG.Schlafzimmer.Computer"]
        assert dev.category == "socket"


class TestHandleStateUpdate:
    """Regression: live updates go through state_names reverse-lookup, the initial
    gRPC snapshot does not — a suffix missing from state_names freezes that field
    forever after the first snapshot (Refs #21 follow-up bug). Now logged (once per
    suffix) instead of silently dropped, so a stale config.yaml deployment shows up
    in the logs instead of just quietly freezing values."""

    @pytest.fixture
    def client(self):
        return IoBrokerClient({
            "host": "localhost",
            "port": 8093,
            "state_names": {"iaq": "iaq", "co2_equiv": "co2_equiv", "on": "on"},
        })

    def _device(self, device_id):
        dev = Device(
            id=device_id, name="Sofaecke", key="sofaecke",
            room="wohnzimmer", room_display_name="Wohnzimmer", floor="EG",
            category="air_quality_sensor",
        )
        return dev

    def test_mapped_suffix_updates_cache(self, client):
        device_id = "javascript.0.virtualDevice.AirQuality.EG.Wohnzimmer.Sofaecke"
        dev = self._device(device_id)
        client._devices_by_id[device_id] = dev

        client.handle_state_update(f"{device_id}.iaq", "98")

        assert dev.current["iaq"] == 98

    def test_unmapped_suffix_is_dropped_but_logged(self, client, caplog):
        device_id = "javascript.0.virtualDevice.AirQuality.EG.Wohnzimmer.Sofaecke"
        dev = self._device(device_id)
        dev.current["voc_equiv"] = 0.5  # value from the initial snapshot
        client._devices_by_id[device_id] = dev

        with caplog.at_level("WARNING"):
            client.handle_state_update(f"{device_id}.voc_equiv", "0.95")

        # "voc_equiv" is missing from state_names in this client's config —
        # the live update never reaches the cache, snapshot value stays frozen.
        assert dev.current["voc_equiv"] == 0.5
        # ...but it's no longer silent — exactly this kind of deployment gap
        # (config.yaml missing a state_names entry the code already expects)
        # is what caused the live air-quality values to freeze in production.
        assert "voc_equiv" in caplog.text
        assert "state_names" in caplog.text

    def test_unmapped_suffix_warning_logged_only_once(self, client, caplog):
        device_id = "javascript.0.virtualDevice.AirQuality.EG.Wohnzimmer.Sofaecke"
        dev = self._device(device_id)
        client._devices_by_id[device_id] = dev

        with caplog.at_level("WARNING"):
            client.handle_state_update(f"{device_id}.voc_equiv", "0.9")
            client.handle_state_update(f"{device_id}.voc_equiv", "0.95")

        assert sum("voc_equiv" in r.message for r in caplog.records) == 1


class TestGetStateRaw:
    @pytest.fixture
    def client(self):
        return IoBrokerClient({"host": "localhost", "port": 8093})

    def test_state_cache_hit(self, client):
        client._state_cache["openweathermap.0.current.temperature"] = 21.5
        assert client.get_state_raw("openweathermap.0.current.temperature") == "21.5"

    def test_state_cache_none_value(self, client):
        client._state_cache["some.state"] = None
        assert client.get_state_raw("some.state") is None

    def test_unknown_state_returns_none(self, client):
        assert client.get_state_raw("nonexistent.0.state") is None

    def test_device_state_hit(self, client):
        dev = Device(
            id="virtualDevice.Licht.EG.Wohnzimmer.Decke",
            name="Decke", key="decke",
            room="wohnzimmer", room_display_name="Wohnzimmer", floor="EG", category="Licht",
        )
        dev.current["on"] = True
        client._devices_by_id["virtualDevice.Licht.EG.Wohnzimmer.Decke"] = dev
        assert client.get_state_raw("virtualDevice.Licht.EG.Wohnzimmer.Decke.on") == "True"

    def test_device_state_missing_suffix_returns_none(self, client):
        dev = Device(
            id="virtualDevice.Licht.EG.Wohnzimmer.Decke",
            name="Decke", key="decke",
            room="wohnzimmer", room_display_name="Wohnzimmer", floor="EG", category="Licht",
        )
        client._devices_by_id["virtualDevice.Licht.EG.Wohnzimmer.Decke"] = dev
        assert client.get_state_raw("virtualDevice.Licht.EG.Wohnzimmer.Decke.level") is None

    def test_state_cache_takes_priority_over_device(self, client):
        client._state_cache["virtualDevice.Licht.EG.Wohnzimmer.Decke.on"] = False
        dev = Device(
            id="virtualDevice.Licht.EG.Wohnzimmer.Decke",
            name="Decke", key="decke",
            room="wohnzimmer", room_display_name="Wohnzimmer", floor="EG", category="Licht",
        )
        dev.current["on"] = True
        client._devices_by_id["virtualDevice.Licht.EG.Wohnzimmer.Decke"] = dev
        assert client.get_state_raw("virtualDevice.Licht.EG.Wohnzimmer.Decke.on") == "False"
