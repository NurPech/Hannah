from hannah.ble_location import BleLocationEngine


class TestTagLoading:
    """#115: BleTag ist jetzt ein eigenes DB-Modell mit user_id direkt als FK-Spalte —
    BleLocationEngine bekommt user_id daher schon aufgelöst rein (kein Username-Lookup
    mehr nötig, das übernimmt hannah.ble_tags.BleTagManager/das Model selbst)."""

    def test_tag_with_user_id_is_loaded(self):
        engine = BleLocationEngine(
            {"tags": [{"mac_address": "AA:BB:CC:DD:EE:FF", "label": "leonie", "user_id": 42}]},
            get_satellite_room=lambda _d: None,
        )

        tag = engine._tags["aa:bb:cc:dd:ee:ff"]
        assert tag.mac == "aa:bb:cc:dd:ee:ff"
        assert tag.label == "leonie"
        assert tag.user_id == 42

    def test_tag_without_user_id_is_valid(self):
        """Tags ohne Owner sind valide (reines Location-Tracking, keine Resident-Bindung)."""
        engine = BleLocationEngine(
            {"tags": [{"mac_address": "AA:BB:CC:DD:EE:FF", "label": "keychain"}]},
            get_satellite_room=lambda _d: None,
        )

        tag = engine._tags["aa:bb:cc:dd:ee:ff"]
        assert tag.user_id is None

    def test_legacy_mac_key_still_accepted(self):
        """"mac" (statt "mac_address") wird weiterhin akzeptiert."""
        engine = BleLocationEngine(
            {"tags": [{"mac": "AA:BB:CC:DD:EE:FF", "label": "leonie", "user_id": 7}]},
            get_satellite_room=lambda _d: None,
        )

        tag = engine._tags["aa:bb:cc:dd:ee:ff"]
        assert tag.user_id == 7


class TestSightingHandler:
    """#437: Die Presence-Fusion braucht jede Sichtung, nicht nur Standortwechsel —
    sonst gilt ein durchgehend gesehener Tag nach ble_staleness_seconds als weg."""

    def _engine(self):
        return BleLocationEngine(
            {"tags": [{"mac_address": "AA:BB:CC:DD:EE:FF", "label": "leonie", "user_id": 42}]},
            get_satellite_room=lambda _d: "Büro",
        )

    def test_sighting_fires_on_every_report_even_without_location_change(self):
        engine = self._engine()
        sightings, changes = [], []
        engine.set_sighting_handler(sightings.append)
        engine.set_location_change_handler(lambda *a: changes.append(a))

        for _ in range(3):
            engine.on_report("bu01", "aa:bb:cc:dd:ee:ff", -60)

        assert [t.user_id for t in sightings] == [42, 42, 42]
        assert len(changes) == 1  # derselbe Satellit: nur die erste Sichtung ist ein Wechsel

    def test_report_of_unknown_tag_is_ignored(self):
        engine = self._engine()
        sightings = []
        engine.set_sighting_handler(sightings.append)

        engine.on_report("bu01", "11:22:33:44:55:66", -60)

        assert sightings == []

    def test_failing_sighting_handler_does_not_break_report(self):
        engine = self._engine()
        changes = []
        engine.set_location_change_handler(lambda *a: changes.append(a))

        def boom(_tag):
            raise RuntimeError("x")

        engine.set_sighting_handler(boom)
        engine.on_report("bu01", "aa:bb:cc:dd:ee:ff", -60)

        assert len(changes) == 1
