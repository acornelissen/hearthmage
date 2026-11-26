import hashlib
import json
import logging
import os
import stat

import pytest

from hearthmage.auth import (
    IDLE_SECONDS,
    MAX_WAIT,
    TOUCH_SECONDS,
    LoginThrottle,
    SessionStore,
    describe_device,
    password_fingerprint,
    throttle_key,
)

DAY = 86400.0


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def store(tmp_path, clock) -> SessionStore:
    return SessionStore(str(tmp_path / "sessions.json"), clock=clock)


FP = "fingerprint-a"


def _file(store: SessionStore) -> dict:
    with open(store.path, encoding="utf-8") as handle:
        return json.load(handle)


# ---- sessions --------------------------------------------------------------


def test_create_returns_a_random_token_and_stores_only_its_hash(store):
    token = store.create(FP, "Firefox", "192.168.1.20")
    assert len(token) >= 43  # secrets.token_urlsafe(32)
    raw = open(store.path, encoding="utf-8").read()
    assert token not in raw
    assert hashlib.sha256(token.encode()).hexdigest() in raw
    assert store.lookup(token, FP) is not None


def test_session_file_is_private(store):
    store.create(FP, "Firefox", "192.168.1.20")
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


def test_each_sign_in_gets_its_own_session(store):
    a = store.create(FP, "Firefox", "192.168.1.20")
    b = store.create(FP, "Safari", "192.168.1.21")
    assert a != b
    assert store.lookup(a, FP).address == "192.168.1.20"
    assert store.lookup(b, FP).address == "192.168.1.21"


@pytest.mark.parametrize("token", ["", "nope", "x" * 5000])
def test_unknown_tokens_are_not_sessions(store, token):
    store.create(FP, "Firefox", "192.168.1.20")
    assert store.lookup(token, FP) is None


def test_a_password_change_signs_the_session_out_and_drops_it(store):
    token = store.create(FP, "Firefox", "192.168.1.20")
    assert store.lookup(token, "fingerprint-b") is None
    assert _file(store)["sessions"] == {}
    assert store.lookup(token, FP) is None  # gone, not just hidden


def test_session_expires_after_90_idle_days(store, clock):
    token = store.create(FP, "Firefox", "192.168.1.20")
    clock.now += IDLE_SECONDS + 1
    assert store.lookup(token, FP) is None
    assert _file(store)["sessions"] == {}


def test_use_renews_the_idle_expiry(store, clock):
    token = store.create(FP, "Firefox", "192.168.1.20")
    clock.now += 80 * DAY
    assert store.lookup(token, FP) is not None
    clock.now += 80 * DAY  # 160 days after sign-in, 80 after last use
    assert store.lookup(token, FP) is not None


def test_last_seen_is_written_at_most_every_few_minutes(store, clock):
    token = store.create(FP, "Firefox", "192.168.1.20")
    before = _file(store)
    clock.now += TOUCH_SECONDS - 1
    session = store.lookup(token, FP)
    assert session.renewed is False
    assert _file(store) == before  # no write on every request
    clock.now += 2
    session = store.lookup(token, FP)
    assert session.renewed is True
    assert session.last_seen == clock.now
    assert next(iter(_file(store)["sessions"].values()))["last_seen"] == clock.now


def test_sessions_survive_a_restart(store, clock, tmp_path):
    token = store.create(FP, "Firefox", "192.168.1.20")
    again = SessionStore(store.path, clock=clock)
    assert again.lookup(token, FP) is not None


def test_revoke_deletes_the_server_side_session(store):
    token = store.create(FP, "Firefox", "192.168.1.20")
    store.revoke(token)
    assert store.lookup(token, FP) is None
    assert SessionStore(store.path).lookup(token, FP) is None


def test_revoke_handle_signs_out_one_other_device(store):
    mine = store.create(FP, "Firefox", "192.168.1.20")
    other = store.create(FP, "Safari", "192.168.1.21")
    handle = store.lookup(other, FP).handle
    assert store.revoke_handle(handle, FP) is True
    assert store.lookup(other, FP) is None
    assert store.lookup(mine, FP) is not None
    assert store.revoke_handle("no-such-handle", FP) is False


def test_handles_are_not_the_stored_hash(store):
    token = store.create(FP, "Firefox", "192.168.1.20")
    handle = store.lookup(token, FP).handle
    assert handle not in _file(store)["sessions"]
    assert handle != token


