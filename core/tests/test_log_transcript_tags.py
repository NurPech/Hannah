"""
Wächter für die TRANSCRIPT-Kategorie im Core-Log (#394).

Ein Logexport ohne Transkripte lässt nur Zeilen weg, die mit extra=TRANSCRIPT getaggt sind.
Dieser Test liest den Core-Quelltext und schlägt an, wenn ein Log-Aufruf Nutzertext, eine
Antwort oder Ähnliches einsetzt, ohne getaggt zu sein. Das ist eine Heuristik über
Variablennamen (siehe _SENSITIVE), keine Garantie: Was anders heißt, fängt sie nicht.
"""
import ast
from pathlib import Path

from hannah.log_shipping import _METADATA_LOGGERS

CORE = Path(__file__).resolve().parents[1]
_LEVELS = {"debug", "info", "warning", "error", "exception", "critical"}

# Namen, hinter denen Nutzertext oder Hannahs Antwort steckt.
_SENSITIVE = {"text", "transcript", "answer", "antwort", "question", "content", "summary",
              "raw_text", "raw", "args", "reply", "utterance"}

# Bewusst ungetaggt, Einordnung offen: Texte von Systemen (ioBroker-Notifications,
# Announcements, Trigger-Ansagen), nicht von Nutzern. Wer eine Stelle entscheidet, nimmt sie
# hier heraus und taggt sie bzw. lässt sie stehen.
_UNDECIDED = {
    "main.py": ("Direct-Notification", "System-Notification"),
    "hannah/grpc_server.py": ("Notify empfangen",),
    "hannah/mqtt_handler.py": ("System-Notification", "Raum-Announcement", "Announcement →"),
    "hannah/trigger_engine.py": ("fragt →", "ausgelöst →", "Aktion →"),
}


def _names(node: ast.AST) -> set[str]:
    """Alle Namen und Attribute in einem Ausdruck, ohne len(...): die Länge eines Textes
    ist kein Text."""
    found: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, ast.Call) and isinstance(current.func, ast.Name) and current.func.id == "len":
            continue
        if isinstance(current, ast.Name):
            found.add(current.id)
        elif isinstance(current, ast.Attribute):
            found.add(current.attr)
        stack.extend(ast.iter_child_nodes(current))
    return found


def _untagged_log_calls():
    files = [CORE / "main.py", *sorted((CORE / "hannah").rglob("*.py"))]
    for path in files:
        relative = path.relative_to(CORE).as_posix()
        if relative.removesuffix(".py").replace("/", ".") in _METADATA_LOGGERS:
            continue  # diese Module taggt der Logger-Name als METADATA, nicht der Aufruf
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _LEVELS
                    and isinstance(node.func.value, ast.Name) and node.func.value.id in ("log", "logger")):
                continue
            if any(keyword.arg == "extra" for keyword in node.keywords) or not node.args:
                continue
            hits = set().union(*(_names(arg) for arg in node.args)) & _SENSITIVE
            if hits:
                yield relative, node.lineno, sorted(hits), ast.unparse(node.args[0])


def test_log_calls_with_user_text_are_tagged_transcript():
    violations = [
        f"{path}:{line} {names} {message[:80]}"
        for path, line, names, message in _untagged_log_calls()
        if not any(marker in message for marker in _UNDECIDED.get(path, ()))
    ]
    assert not violations, (
        "Log-Aufruf mit Nutzertext/Antwort ohne extra=TRANSCRIPT (from hannah.log_shipping import "
        "TRANSCRIPT), sonst steht er im Export ohne Transkripte:\n" + "\n".join(violations)
    )
