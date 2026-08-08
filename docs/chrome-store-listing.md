# Chrome Web Store listing - Cove Download Manager 1.3.6

Lives in `docs/` rather than `dist/`, which `scripts/build_extension.py`
deletes on every build.

Upload `dist/cove-chrome-1.3.6.zip` at
<https://chrome.google.com/webstore/devconsole>.

Paste the block below into **Store listing -> Description**. It is plain text,
not markdown - the store renders it literally, so the bullets are real `•`
characters and there is no other formatting to strip.

## Context: why this listing changed

1.3.5 was rejected under Fostering a Safe Ecosystem - Malicious and Prohibited
Products (reference "Blue Zinc", routing ID FZSL) for facilitating downloads of
copyrighted media, naming YouTube.

1.3.6 removes video handling from the Chrome build entirely rather than
rewording around it: no in-page video pill, no video/audio context menus, no
HLS stream detection, and no page-extractor code. The description below
describes only what the bundle still does. Firefox (AMO) keeps the full
feature set from the same shared source.

## Description

```text
Cove Download Manager (browser extension) hands your downloads off to the Cove desktop app so they download faster and stay organized.

When you start a download, the extension can intercept it and send the link to Cove, along with the page's cookies and referrer so logged-in and protected downloads still work. Cove then downloads the file using multiple connections and manages it in a real queue.

Features:
• Multi-connection downloads (up to 16 connections per file) for higher speeds
• Real download queue with start/pause and per-item control
• Daily schedule window and a global speed cap
• Right-click any link or image and choose "Download with Cove"
• Sends cookies, referrer, and user-agent so authenticated downloads work
• Toggle interception on/off and set a minimum file size and excluded domains

How it works:
Cove takes over the file downloads your browser would otherwise handle on its own - installers, archives, documents, and other direct file links. You stay in control of what it touches: interception can be switched off with a keyboard shortcut, limited by minimum file size, restricted by file type, and disabled per domain. Nothing is downloaded without an action you took.

What's new in 1.3.6:
• Removed in-page video handling from the Chrome version. Cove for Chrome now handles direct file downloads and right-click downloads on links and images.

Requires the free Cove Download Manager desktop app, which provides the download engine. Install Cove, launch it once, then click "Test Connection to Cove" in the extension to link them.

Cove is open source: https://github.com/Sin213/cove-download-manager
```

## What changed from the 1.3.4 copy

| 1.3.4 said | 1.3.6 says | Why |
| --- | --- | --- |
| "Right-click any link, image, video, or audio" | "any link or image" | The video and audio contexts are not registered in the Chrome build |
| "In-page video pill" bullet and paragraph | removed | `content/media-tab.js` is not in the Chrome bundle |
| "YouTube and other supported video pages are handed to Cove for extraction" | removed | The extractor code is not in the Chrome bundle |
| "What's new in 1.3.4: ... works on YouTube ... blob: video sources" | replaced | Described the removed feature |

## Facts behind the copy

| Claim | Source |
| --- | --- |
| 16 connections per file | `cove/config.py:30`, `cove/config.py:133` |
| link and image contexts only | `extension/background.js` `registerContextMenu`, asserted by `tests/extension_background.test.js` |
| no pill, no extractor, no HLS in the bundle | `scripts/build_extension.py` `_CHROME_EXCLUDE`, asserted by `tests/test_extension_bundle.py` |
| interception is bounded by size, type, domain, and a toggle | `extension/background.js` `handleCreated` |

## Before resubmitting

- Screenshots: any that show the video pill must be replaced. The pill does
  not exist in this build and a screenshot of it contradicts the description.
- The store's category and permission justifications should not mention video
  or media capture.
- `nativeMessaging`, `cookies`, and `<all_urls>` are still requested and still
  need their existing justifications.