def test_revoke_others_keeps_only_the_current_device(store):
    mine = store.create(FP, "Firefox", "192.168.1.20")
    others = [store.create(FP, "Safari", f"192.168.1.{n}") for n in (21, 22)]
    assert store.revoke_others(mine) == 2
    assert store.lookup(mine, FP) is not None
    assert all(store.lookup(t, FP) is None for t in others)


def test_list_shows_device_metadata_and_prunes_expired(store, clock):
    old = store.create(FP, "Old browser", "192.168.1.9")
    clock.now += 10 * DAY
    store.create(FP, "Mozilla/5.0 " + "x" * 500, "192.168.1.20")
    clock.now += IDLE_SECONDS - 5 * DAY  # the first one is now past 90 idle days
    listed = store.list(FP)
    assert len(listed) == 1
    assert listed[0].address == "192.168.1.20"
    assert len(listed[0].user_agent) == 200
    assert listed[0].created == listed[0].last_seen
    assert len(_file(store)["sessions"]) == 1
    assert store.lookup(old, FP) is None


def test_list_hides_sessions_from_an_old_password(store):
    store.create("old-fp", "Firefox", "192.168.1.20")
    store.create(FP, "Safari", "192.168.1.21")
    assert [s.address for s in store.list(FP)] == ["192.168.1.21"]


def test_session_count_is_bounded(tmp_path, clock):
    store = SessionStore(str(tmp_path / "s.json"), clock=clock, max_sessions=3)
    tokens = []
    for n in range(4):
        tokens.append(store.create(FP, "Firefox", f"192.168.1.{n}"))
        clock.now += 1
    assert store.lookup(tokens[0], FP) is None  # least recently seen goes
    assert all(store.lookup(t, FP) is not None for t in tokens[1:])


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"sessions": []}', '{"sessions": {"abc": "x"}}',
     '{"sessions": {"abc": {"handle": 1}}}'],
)
def test_a_damaged_file_loads_as_no_sessions(tmp_path, content):
    path = tmp_path / "s.json"
    path.write_text(content, encoding="utf-8")
    store = SessionStore(str(path))
    assert store.list(FP) == []
    store.create(FP, "Firefox", "192.168.1.20")  # and still works


# ---- password fingerprint -----------------------------------------------------


def test_password_fingerprint_depends_on_password_and_key():
    a = password_fingerprint("swordfish", "key1")
    assert a == password_fingerprint("swordfish", "key1")
    assert a != password_fingerprint("swordfish2", "key1")
    assert a != password_fingerprint("swordfish", "key2")
    assert "swordfish" not in a
    assert password_fingerprint("päss", "key1")  # non-ASCII works


# ---- device names -------------------------------------------------------------


@pytest.mark.parametrize(
    "agent, label",
    [
        ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:132.0) Gecko/20100101 Firefox/132.0",
         "Firefox on macOS"),
        ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/131.0.0.0 Mobile Safari/537.36", "Chrome on Android"),
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
         "(KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1", "Safari on iPhone"),
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0", "Edge on Windows"),
        ("Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
         "Firefox on Linux"),
        ("curl/8.7.1", "Unknown browser"),
        ("", "Unknown browser"),
    ],
)
def test_describe_device(agent, label):
    assert describe_device(agent) == label


# ---- login throttle -----------------------------------------------------------


@pytest.fixture()
def throttle(clock) -> LoginThrottle:
    return LoginThrottle(clock=clock)


def _fail(throttle, key, times):
    for _ in range(times):
        throttle.failure(key)


def test_five_failures_are_free_then_the_wait_doubles(throttle, clock):
    for _ in range(4):
        assert throttle.failure("10.0.0.1") == 0
    assert throttle.failure("10.0.0.1") == 1  # after 5 failures: wait 1 s
    assert throttle.wait("10.0.0.1") == 1
    clock.now += 1
    assert throttle.wait("10.0.0.1") == 0
    assert throttle.failure("10.0.0.1") == 2
    clock.now += 2
    assert throttle.failure("10.0.0.1") == 4


def test_wait_counts_down(throttle, clock):
    _fail(throttle, "10.0.0.1", 6)  # wait 2 s
    clock.now += 0.5
    assert throttle.wait("10.0.0.1") == pytest.approx(1.5)


def test_wait_is_capped_at_15_minutes(throttle):
    _fail(throttle, "10.0.0.1", 5000)  # no overflow either
    assert throttle.wait("10.0.0.1") == MAX_WAIT == 900


def test_success_clears_that_address_only(throttle):
    _fail(throttle, "10.0.0.1", 7)
    _fail(throttle, "10.0.0.2", 7)
    throttle.success("10.0.0.1")
    assert throttle.wait("10.0.0.1") == 0
    assert throttle.wait("10.0.0.2") > 0


