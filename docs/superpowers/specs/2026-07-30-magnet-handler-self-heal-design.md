# Magnet handler: in-app registration and self-heal

Date: 2026-07-30
Status: approved, not yet implemented

## Problem

Cove's magnet-handler registration records the absolute path of the running
executable. Two of the four distributions carry their version in that path, so
updating breaks the association silently:

- Windows portable: `Cove-Download-Manager-3.2.0-Portable.exe` becomes
  `...-3.2.1-Portable.exe`. The HKCU command still points at the old file.
- AppImage: the desktop entry's `Exec=` records the old AppImage path.

Registration is also opt-in through a terminal command today
(`--register-magnet-handler`, documented in `README.md:195-207`). Users should
not have to open a terminal or run a helper script, before or after an update.

Installed builds (Windows Setup, Debian) have stable paths and are less
affected, but share the same code path and benefit from the same repair.

## Constraints

**Windows cannot self-assign the default.** Since Windows 10 the `UserChoice`
association is hash-protected; only the user can set it, through Settings. Cove
can register itself as capable, read whether it currently holds the default, and
deep-link to the Default Apps page. The final click is always the user's.

**Linux can.** `xdg-mime default` sets the handler outright.

This asymmetry is intrinsic and is reflected in the UI wording per platform
rather than hidden behind one label that means different things.

## Decisions

1. A stale registration is repaired silently, without prompting, but only when
   the existing registration is owned by this Cove identity. The user already
   opted in; an update is not a new decision. Silently seizing an association
   Cove was never granted remains out of bounds.
2. Initial opt-in is offered contextually: once, the first time the user adds a
   magnet or torrent by hand. A first-run prompt arrives before the user knows
   what Cove does and competes with the existing auto-update prompt. A Settings
   toggle is the permanent, discoverable control and the backstop for users who
   only ever click magnet links in the browser.
3. The toggle does the most each platform allows: sets the default on Linux,
   registers and opens Default Apps on Windows. The toggle is an explicit opt-in
   action, so finishing the job on Linux is what the user asked for. The
   README's non-invasiveness promise is about installation, which stays true:
   nothing changes unless the box is ticked.

Rejected: taking the Linux default only when no other handler holds it. The same
tick would produce different results on different machines for invisible
reasons, and it refuses exactly the case where the user most wants to switch.

## Architecture

New module `cove/magnet_handler.py`, one platform-agnostic surface:

    status()            -> registered, is_default, owned_by_cove, stale, identity
    enable(set_default) -> register; on Linux also set the default
    disable()           -> remove only Cove's own registration
    repair()            -> re-point a stale, Cove-owned registration

Windows registry primitives move from `packaging/portable_launcher.py` into
`cove/magnet_win.py`. That module imports no Qt and does not import
`cove.entry`, so `portable_launcher` can keep importing it before the GUI stack
exists, preserving the property that a registration run never starts the GUI.
This removes the current duplication instead of adding a third copy. The
existing `--register-magnet-handler` and `--unregister-magnet-handler` flags
keep working and delegate to the same code.

Linux support lives in `cove/magnet_linux.py`: desktop-entry authoring, desktop
database refresh, and the `xdg-mime` calls.

### Identity

Portable and installed copies already use distinct ProgIDs
(`Cove.Magnet.Portable` vs `Cove.Magnet`) so neither can unregister the other.
Linux gets the same separation through distinct desktop-entry filenames.
`repair()` rewrites a registration only when its recorded command resolves to
this identity, reusing the ownership check at
`packaging/portable_launcher.py:139-145`.

### Per-platform behavior

| Step | Windows | Linux |
|------|---------|-------|
| Enable | Write HKCU capability keys, then open `ms-settings:defaultapps` | Install `~/.local/share/applications/cove-download-manager.desktop` with `Exec` set to the live `$APPIMAGE`, refresh the desktop database, then `xdg-mime default` |
| Reported result | "Registered. Pick Cove in the window that opened." | "Cove is now your magnet handler." |
| Detect default | Read `UserChoice\ProgId` | `xdg-mime query default x-scheme-handler/magnet` |

The Debian package installs its desktop entry system-wide already, so enabling
there is only the `xdg-mime` call.

### Self-heal

On startup, and only when the setting is enabled, compare the recorded
registration path against the running executable. If they differ and the
registration is Cove-owned, rewrite it. Both the portable executable path and
`$APPIMAGE` are read live, which is what makes this survive an update.

Runs off the UI thread with a short timeout. Failure is silent and never fatal:
a broken magnet association must not prevent Cove from starting or block the
window from appearing.

### Configuration

Two additive settings fields, defaulting off, so existing installs are
unaffected until the user acts:

- `magnet_handler_enabled: bool = False`
- `magnet_prompt_shown: bool = False`

### UI

Settings gains a "Magnet links" row: a checkbox plus a status line derived from
`status()`, reflecting real system state rather than the stored preference, so a
default lost outside Cove is visible. Labels differ per platform to match what
each can deliver.

The contextual prompt fires once, on the first manual magnet or torrent add, and
sets `magnet_prompt_shown` so it never returns whichever way it is answered.

## Error handling

Every operation is best-effort and reports a result rather than raising. Missing
`xdg-mime` or `update-desktop-database`, a read-only registry, or an absent
`$APPIMAGE` all degrade to "could not register", surfaced only in the Settings
status line. Nothing here is permitted to interrupt startup or a download.

## Testing

`winreg` and the Linux subprocess runner are injected, mirroring
`tests/test_magnet_registration.py` and
`tests/test_portable_magnet_registration.py`.

Cases:

- a stale Cove-owned path is repaired to the current executable
- a registration owned by another application is left untouched
- portable and installed identities never modify each other
- repair is a no-op when the recorded path is already current
- missing `xdg-mime` or `update-desktop-database` degrades without raising
- enable and disable are idempotent
- disable removes only Cove's own keys and entry
- the contextual prompt fires once and not again after either answer
- self-heal failure does not propagate to startup

No test touches the real registry, the real `~/.local/share/applications`, or
the real MIME database.

## Out of scope

Elevation, `HKLM`, machine-wide changes, and modifying any registration Cove
does not own.

## Known limitation

On Windows, self-heal restores Cove's registration, but if the user's
`UserChoice` default pointed at the removed executable, Windows may drop the
default entirely and only the user can restore it in Settings. The honest
Windows promise is that magnet links keep working when the association survives
the update, not that they can never break. Linux carries no such caveat, since
`xdg-mime default` is reapplied directly.
