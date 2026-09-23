import re

from hannah.link_tokens import LinkTokenStore, LOOKUP_EXPIRED, LOOKUP_OK, LOOKUP_UNKNOWN


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_issued_token_matches_telegram_start_parameter_rules():
    """Telegram allows max. 64 chars from A-Za-z0-9_- for the start parameter."""
    entry = LinkTokenStore().issue(1, "telegram")
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", entry.token)


def test_lookup_ok_and_expires_after_ttl():
    clock = _Clock()
    store = LinkTokenStore(ttl_seconds=600, clock=clock)
    entry = store.issue(1, "telegram")
    assert entry.expires_at == 1600.0

    status, found = store.lookup(entry.token, "telegram")
    assert status == LOOKUP_OK
    assert found.user_id == 1

    clock.now = 1600.0
    assert store.lookup(entry.token, "telegram") == (LOOKUP_EXPIRED, None)
    # Expired tokens are removed on lookup.
    assert store.lookup(entry.token, "telegram") == (LOOKUP_UNKNOWN, None)


def test_lookup_does_not_consume_but_consume_does():
    store = LinkTokenStore()
    entry = store.issue(1, "telegram")
    store.lookup(entry.token, "telegram")
    assert store.lookup(entry.token, "telegram")[0] == LOOKUP_OK

    store.consume(entry.token)
    assert store.lookup(entry.token, "telegram") == (LOOKUP_UNKNOWN, None)


def test_token_for_other_service_is_unknown():
    store = LinkTokenStore()
    entry = store.issue(1, "telegram")
    assert store.lookup(entry.token, "teams") == (LOOKUP_UNKNOWN, None)
    # ...and stays valid for its own service.
    assert store.lookup(entry.token, "telegram")[0] == LOOKUP_OK


def test_new_token_replaces_old_one_for_same_user_and_service():
    store = LinkTokenStore()
    first = store.issue(1, "telegram")
    second = store.issue(1, "telegram")
    assert first.token != second.token
    assert store.lookup(first.token, "telegram") == (LOOKUP_UNKNOWN, None)
    assert store.lookup(second.token, "telegram")[0] == LOOKUP_OK


def test_tokens_of_different_users_are_independent():
    store = LinkTokenStore()
    a = store.issue(1, "telegram")
    b = store.issue(2, "telegram")
    assert store.lookup(a.token, "telegram")[0] == LOOKUP_OK
    assert store.lookup(b.token, "telegram")[0] == LOOKUP_OK