def test_one_address_failing_does_not_slow_another(throttle):
    _fail(throttle, "10.0.0.1", 10)
    assert throttle.wait("10.0.0.2") == 0


def test_failures_across_many_addresses_slow_every_sign_in(throttle, clock, caplog):
    for n in range(20):
        throttle.failure(f"10.0.1.{n}")
    assert throttle.wait("10.0.9.9") == 0  # 20 is not more than 20
    with caplog.at_level(logging.WARNING, logger="hearthmage.security"):
        throttle.failure("10.0.1.20")
    assert throttle.wait("10.0.9.9") == 1  # a brand-new address waits too
    assert any("every sign-in" in r.getMessage() for r in caplog.records)
    clock.now += 1
    throttle.failure("10.0.1.21")
    assert throttle.wait("10.0.9.9") == 2


def test_global_slowdown_ends_after_a_quiet_ten_minutes(throttle, clock):
    for n in range(25):
        clock.now += 20  # spread out, but no 10-minute pause
        throttle.failure(f"10.0.1.{n}")
    assert throttle.wait("10.0.9.9") > 0
    clock.now += 900 + 601
    assert throttle.wait("10.0.9.9") == 0
    throttle.failure("10.0.1.99")
    assert throttle.wait("10.0.9.9") == 0  # counting starts again


def test_an_address_is_forgotten_after_a_quiet_day(throttle, clock):
    _fail(throttle, "10.0.0.1", 8)
    clock.now += 86400 + 900
    assert throttle.failure("10.0.0.1") == 0  # first failure of a new streak


def test_tracked_addresses_are_bounded(clock):
    throttle = LoginThrottle(clock=clock, max_tracked=3)
    for n in range(4):
        _fail(throttle, f"10.0.0.{n}", 5)  # 20 in all: below the global cap
    assert throttle.tracked() == 3
    assert throttle.wait("10.0.0.0") == 0  # the oldest was evicted
    assert throttle.wait("10.0.0.3") > 0


