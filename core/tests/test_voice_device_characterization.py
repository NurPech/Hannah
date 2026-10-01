"""#387 Schritt 0: Sicherheitsnetz für den Umzug der NLU/Abfragen auf die typisierte Registry.

Friert das heutige Verhalten der Sprachbefehle für Geräte ein: eine synthetische Wohnung nach
den Echtdaten-Mustern aus #377/#387 (Namen erfunden), dazu Sätze, die durch `NLU.parse` →
`IoBrokerClient.execute` (gesetzte States) bzw. `answer_query` (Antworttext) laufen.

Wer in einem späteren Schritt eine Erwartung ändert, tut das mit Kommentar, und nur dort, wo der
alte Pfad einen Fehler hatte (Raumthermostat ohne Sollwert, `color`-Kollision, stilles Ignorieren
fehlender Slots). Diese Stellen sind unten mit `# ÄNDERT SICH` markiert.
"""
import json

import pytest
from hannah_proto.v1 import hannah_pb2 as pb1

from hannah import device_answers, legacy_devices, nlu_devices
from hannah.device_control import DeviceController
from hannah.iobroker import IoBrokerClient
from hannah.nlu import NLU
from hannah.settings_manager import DEFAULT_NLU_SETTINGS
from hannah.typed_devices import DeviceRegistry

BOOLEAN, NUMERIC, ENUM, COLOR = (pb1.StateType.BOOLEAN, pb1.StateType.NUMERIC, pb1.StateType.ENUM,
                                 pb1.StateType.COLOR)

BASE = "javascript.0.virtualDevice"
ROOM_NAMES = {"wohnzimmer": "Wohnzimmer", "buero": "Büro", "schlafzimmer": "Schlafzimmer", "kueche": "Küche"}


def state(device, suffix, value, *, room, dtype, state_type=NUMERIC, writable=True, inverted=None):
    """Ein State eines v1-Adapters. Die Geräte-ID enthält den Raum (wie im echten
    virtualDevice-Baum), sonst überschreiben sich gleichnamige Geräte in verschiedenen Räumen."""
    message = pb1.AgentDevice(
        state_id=f"{BASE}.{room}.{device}.{suffix}", device_id=f"{BASE}.{room}.{device}",
        canonical_key=suffix, room=room, device=device, device_type=dtype,
        value=pb1.AgentStateValue(value=value, ack=True), room_names={"de": ROOM_NAMES[room]},
        state_type=state_type, writable=writable, floor="EG",
    )
    if inverted is not None:
        message.inverted = inverted
    return message


