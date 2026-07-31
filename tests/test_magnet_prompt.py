"""The one-time offer made on the first hand-added magnet or torrent."""
from cove.magnet_handler import HandlerStatus, Result


class FakeSettings:
    def __init__(self, shown=False):
        self.magnet_prompt_shown = shown
        self.magnet_handler_enabled = False
        self.saved = False

    def save(self):
        self.saved = True


def _offer(monkeypatch, settings, state, answer):
    """Drive MainWindow._maybe_offer_magnet_handler with everything stubbed."""
    from cove import main_window as mw

    monkeypatch.setattr(mw.magnet_handler, "status", lambda: state)
    monkeypatch.setattr(mw.magnet_handler, "enable", lambda: Result(True, "ok"))
    monkeypatch.setattr(mw, "_ask_magnet_offer", lambda parent: answer)
    return mw.MainWindow._maybe_offer_magnet_handler(
        type("Stub", (), {"settings": settings})()
    )


def test_offer_is_made_once_and_records_that_it_was(monkeypatch):
    settings = FakeSettings()
    state = HandlerStatus(supported=True, registered=False, is_default=False)
    assert _offer(monkeypatch, settings, state, True) is True
    assert settings.magnet_prompt_shown is True
    assert settings.saved is True


def test_declining_still_records_that_the_offer_was_made(monkeypatch):
    settings = FakeSettings()
    state = HandlerStatus(supported=True, registered=False, is_default=False)
    assert _offer(monkeypatch, settings, state, False) is True
    assert settings.magnet_prompt_shown is True


def test_offer_never_repeats(monkeypatch):
    settings = FakeSettings(shown=True)
    state = HandlerStatus(supported=True, registered=False, is_default=False)
    assert _offer(monkeypatch, settings, state, True) is False


def test_no_offer_when_cove_is_already_the_default(monkeypatch):
    settings = FakeSettings()
    state = HandlerStatus(supported=True, registered=True, is_default=True)
    assert _offer(monkeypatch, settings, state, True) is False
    assert settings.magnet_prompt_shown is False


def test_no_offer_on_an_unsupported_build(monkeypatch):
    settings = FakeSettings()
    assert _offer(monkeypatch, settings, HandlerStatus(supported=False), True) is False
