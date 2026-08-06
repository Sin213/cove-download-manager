"""Where to get the companion browser extension.

The desktop app is usable on its own, but download interception and the
in-page "Download with Cove" pill both come from the extension. The store
listings used to live only in README.md, which a running app never shows.
"""
from __future__ import annotations

FIREFOX_EXTENSION_URL = (
    "https://addons.mozilla.org/en-US/firefox/addon/cove-download-manager/"
)
CHROME_EXTENSION_URL = (
    "https://chromewebstore.google.com/detail/cove-download-manager/"
    "liakghhamogjcmmgnmcpephlfecmilnf"
)

EXTENSION_SETUP_FAILED_TEXT = (
    "Cove could not register its browser connector, so the extension will "
    "not be able to talk to Cove even once it is installed."
)


def setup_failure_text(exc: BaseException) -> str:
    """One-line reason for a native-host registration failure.

    Names the exception type as well as its message: the message alone is
    often an empty string or a bare path, which tells a user nothing.
    """
    return f"{type(exc).__name__}: {exc}"


EXTENSION_HELP_TEXT = (
    "Cove captures downloads from your browser through the Cove browser "
    "extension.\n\n"
    "Without it, Cove still works as a normal download manager: add links "
    "with Ctrl+N, paste from the clipboard, or drop them onto the window.\n\n"
    "Install it for your browser:"
)