def home() -> list:
    """Die Wohnung als v1-Snapshot. Muster aus dem Live-Core (2026-09-30): Lichter mit Kelvin
    unter `color` (Adapter-Bug #210), ein Licht mit schreibgeschütztem Level, Steckdosen mit
    Leistung, Raumthermostat (`current`+`expected`) als `temperature_sensor`, Sensor ohne
    Sollwert, Luftqualität, Szene, Fenster/Türen, dazu Rollläden (auch invertiert) und eine
    Klimaanlage, die die echten Daten nicht enthalten."""
    s = []
    # Wohnzimmer
    s += [state("Deckenlampe", "color", "2700", room="wohnzimmer", dtype="light", state_type=COLOR),
          state("Deckenlampe", "level", "100", room="wohnzimmer", dtype="light"),
          state("Deckenlampe", "on", "false", room="wohnzimmer", dtype="light", state_type=BOOLEAN)]
    s += [state("Stehlampe", "on", "true", room="wohnzimmer", dtype="light", state_type=BOOLEAN)]
    s += [state("Sofalicht", "level", "40", room="wohnzimmer", dtype="light"),
          state("Sofalicht", "on", "true", room="wohnzimmer", dtype="light", state_type=BOOLEAN)]
    s += [state("Fernseher", "power", "5", room="wohnzimmer", dtype="socket", writable=False),
          state("Fernseher", "on", "true", room="wohnzimmer", dtype="socket", state_type=BOOLEAN)]
    s += [state("Heizung", "current", "26.7", room="wohnzimmer", dtype="temperature_sensor", writable=False),
          state("Heizung", "expected", "18", room="wohnzimmer", dtype="temperature_sensor")]
    s += [state("Rollladen", "level", "100", room="wohnzimmer", dtype="blind")]
    s += [state("Fenster", "open", "false", room="wohnzimmer", dtype="window", state_type=BOOLEAN, writable=False)]
    s += [state("Terrassentuer", "open", "true", room="wohnzimmer", dtype="door", state_type=BOOLEAN, writable=False)]
    # Büro
    s += [state("Raumtemperatur", "current", "25.33", room="buero", dtype="temperature_sensor", writable=False)]
    s += [state("Schreibtischlampe", "color", '"#0096ff"', room="buero", dtype="light", state_type=COLOR),
          state("Schreibtischlampe", "level", "24", room="buero", dtype="light", writable=False),
          state("Schreibtischlampe", "on", "true", room="buero", dtype="light", state_type=BOOLEAN)]
    s += [state("Luftqualitaet", "iaq", "92.3", room="buero", dtype="air_quality_sensor", writable=False),
          state("Luftqualitaet", "co2_equiv", "911", room="buero", dtype="air_quality_sensor", writable=False),
          state("Luftqualitaet", "voc_equiv", "1.27", room="buero", dtype="air_quality_sensor", writable=False)]
    s += [state("Luftfeuchte", "current", "54.3", room="buero", dtype="humidity_sensor", writable=False)]
    s += [state("Fenster", "open", "true", room="buero", dtype="window", state_type=BOOLEAN, writable=False)]
    # Schlafzimmer
    s += [state("Nachtlicht", "on", "false", room="schlafzimmer", dtype="light", state_type=BOOLEAN)]
    s += [state("Bettlampe", "on", "false", room="schlafzimmer", dtype="light", state_type=BOOLEAN)]
    s += [state("Filmabend", "on", "false", room="schlafzimmer", dtype="scene", state_type=BOOLEAN)]
    s += [state("Rollladen", "level", "0", room="schlafzimmer", dtype="blind")]
    s += [state("Klimaanlage", "on", "true", room="schlafzimmer", dtype="climate", state_type=BOOLEAN),
          state("Klimaanlage", "mode", "cool", room="schlafzimmer", dtype="climate", state_type=ENUM),
          state("Klimaanlage", "current", "24.5", room="schlafzimmer", dtype="climate", writable=False),
          state("Klimaanlage", "expected", "22", room="schlafzimmer", dtype="climate"),
          state("Klimaanlage", "fanSpeed", "low", room="schlafzimmer", dtype="climate", state_type=ENUM)]
    s += [state("Fenster", "open", "false", room="schlafzimmer", dtype="window", state_type=BOOLEAN, writable=False)]
    # Küche
    s += [state("Deckenlicht", "on", "false", room="kueche", dtype="light", state_type=BOOLEAN),
          state("Deckenlicht", "level", "0", room="kueche", dtype="light")]
    s += [state("Markise", "level", "30", room="kueche", dtype="blind", inverted=True)]
    s += [state("Kaffeemaschine", "on", "false", room="kueche", dtype="socket", state_type=BOOLEAN),
          state("Kaffeemaschine", "power", "0", room="kueche", dtype="socket", writable=False)]
    return s


@pytest.fixture
def world():
    """(client, nlu, sent): `sent` sammelt die gesetzten States als (Pfad ab virtualDevice, Wert)."""
    client = IoBrokerClient({})
    client.handle_device_snapshot(home())
    # Die NLU sucht seit #387 (Schritt 1) in der typisierten Registry, execute/answer_query
    # lesen bis Schritt 3/4 noch den alten Baum
    registry = DeviceRegistry()
    legacy_devices.load_snapshot(registry, home())
    nlu = NLU(DEFAULT_NLU_SETTINGS, client.rooms, nlu_devices.build_index(registry))
    client.set_answerer(device_answers.DeviceAnswers(registry, lambda: client.rooms).answer)
    sent = []
    client.set_setter(lambda sid, payload: sent.append((sid.replace(BASE + ".", ""), json.loads(payload))) or True)
    # Geschrieben wird seit #387 (Schritt 4) über die Registry und den DeviceController
    controller = DeviceController(
        registry, send_set_slot=lambda *_: False, send_set_state=lambda sid, value: client.set_state(sid, value),
    )
    client.set_slot_control(registry, controller, lambda: client.rooms)
    return client, nlu, sent


def ident(intent):
    return intent.name, intent.room_id, intent.device, intent.category_filter


# ------------------------------------------------------------------
# Steuern: Satz → Intent → gesetzte States

