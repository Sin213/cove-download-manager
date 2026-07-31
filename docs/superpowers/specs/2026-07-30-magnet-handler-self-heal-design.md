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

4. Repair restores a stale path; it never reclaims a default the user has since
   given to another application. A Cove-owned registration with a stale path is
   repaired. A default that now belongs to another app is reported in Settings
   and reclaimed only by a new explicit click. Otherwise an old preference would
   silently retake the association forever.
5. Existing opt-ins are migrated. Users who already registered through the
   installer or the CLI flag would otherwise load the new version, read a
   missing setting as `False`, and never self-heal despite having opted in. On
   first launch after upgrade: if the setting was absent and
   `status().owned_by_cove` is true, set `magnet_handler_enabled` and
   `magnet_prompt_shown` to true and run `repair()`. This preserves existing
   intent without taking anything from another application.
6. Registration is refused for unpackaged launches. See "Package identity gate".

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
Linux needs the same separation, and the desktop IDs must not collide:

| Build | Desktop ID | Location |
|-------|-----------|----------|
| Debian | `cove-download-manager.desktop` | `/usr/share/applications` (shipped by the package, see `scripts/build-deb.sh:95`) |
| AppImage | `cove-download-manager-appimage.desktop` | `~/.local/share/applications` |

A user-level entry reusing the Debian basename would shadow the packaged one on
a machine that has both. Every `xdg-mime default` call, every status query, and
every ownership comparison uses the ID matching the running build.

`Exec=` is built with Desktop Entry specification escaping, not shell quoting.
Paths containing spaces, `$`, backslashes, or quotes need spec-level escaping,
and a literal `%` must be written `%%`. An AppImage sitting in a directory with
a `%` in its name is otherwise silently unlaunchable.

`repair()` rewrites a registration only when its recorded command resolves to
this identity, reusing the ownership check at
`packaging/portable_launcher.py:139-145`.

### Package identity gate

Registration is refused unless the running process is a supported packaged
build: Windows Setup, Windows portable, Debian-installed, or an AppImage with a
valid `$APPIMAGE`. A source or development launch (`python -m cove`, a
virtualenv interpreter, an extracted AppDir mount path, an unrecognised
executable) never registers anything, because the path it would record is
temporary or belongs to the interpreter rather than to Cove. The Settings row
shows why it is unavailable rather than silently doing nothing.

### Per-platform behavior

| Step | Windows | Linux |
|------|---------|-------|
| Enable | Write HKCU capability keys, then open Default Apps deep-linked to Cove | Install the build's desktop entry with `Exec` set to the live `$APPIMAGE`, refresh the desktop database, then `xdg-mime default` |
| Verify | Read `UserChoice\ProgId` | Re-query `xdg-mime query default` and compare the exact desktop ID |
| Reported result | "Registered. Choose Cove in the window that opened." | Only "Cove is now your magnet handler" when the query confirms it, otherwise "Cove was registered, but your desktop did not make it the default." |

The Debian package installs its desktop entry system-wide already, so enabling
there is only the `xdg-mime` call.

Windows opens
`ms-settings:defaultapps?registeredAppUser=<escaped registered application name>`,
the documented per-user deep link for entries under
`HKCU\Software\RegisteredApplications`, using the exact registered name for the
installed or portable identity. Older Windows versions that do not honour the
parameter fall back to the plain `ms-settings:defaultapps` page. This turns
"find Cove somewhere in Settings" into a single confirming click. The deep-link
form needs verification on a real Windows 10 and Windows 11 machine during
implementation.

Linux never reports success on the strength of the `xdg-mime default` call
alone: desktop policy can decline it, so the result is always read back before
anything is claimed.

### Self-heal

On startup, and only when the setting is enabled, compare the recorded
registration path against the running executable. If they differ and the
registration is Cove-owned, rewrite it. Both the portable executable path and
`$APPIMAGE` are read live, which is what makes this survive an update.

Repair fixes the recorded path and nothing else. It preserves the desktop ID and
the ProgID, and it never calls `xdg-mime default` or otherwise reasserts the
default. If the user has since chosen another handler, that choice stands and
Settings reports it; reclaiming requires a fresh explicit click.

Runs off the UI thread with a short timeout. Failure is silent and never fatal:
a broken magnet association must not prevent Cove from starting or block the
window from appearing.

