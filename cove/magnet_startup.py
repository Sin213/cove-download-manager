"""One-time migration plus the self-heal run, both done at startup.

Runs off the GUI thread. Nothing here may raise into the caller: a magnet
association is never worth blocking the window or failing a launch.
"""
from . import magnet_handler


def migrate_and_repair(settings, status_fn=None, repair_fn=None) -> None:
    status_fn = status_fn or magnet_handler.status
    repair_fn = repair_fn or magnet_handler.repair

    try:
        state = status_fn()
    except Exception:
        return

    # Migration: users who registered through the installer or the CLI flag
    # have no setting at all. Reading that absence as False would strand them
    # without self-heal despite having opted in. An explicit False is a real
    # decision and is never overridden.
    if getattr(settings, "magnet_setting_missing", False):
        if state.supported and state.registered and state.owned_by_cove:
            settings.magnet_handler_enabled = True
            settings.magnet_prompt_shown = True
            try:
                settings.save()
            except Exception:
                return
        settings.magnet_setting_missing = False

    if not getattr(settings, "magnet_handler_enabled", False):
        return
    if not (state.supported and state.registered and state.owned_by_cove):
        return
    if not state.stale:
        return
    try:
        repair_fn()
    except Exception:
        return