CONTROL = [
    # Ein/Aus
    ("mach das licht im wohnzimmer an", ("TurnOn", "wohnzimmer", None, "light"),
     [("wohnzimmer.Deckenlampe.on", True), ("wohnzimmer.Sofalicht.on", True), ("wohnzimmer.Stehlampe.on", True)]),
    ("wohnzimmer licht aus", ("TurnOff", "wohnzimmer", None, "light"),
     [("wohnzimmer.Deckenlampe.on", False), ("wohnzimmer.Sofalicht.on", False), ("wohnzimmer.Stehlampe.on", False)]),
    ("mach alle lichter im wohnzimmer aus", ("TurnOff", "wohnzimmer", None, "light"),
     [("wohnzimmer.Deckenlampe.on", False), ("wohnzimmer.Sofalicht.on", False), ("wohnzimmer.Stehlampe.on", False)]),
    ("schalte die deckenlampe im wohnzimmer aus", ("TurnOff", "wohnzimmer", "Deckenlampe", None),
     [("wohnzimmer.Deckenlampe.on", False)]),
    ("mach die nachtlicht im schlafzimmer an", ("TurnOn", "schlafzimmer", "Nachtlicht", None),
     [("schlafzimmer.Nachtlicht.on", True)]),
    ("schalte den fernseher im wohnzimmer aus", ("TurnOff", "wohnzimmer", "Fernseher", None),
     [("wohnzimmer.Fernseher.on", False)]),
    ("schalte die kaffeemaschine in der kueche an", ("TurnOn", "kueche", "Kaffeemaschine", None),
     [("kueche.Kaffeemaschine.on", True)]),
    ("starte den filmabend im schlafzimmer", ("TurnOn", "schlafzimmer", "Filmabend", None),
     [("schlafzimmer.Filmabend.on", True)]),
    ("schalte die klimaanlage im schlafzimmer aus", ("TurnOff", "schlafzimmer", "Klimaanlage", None),
     [("schlafzimmer.Klimaanlage.on", False)]),
    # Gerät ohne Raum im Satz: der Name ist eindeutig, der Raum kommt vom Gerät
    ("deckenlicht an", ("TurnOn", None, "Deckenlicht", None), [("kueche.Deckenlicht.on", True)]),
    # Helligkeit und Farbe
    ("dimme die deckenlampe im wohnzimmer auf 30 prozent", ("SetLevel", "wohnzimmer", "Deckenlampe", None),
     [("wohnzimmer.Deckenlampe.level", 30.0)]),
    ("mach die deckenlampe im wohnzimmer rot", ("SetColor", "wohnzimmer", "Deckenlampe", None),
     [("wohnzimmer.Deckenlampe.color", "#FF0000")]),
    ("mach die schreibtischlampe im buero blau", ("SetColor", "buero", "Schreibtischlampe", None),
     [("buero.Schreibtischlampe.color", "#0000FF")]),
    # Rollläden: "hoch"/"runter" sind öffnen/schließen, Prozent bleibt Prozent
    ("rollladen im wohnzimmer hoch", ("SetLevel", "wohnzimmer", "Rollladen", "blind"),
     [("wohnzimmer.Rollladen.level", 100)]),
    ("rollladen im wohnzimmer runter", ("SetLevel", "wohnzimmer", "Rollladen", "blind"),
     [("wohnzimmer.Rollladen.level", 0)]),
    ("oeffne den rollladen im schlafzimmer", ("SetLevel", "schlafzimmer", "Rollladen", "blind"),
     [("schlafzimmer.Rollladen.level", 100)]),
    ("schliesse alle rolllaeden im schlafzimmer", ("SetLevel", "schlafzimmer", None, "blind"),
     [("schlafzimmer.Rollladen.level", 0)]),
    ("rollladen im wohnzimmer auf 50 prozent", ("SetLevel", "wohnzimmer", "Rollladen", "blind"),
     [("wohnzimmer.Rollladen.level", 50.0)]),
    # invertierter Aktor (#270): nur öffnen/schließen wird umgerechnet, ein Prozentwert nie
    ("oeffne die markise in der kueche", ("SetLevel", "kueche", "Markise", "blind"),
     [("kueche.Markise.level", 0)]),
    ("schliesse die markise in der kueche", ("SetLevel", "kueche", "Markise", "blind"),
     [("kueche.Markise.level", 100)]),
    ("markise in der kueche auf 50 prozent", ("SetLevel", "kueche", "Markise", None),
     [("kueche.Markise.level", 50.0)]),
    # Klima
    ("stelle die heizung im wohnzimmer auf 21 grad", ("SetTemperature", "wohnzimmer", "Heizung", "thermostat"),
     [("wohnzimmer.Heizung.expected", 21.0)]),
    ("klimaanlage im schlafzimmer auf 24 grad", ("SetTemperature", "schlafzimmer", "Klimaanlage", None),
     [("schlafzimmer.Klimaanlage.expected", 24.0)]),
    ("kuehlen im schlafzimmer", ("SetMode", "schlafzimmer", None, "climate"),
     [("schlafzimmer.Klimaanlage.mode", "cool")]),
    ("klimaanlage im schlafzimmer auf heizen", ("SetMode", "schlafzimmer", "Klimaanlage", "climate"),
     [("schlafzimmer.Klimaanlage.mode", "heat")]),
    ("klimaanlage im schlafzimmer leise", ("SetFanSpeed", "schlafzimmer", "Klimaanlage", "climate"),
     [("schlafzimmer.Klimaanlage.fanSpeed", "low")]),
    # Dem Gerät fehlt der Slot oder es kann ihn nur lesen: es wird nichts gesetzt, die Antwort
    # dazu ist ehrlich (siehe UNSUPPORTED unten, Schritt 2 von #387)
    ("stelle die stehlampe im wohnzimmer auf 30 prozent", ("SetLevel", "wohnzimmer", "Stehlampe", None), []),
    ("dimme die nachtlicht im schlafzimmer auf 20 prozent", ("SetLevel", "schlafzimmer", "Nachtlicht", None), []),
    ("mach die stehlampe im wohnzimmer rot", ("SetColor", "wohnzimmer", "Stehlampe", None), []),
    # Früher wurde ein schreibgeschützter Helligkeits-Slot trotzdem beschrieben (der alte Pfad
    # prüfte `writable` nicht), jetzt ist die Lampe nicht dimmbar.
    ("dimme die schreibtischlampe im buero auf 50 prozent", ("SetLevel", "buero", "Schreibtischlampe", None), []),
]


