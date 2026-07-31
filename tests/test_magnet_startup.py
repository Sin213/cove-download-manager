"""Startup migration and self-heal. Never touches a real registry or MIME db."""
from dataclasses import dataclass

from cove import magnet_startup
from cove.magnet_handler import HandlerStatus


@dataclass
class FakeSettings:
    magnet_handler_enabled: bool = False
    magnet_prompt_shown: bool = False
    magnet_setting_missing: bool = False
    saved: bool = False

    def save(self):
        self.saved = True


def test_absent_setting_with_cove_registration_migrates_and_repairs():
    settings = FakeSettings(magnet_setting_missing=True)
    calls = []
    magnet_startup.migrate_and_repair(
        settings,
        status_fn=lambda: HandlerStatus(
            supported=True, registered=True, owned_by_cove=True, stale=True
        ),
        repair_fn=lambda: calls.append("repair") or True,
    )
    assert settings.magnet_handler_enabled is True
    assert settings.magnet_prompt_shown is True
    assert settings.saved is True
    assert calls == ["repair"]


def test_absent_setting_without_a_registration_does_not_migrate():
    settings = FakeSettings(magnet_setting_missing=True)
    calls = []
    magnet_startup.migrate_and_repair(
        settings,
        status_fn=lambda: HandlerStatus(supported=True, registered=False),
        repair_fn=lambda: calls.append("repair") or True,
    )
    assert settings.magnet_handler_enabled is False
    assert settings.magnet_prompt_shown is False
    assert calls == []


def test_absent_setting_with_a_foreign_registration_does_not_migrate():
    settings = FakeSettings(magnet_setting_missing=True)
    magnet_startup.migrate_and_repair(
        settings,
        status_fn=lambda: HandlerStatus(
            supported=True, registered=True, owned_by_cove=False
        ),
        repair_fn=lambda: True,
    )
    assert settings.magnet_handler_enabled is False


def test_explicit_false_is_respected_and_never_repairs():
    settings = FakeSettings(magnet_handler_enabled=False, magnet_setting_missing=False)
    calls = []
    magnet_startup.migrate_and_repair(
        settings,
        status_fn=lambda: HandlerStatus(
            supported=True, registered=True, owned_by_cove=True, stale=True
        ),
        repair_fn=lambda: calls.append("repair") or True,
    )
    assert settings.magnet_handler_enabled is False
    assert calls == []


def test_enabled_setting_repairs_a_stale_registration():
    settings = FakeSettings(magnet_handler_enabled=True)
    calls = []
    magnet_startup.migrate_and_repair(
        settings,
        status_fn=lambda: HandlerStatus(
            supported=True, registered=True, owned_by_cove=True, stale=True
        ),
        repair_fn=lambda: calls.append("repair") or True,
    )
    assert calls == ["repair"]


def test_a_failing_repair_never_propagates():
    settings = FakeSettings(magnet_handler_enabled=True)

    def boom():
        raise OSError("registry unavailable")

    magnet_startup.migrate_and_repair(
        settings,
        status_fn=lambda: HandlerStatus(
            supported=True, registered=True, owned_by_cove=True, stale=True
        ),
        repair_fn=boom,
    )  # must not raise


def test_a_decline_landing_mid_probe_is_respected():
    """Regression: status_fn() (a slow xdg-mime probe on a real build) can
    take long enough for the user to open Add, paste a magnet, and decline
    the in-app offer while it's running. That offer clears
    magnet_setting_missing directly on the settings object. migrate_and_repair
    must re-check the flag right before acting on it, not trust a value it
    saw before the slow status_fn() call returned - otherwise the decline is
    silently overridden back to enabled.
    """
    settings = FakeSettings(magnet_setting_missing=True)

    def slow_status():
        # Simulate the offer being answered (declined) while this call is
        # still in flight, exactly as it would land mid-probe in the app.
        settings.magnet_prompt_shown = True
        settings.magnet_setting_missing = False
        return HandlerStatus(
            supported=True, registered=True, owned_by_cove=True, stale=True
        )

    calls = []
    magnet_startup.migrate_and_repair(
        settings,
        status_fn=slow_status,
        repair_fn=lambda: calls.append("repair") or True,
    )

    assert settings.magnet_handler_enabled is False
    assert settings.magnet_setting_missing is False


def test_a_failing_status_never_propagates():
    def boom():
        raise OSError("registry unavailable")

    magnet_startup.migrate_and_repair(FakeSettings(), status_fn=boom)
