"""Response variance for device-control replies (#374).

Occasionally swaps a plain, deterministic answer (e.g. "OK.") for a
personalized variant, so control-command replies don't sound identical
every time. Each category keeps its existing default text untouched —
callers pass it in, this module only decides whether to replace it.
"""

import random

_VARIANCE_PROBABILITY = 0.10

_SUCCESS_VARIANTS = [
    "Sehr gerne, {name}.",
    "Mach ich, {name}.",
    "Klar doch, {name}.",
    "Für dich immer, {name}.",
]

_DENIED_VARIANTS = [
    "Tut mir leid, {name}, das darf ich für dich nicht tun.",
    "Das ist mir für dich leider nicht erlaubt, {name}.",
]

_OFFLINE_VARIANTS = [
    "Tut mir leid, das Gerät ist gerade offline.",
    "Das Gerät antwortet gerade nicht.",
]


def _pick(default: str, variants: list[str], name: str) -> str:
    if random.random() >= _VARIANCE_PROBABILITY:
        return default
    variant = random.choice(variants)
    return variant.format(name=name) if name else variant


def success(default: str, name: str = "") -> str:
    """Device control succeeded. Without a resolved speaker name, always keeps `default`."""
    if not name:
        return default
    return _pick(default, _SUCCESS_VARIANTS, name)


def denied(default: str, name: str = "") -> str:
    """Requester's trust level was too low for every targeted device."""
    if not name:
        return default
    return _pick(default, _DENIED_VARIANTS, name)


def offline(default: str) -> str:
    """A targeted device didn't confirm within the timeout. Not personalized —
    it's about the device, not the relationship to the speaker."""
    return _pick(default, _OFFLINE_VARIANTS, "")