@pytest.mark.parametrize("text,expected_intent,expected_writes", CONTROL, ids=[c[0] for c in CONTROL])
def test_control_sentences(world, text, expected_intent, expected_writes):
    client, nlu, sent = world
    intent = nlu.parse(text)

    assert ident(intent) == expected_intent

    count = client.execute(intent, trust_level=None)

    assert sorted(sent) == sorted(expected_writes)
    assert count == len(expected_writes)


# Ehrliche Antworten (Schritt 2 von #387): ohne die gefragte Fähigkeit wird nichts gesetzt und
# `execute` liefert den Satz dazu, den main.py statt "Keine Geräte gefunden." sagt.
UNSUPPORTED = [
    ("stelle die stehlampe im wohnzimmer auf 30 prozent", "Stehlampe im Wohnzimmer lässt sich nicht dimmen."),
    ("dimme die nachtlicht im schlafzimmer auf 20 prozent", "Nachtlicht im Schlafzimmer lässt sich nicht dimmen."),
    ("mach die stehlampe im wohnzimmer rot", "Stehlampe im Wohnzimmer lässt sich nicht umfärben."),
    ("stelle die stehlampe im wohnzimmer auf 21 grad", "Stehlampe im Wohnzimmer hat keine einstellbare Solltemperatur."),
    # nur lesbarer Helligkeits-State
    ("dimme die schreibtischlampe im buero auf 50 prozent", "Schreibtischlampe im Büro lässt sich nicht dimmen."),
]


@pytest.mark.parametrize("text,expected_sentence", UNSUPPORTED, ids=[u[0] for u in UNSUPPORTED])
def test_a_missing_capability_is_answered_honestly(world, text, expected_sentence):
    client, nlu, sent = world
    unsupported = []

    count = client.execute(nlu.parse(text), trust_level=None, unsupported=unsupported)

    assert (count, sent) == (0, [])
    assert unsupported == [expected_sentence]


def test_a_bulk_command_with_one_capable_device_still_sets_it(world):
    """Nur wenn gar nichts gesetzt wird, zählt der Hinweis: im Wohnzimmer können zwei von drei
    Lichtern dimmen, das dritte (Stehlampe) meldet sich im Hinweis, die anderen werden gesetzt."""
    client, nlu, sent = world
    unsupported = []

    count = client.execute(nlu.parse("wohnzimmer licht auf 30 prozent"), trust_level=None, unsupported=unsupported)

    assert count == 2
    assert sorted(sent) == [("wohnzimmer.Deckenlampe.level", 30.0), ("wohnzimmer.Sofalicht.level", 30.0)]
    assert unsupported == ["Stehlampe im Wohnzimmer lässt sich nicht dimmen."]