@pytest.mark.parametrize(
    "address, key",
    [
        ("192.168.1.20", "192.168.1.20"),
        ("::ffff:192.168.1.20", "192.168.1.20"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2:3:4:5:6"),
        ("2001:db8:1:2:ffff::1", "2001:db8:1:2:ffff::1"),  # same /64, own key
        ("2A02:1810:4D00:AA00::17", "2a02:1810:4d00:aa00::17"),
        ("fd12:3456:789a:1::20", "fd12:3456:789a:1::20"),
        ("fe80::1c2b:3cff:fe4d:5e6f", "fe80::1c2b:3cff:fe4d:5e6f"),
        ("testclient", "testclient"),
    ],
)
def test_throttle_key_is_the_full_address(address, key):
    assert throttle_key(address) == key


def test_attacker_rotating_addresses_cannot_lock_out_a_known_owner(clock):
    # The review's simulation: the owner signed in before; an attacker on the
    # LAN tries once a second from ever-new addresses for a day while the owner
    # tries to sign in again (a new browser) once a minute.
    throttle = LoginThrottle(clock=clock)
    owner = "10.0.0.2"
    throttle.success(owner)
    owner_ok = owner_tries = 0
    for second in range(1, 86400):
        clock.now += 1
        attacker = f"10.0.{(second // 250) % 250}.{second % 250}"
        if throttle.wait(attacker) == 0:
            throttle.failure(attacker)
        if second % 60 == 0:
            owner_tries += 1
            if throttle.wait(owner) == 0:
                owner_ok += 1
                throttle.success(owner)
    assert owner_tries > 1400
    assert owner_ok == owner_tries
    assert throttle.wait("10.0.250.250") > 0  # a stranger still meets the global cap


def test_a_known_address_still_has_its_own_throttle(throttle):
    throttle.success("10.0.0.2")
    _fail(throttle, "10.0.0.2", 6)
    assert throttle.wait("10.0.0.2") == 2


def test_known_addresses_expire_after_90_days(throttle, clock):
    throttle.success("10.0.0.2")
    for n in range(25):
        throttle.failure(f"10.0.1.{n}")
    assert throttle.wait("10.0.0.2") == 0
    clock.now += IDLE_SECONDS + 1
    for n in range(25):
        throttle.failure(f"10.0.2.{n}")
    assert throttle.wait("10.0.0.2") > 0


def test_known_addresses_are_bounded(clock):
    throttle = LoginThrottle(clock=clock, max_known=2)
    for n in range(3):
        throttle.success(f"10.0.0.{n}")
    for n in range(25):
        throttle.failure(f"10.0.1.{n}")
    assert throttle.wait("10.0.0.0") > 0  # evicted
    assert throttle.wait("10.0.0.2") == 0


def test_a_try_inside_the_wait_changes_nothing(throttle, clock):
    _fail(throttle, "10.0.0.1", 6)  # wait 2 s
    for _ in range(100):  # hammering inside the wait
        clock.now += 0.01
        assert throttle.wait("10.0.0.1") > 0
    clock.now += 1.0  # 2 s after the last counted failure
    assert throttle.wait("10.0.0.1") == 0


def test_someone_sharing_the_owners_address_cannot_extend_the_wait(clock):
    # The review's shape: an attacker behind the owner's own address (an
    # unlisted proxy) tries every second for a day. The wait never runs past
    # 15 minutes after the last checked guess, and the guesses stay few.
    throttle = LoginThrottle(clock=clock)
    shared = "10.0.0.2"
    checked = 0
    last_checked = clock.now
    for _ in range(86400):
        clock.now += 1
        assert throttle.wait(shared) <= MAX_WAIT - (clock.now - last_checked) + 1e-9
        if throttle.wait(shared) == 0:
            throttle.failure(shared)
            checked += 1
            last_checked = clock.now
    assert checked < 5 + 15 + 96 + 1  # free tries, ramp, then one per 15 minutes


def test_known_addresses_can_be_seeded(throttle):
    throttle.remember(["10.0.0.2", "10.0.0.3"])
    for n in range(25):
        throttle.failure(f"10.0.1.{n}")
    assert throttle.wait("10.0.0.2") == 0 and throttle.wait("10.0.0.3") == 0
    assert throttle.wait("10.0.0.4") > 0


def test_seeding_is_bounded(clock):
    throttle = LoginThrottle(clock=clock, max_known=2)
    throttle.remember([f"10.0.0.{n}" for n in range(5)])
    for n in range(25):
        throttle.failure(f"10.0.1.{n}")
    assert sum(throttle.wait(f"10.0.0.{n}") == 0 for n in range(5)) == 2


def test_store_lists_the_addresses_of_unexpired_sessions(store, clock):
    store.create(FP, "Firefox", "192.168.1.9")
    clock.now += IDLE_SECONDS - DAY
    store.create("old-password", "Safari", "192.168.1.20")  # still the owner's device
    store.create(FP, "Chrome", "192.168.1.21")
    clock.now += 2 * DAY  # the first one has expired
    assert sorted(store.addresses()) == ["192.168.1.20", "192.168.1.21"]


def test_a_last_seen_far_in_the_future_counts_as_expired(store, clock):
    token = store.create(FP, "Firefox", "192.168.1.20")
    clock.now -= 2 * DAY  # the clock was ahead when the session was made
    assert store.lookup(token, FP) is None


def test_a_last_seen_slightly_ahead_still_works(store, clock):
    token = store.create(FP, "Firefox", "192.168.1.20")
    clock.now -= 3600
    assert store.lookup(token, FP) is not None


def _failing_saves(monkeypatch):
    import hearthmage.auth as auth_mod

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(auth_mod, "write_json_atomic", boom)


def test_a_sign_out_that_fails_to_save_leaves_the_session_as_it_was(store, clock, monkeypatch):
    mine = store.create(FP, "Firefox", "192.168.1.20")
    other = store.create(FP, "Safari", "192.168.1.21")
    handle = store.lookup(other, FP).handle
    _failing_saves(monkeypatch)
    for revoke in (lambda: store.revoke(other), lambda: store.revoke_handle(handle, FP),
                   lambda: store.revoke_others(mine)):
        with pytest.raises(OSError):
            revoke()
        assert store.lookup(other, FP) is not None  # memory agrees with disk
    assert SessionStore(store.path, clock=clock).lookup(other, FP) is not None


def test_a_create_that_fails_to_save_opens_no_session(store, monkeypatch):
    store.create(FP, "Firefox", "192.168.1.20")
    _failing_saves(monkeypatch)
    with pytest.raises(OSError):
        store.create(FP, "Safari", "192.168.1.21")
    assert len(store.list(FP)) == 1


def test_a_renewal_that_fails_to_save_still_lets_the_device_in(store, clock, monkeypatch):
    token = store.create(FP, "Firefox", "192.168.1.20")
    _failing_saves(monkeypatch)
    clock.now += TOUCH_SECONDS + 1
    session = store.lookup(token, FP)
    assert session is not None and session.renewed is False
    assert store.lookup(token, FP).last_seen < clock.now  # memory not ahead of disk