### Configuration

Two additive settings fields, defaulting off:

- `magnet_handler_enabled: bool = False`
- `magnet_prompt_shown: bool = False`

`magnet_handler_enabled` means exactly one thing: **keep Cove's registration
repaired after updates.** It is not a claim that Cove is currently the default,
and no UI may present it as one.

Absence of the field is distinguished from an explicit `False`, because the
migration in decision 5 depends on telling "never configured" apart from
"turned off".

### UI

Settings gains a "Magnet links" row built from actions plus a live status line,
not a checkbox. A checkbox reads as "this is on", which on Windows would stay
ticked after the user closed the Settings window without choosing Cove, i.e. it
would state something false.

Windows:

    Magnet links
    Status: Registered, but not currently selected as default
    [Choose Cove as default]  [Remove Cove registration]

Linux:

    Magnet links
    Status: Cove is the current default
    [Make Cove default]  [Remove Cove registration]

Status is always derived from `status()`, so a default lost or changed outside
Cove shows up immediately. A separate, clearly worded control governs
`magnet_handler_enabled` ("Repair Cove's magnet registration after updates").

The contextual prompt fires once, on the first manual magnet or torrent add, and
sets `magnet_prompt_shown` so it never returns whichever way it is answered.

### Unregister

Deleting the registration while the system still points its default at Cove
leaves the user with a broken magnet handler, so "off" is not a blind delete.

Windows, when Cove currently holds the default: do not delete the ProgID. Open
Default Apps, explain that another handler needs to be chosen first, and remove
the registration only once Cove is no longer selected. If the user declines,
leave the registration in place and turn off self-healing instead.

Linux: stop self-healing and leave the desktop entry installed and valid. The
entry is harmless on its own, and removing the entry that currently owns the
default is precisely what breaks magnet links. Restoring a remembered previous
handler was considered and rejected as more state than the benefit warrants.

In both cases the user always has a way to stop Cove repairing itself without
being forced through a state where magnet links do not work at all.

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
- the AppImage and Debian desktop IDs never collide, and each build queries and
  sets only its own ID
- `Exec=` escaping survives paths containing spaces, `$`, quotes, and a literal
  `%`
- repair is a no-op when the recorded path is already current
- repair fixes a stale path without reasserting the default, including when
  another application now holds it
- migration: an absent setting plus a Cove-owned registration enables repair and
  runs it once; an absent setting with no registration, or a foreign
  registration, does not
- an explicit `False` is never mistaken for an absent setting
- unpackaged launches (interpreter path, virtualenv, extracted AppDir) refuse to
  register
- Linux reports success only when the read-back query confirms the desktop ID,
  and reports the honest partial result when it does not
- Windows falls back to the plain Settings page when the deep link is not
  honoured
- unregister while Cove holds the default does not leave a dangling association
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

## What "survives updates" actually means

| Distribution | Result |
|--------------|--------|
| Windows Setup | Yes; the executable path is stable |
| Debian | Yes; the desktop ID and installed path are stable |
| AppImage | Repairs once the updated AppImage has been launched |
| Windows portable | Repairs once the updated executable has been launched |

The last two keep an unavoidable gap: a user who deletes the old file and clicks
a magnet link before ever launching the new one gives Cove no opportunity to run
repair code. Nothing in this design closes that window, and the feature must not
be described as if it does.

Closing it entirely requires registering a **stable indirection** rather than
the versioned application file, for example a small launcher at
`%LOCALAPPDATA%\Cove\CoveMagnetLauncher.exe` or `~/.local/bin/`, whose target
the updater rewrites while the registered path never changes. That is a
different and larger feature, and for the portable build it undercuts the point
of being portable by leaving a file outside the portable folder.

The cheaper alternative for the portable build is to ship it under a stable
filename and carry the version in file metadata and the release archive name
only. That is a release-artifact naming change, out of scope here, and recorded
as an open question rather than a decision.

## Known limitation

On Windows, self-heal restores Cove's registration, but if the user's
`UserChoice` default pointed at the removed executable, Windows may drop the
default entirely and only the user can restore it in Settings. The honest
Windows promise is that magnet links keep working when the association survives
the update, not that they can never break. Linux carries no such caveat for the
handler entry itself, since the desktop entry's `Exec` is rewritten in place.