# ------------------------------------------------------------------
# Abfragen: Satz → Intent → Antworttext

QUERY = [
    # Sensoren nach Kategorie
    ("wie warm ist es im buero", ("Query", "buero", None, "temperature_sensor"),
     "Raumtemperatur im Büro: 25.3 Grad."),
    # Das Raumthermostat (Ist und Soll) nennt auch den Sollwert. Früher fehlte er, weil das Gerät als
    # reiner Temperatursensor geführt wurde (Schritt 3 von #387).
    ("wie warm ist es im wohnzimmer", ("Query", "wohnzimmer", None, "temperature_sensor"),
     "Heizung im Wohnzimmer: 26.7 Grad, Soll 18 Grad."),
    ("welche temperatur hat die heizung im wohnzimmer", ("Query", "wohnzimmer", "Heizung", "temperature_sensor"),
     "Heizung im Wohnzimmer: 26.7 Grad, Soll 18 Grad."),
    # Die Klimaanlage hat Ist- und Solltemperatur und antwortet jetzt auf die Frage danach; früher
    # zählte sie nicht als Temperatursensor ("Ich kenne keine Temperatursensoren im Schlafzimmer.")
    ("wie ist die temperatur im schlafzimmer", ("Query", "schlafzimmer", None, "temperature_sensor"),
     "Klimaanlage im Schlafzimmer: 24.5 Grad, Soll 22 Grad."),
    ("wie warm ist es in der kueche", ("Query", "kueche", None, "temperature_sensor"),
     "Ich kenne keine Temperatursensoren im Küche."),
    ("wie ist die luftqualitaet im buero", ("Query", "buero", "Luftqualitaet", "air_quality_sensor"),
     "Luftqualitaet im Büro: okay, 911 ppm CO₂, 1.3 ppm VOC."),
    ("wie hoch ist die luftfeuchtigkeit im buero", ("Query", "buero", None, "humidity_sensor"),
     "Luftfeuchte im Büro: 54.3 %."),
    # Fenster und Türen
    ("ist das fenster im buero offen", ("Query", "buero", "Fenster", "window"), "Fenster im Büro: offen."),
    ("ist das fenster im wohnzimmer offen", ("Query", "wohnzimmer", "Fenster", "window"),
     "Fenster im Wohnzimmer: geschlossen."),
    ("sind fenster im wohnzimmer offen", ("Query", "wohnzimmer", "Fenster", "window"),
     "Fenster im Wohnzimmer: geschlossen."),
    ("ist die terrassentuer offen", ("Query", None, "Terrassentuer", None), "Terrassentuer im Wohnzimmer: offen."),
    ("ist die tuer offen", ("Query", None, None, "door"), "Terrassentuer im Wohnzimmer: offen."),
    # Rolladen: der Prozentwert (kanonisch, also bei der invertierten Markise umgerechnet)
    ("ist der rollladen im wohnzimmer offen", ("Query", "wohnzimmer", "Rollladen", "blind"),
     "Rollladen im Wohnzimmer: 100 %."),
    ("wie weit ist die markise in der kueche offen", ("Query", "kueche", "Markise", None),
     "Markise im Küche: 70 %."),
    # Lichter
    ("wie hell ist die deckenlampe im wohnzimmer", ("Query", "wohnzimmer", "Deckenlampe", None),
     "Deckenlampe im Wohnzimmer ist auf 100 Prozent."),
    ("wie hell ist die schreibtischlampe im buero", ("Query", "buero", "Schreibtischlampe", None),
     "Schreibtischlampe im Büro ist auf 24 Prozent."),
    # Die Farbe kommt als Farbwert (Hex). Die Deckenlampe ist der bekannte Adapter-Fall #210: der
    # v1-Adapter legt die Kelvinzahl unter `color`, 2700 erscheint deshalb als Farbwert. Das wird im
    # Adapter behoben, Core rät nicht. Früher kam hier "2700" bzw. der Wert samt Anführungszeichen.
    ("welche farbe hat die deckenlampe im wohnzimmer", ("Query", "wohnzimmer", "Deckenlampe", None),
     "Deckenlampe im Wohnzimmer leuchtet in #000A8C."),
    ("welche farbe hat die schreibtischlampe im buero", ("Query", "buero", "Schreibtischlampe", None),
     "Schreibtischlampe im Büro leuchtet in #0096FF."),
    ("ist die stehlampe im wohnzimmer an", ("Query", "wohnzimmer", "Stehlampe", None),
     "Stehlampe im Wohnzimmer ist an."),
    ("ist das licht im wohnzimmer an", ("Query", "wohnzimmer", None, "light"),
     "Im Wohnzimmer: Stehlampe, Sofalicht sind an, Deckenlampe ist aus."),
    ("welche lichter sind an", ("Query", None, None, "light"), "Eingeschaltete Lichter in: Büro, Wohnzimmer."),
    ("wo ist licht an", ("Query", None, None, "light"), "Eingeschaltete Lichter in: Büro, Wohnzimmer."),
    # Steckdosen
    ("wie viel strom braucht der fernseher", ("Query", None, "Fernseher", "socket"), "Fernseher im Wohnzimmer: 5 Watt."),
    ("wie viel strom braucht die kaffeemaschine", ("Query", None, "Kaffeemaschine", "socket"),
     "Kaffeemaschine im Küche: 0 Watt."),
    ("ist der fernseher im wohnzimmer an", ("Query", "wohnzimmer", "Fernseher", None),
     "Fernseher im Wohnzimmer ist an (5 W)."),
    ("ist das filmabend im schlafzimmer an", ("Query", "schlafzimmer", "Filmabend", None),
     "Filmabend im Schlafzimmer ist aus."),
    # Klimaanlage: Status mit Modus, Ist, Soll und Lüfter (auch auf "ist sie an?")
    ("wie ist der status der klimaanlage im schlafzimmer", ("Query", "schlafzimmer", "Klimaanlage", None),
     "Klimaanlage im Schlafzimmer: an, Modus Kühlen, 24.5°C, Soll 22.0°C, Lüfter niedrig."),
    ("ist die klimaanlage im schlafzimmer an", ("Query", "schlafzimmer", "Klimaanlage", None),
     "Klimaanlage im Schlafzimmer: an, Modus Kühlen, 24.5°C, Soll 22.0°C, Lüfter niedrig."),
]


