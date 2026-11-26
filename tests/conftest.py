import pytest


@pytest.fixture(autouse=True)
def _fresh_auth_state(monkeypatch):
    """The login throttle and the session store are process-wide; give every
    test its own so failed sign-ins in one test cannot slow the next."""
    import hearthmage.app as appmod
    from hearthmage.auth import LoginThrottle

    monkeypatch.setattr(appmod, "_login_throttle", LoginThrottle())
    monkeypatch.setattr(appmod, "_session_store", None)
