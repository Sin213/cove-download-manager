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

EXTENSION_HELP_TEXT = (
    "Cove captures downloads from your browser through the Cove browser "
    "extension.\n\n"
    "Without it, Cove still works as a normal download manager: add links "
    "with Ctrl+N, paste from the clipboard, or drop them onto the window.\n\n"
    "Install it for your browser:"
)