@pytest.mark.parametrize("text,expected_intent,expected_answer", QUERY, ids=[q[0] for q in QUERY])
def test_query_sentences(world, text, expected_intent, expected_answer):
    client, nlu, _ = world
    intent = nlu.parse(text)

    assert ident(intent) == expected_intent
    assert client.answer_query(intent) == expected_answer


# ------------------------------------------------------------------
# Raumübergreifend, Mehrdeutigkeit und unbekannte Geräte

def test_global_window_query_lists_every_room_sorted_by_room_id(world):
    client, nlu, _ = world
    intent = nlu.parse("sind die fenster offen")

    assert (intent.name, intent.category_filter) == ("Query", "window")
    # Das Wort "fenster" ist der Gerätename in drei Räumen: Rückfrage-Kandidaten, die Antwort
    # deckt trotzdem alle Räume ab.
    assert sorted(intent.candidates) == [("buero", "Büro"), ("schlafzimmer", "Schlafzimmer"), ("wohnzimmer", "Wohnzimmer")]
    assert client.answer_query(intent) == (
        "Fenster im Büro: offen. Fenster im Schlafzimmer: geschlossen. Fenster im Wohnzimmer: geschlossen."
    )


def test_blind_in_two_rooms_asks_which_room_and_sets_nothing(world):
    client, nlu, sent = world
    intent = nlu.parse("rollladen hoch")

    assert (intent.name, intent.category_filter, intent.is_open_close) == ("SetLevel", "blind", True)
    assert sorted(intent.candidates) == [("schlafzimmer", "Schlafzimmer"), ("wohnzimmer", "Wohnzimmer")]
    assert client.execute(intent, trust_level=None) == 0
    assert sent == []


@pytest.mark.parametrize("text", [
    "schalte das radio im wohnzimmer aus",
    "schalte die steckdose im wohnzimmer aus",
    "schalte die deckenlampe im buero an",
])
def test_unknown_device_in_a_known_room_is_reported(world, text):
    _, nlu, _ = world

    assert nlu.parse(text).name == "DeviceNotFound"


@pytest.mark.parametrize("text", ["mach das licht an", "mach alles aus"])
def test_control_without_room_or_device_sets_nothing(world, text):
    client, nlu, sent = world
    intent = nlu.parse(text)

    assert intent.name in ("TurnOn", "TurnOff")
    assert client.execute(intent, trust_level=None) == 0
    assert sent == []
