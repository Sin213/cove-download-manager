# Reddit Feed Video Downloads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Downloading a video from the Reddit feed sends the post's permalink to yt-dlp, so it arrives with audio, instead of failing.

**Architecture:** When nothing on the page names the media, walk up from the clicked `<video>` to its post card and read that card's permalink. Cove's existing extractor path recognises Reddit post URLs and hands them to yt-dlp, which muxes Reddit's separate audio and video. No new download path.

**Tech Stack:** Python 3.10+ (`cove/extractor.py`), plain JS extension (`extension/`), `node --test`, pytest.

## Global Constraints

- Permalink lookup is ancestor-scoped only. Never `document.querySelector` - a feed holds many posts and a document-wide lookup downloads a different post's video.
- Thread pages must keep their existing HLS/ffmpeg path. `postPermalink` is tried only after `candidateUrl`.
- `v.redd.it` links stay direct downloads. They are not extractor URLs.
- Reddit hosts, used verbatim everywhere: `reddit.com`, `old.reddit.com`, `new.reddit.com`, `sh.reddit.com`, `np.reddit.com`, each also matching a `www.` prefix.
- Reddit post path, used verbatim everywhere: `/r/<sub>/comments/<id>`.
- Nothing may enter the Chrome bundle. `media.js` and `content/` are Firefox-only and `tests/test_extension_bundle.py` enforces it.
- Run JS tests with `rtk proxy node --test <file>`; bare `node --test` is rewritten by the RTK hook.

---

### Task 1: Reddit post URLs are extractor URLs in the app

**Files:**
- Modify: `cove/extractor.py:11` (constants), `cove/extractor.py:17-29` (`is_extractor_url`)
- Test: `tests/test_extractor.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `is_extractor_url(url: str) -> bool` now returns True for Reddit post URLs. `cove/queue.py:908-911` already calls it to choose `yt-dlp` over `aria2`; no change needed there.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_extractor.py`:

```python
def test_reddit_post_urls_go_to_the_extractor():
    assert is_extractor_url("https://www.reddit.com/r/aww/comments/abc123/a_title/")
    assert is_extractor_url("https://reddit.com/r/aww/comments/abc123/a_title/")
    assert is_extractor_url("https://old.reddit.com/r/aww/comments/abc123/a_title/")
    assert is_extractor_url("https://new.reddit.com/r/aww/comments/abc123/a_title/")
    assert is_extractor_url("https://sh.reddit.com/r/aww/comments/abc123/a_title/")
    assert is_extractor_url("https://np.reddit.com/r/aww/comments/abc123/a_title/")
    assert is_extractor_url("https://www.reddit.com/r/aww/comments/abc123")


def test_non_post_reddit_urls_are_left_alone():
    # The feed, a subreddit and a user page are pages, not videos.
    assert not is_extractor_url("https://www.reddit.com/")
    assert not is_extractor_url("https://www.reddit.com/r/aww/")
    assert not is_extractor_url("https://www.reddit.com/r/aww")
    assert not is_extractor_url("https://www.reddit.com/user/someone/")
    # A direct media link is what the user asked for: download it as-is.
    assert not is_extractor_url("https://v.redd.it/abc123/DASH_720.mp4")
    assert not is_extractor_url("https://preview.redd.it/abc123.jpg")
    # Not Reddit at all.
    assert not is_extractor_url("https://reddit.com.evil.test/r/aww/comments/x/y/")
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_extractor.py -q -k reddit`
Expected: FAIL, both tests, on the first `assert is_extractor_url(...)`.

- [ ] **Step 3: Add the rule**

In `cove/extractor.py`, beside `_YOUTUBE_PATH` at line 11:

```python
_YOUTUBE_PATH = re.compile(r"^/(?:shorts|live|embed)/[^/]+")
# Reddit keeps DASH audio in a separate file from video, so a direct media
# link downloads silent. yt-dlp resolves the post and muxes the two.
_REDDIT_HOSTS = frozenset({
    "reddit.com", "old.reddit.com", "new.reddit.com",
    "sh.reddit.com", "np.reddit.com",
})
_REDDIT_POST = re.compile(r"^/r/[^/]+/comments/[^/]+")
```

In `is_extractor_url`, immediately after the `youtu.be` branch and before the
YouTube host guard:

```python
        if host == "youtu.be":
            return bool(parsed.path.strip("/"))
        if host in _REDDIT_HOSTS:
            return bool(_REDDIT_POST.match(parsed.path))
        if host not in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
            return False
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_extractor.py -q`
Expected: PASS, all tests in the file.

- [ ] **Step 5: Run the full Python suite**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/ -q`
Expected: `1752 passed, 12 skipped` plus the 2 new tests, so `1754 passed, 12 skipped`.

- [ ] **Step 6: Commit**

```bash
git add cove/extractor.py tests/test_extractor.py
git commit -m "Route Reddit post URLs to the extractor"
```

---

### Task 2: The pill sends the clicked post's permalink

**Files:**
- Modify: `tests/extension_media_tab.test.js:61-79` (StubNode `closest`/`querySelector`)
- Modify: `extension/content/media-tab.js` (`postPermalink`, `videoUrl`, `onPillClick`)
- Modify: `extension/media.js:16-26` (`extractorPageUrl`)
- Test: `tests/extension_media_tab.test.js`

**Interfaces:**
- Consumes: `is_extractor_url` from Task 1 recognising the URLs this produces.
- Produces: `postPermalink(video) -> string` in the content script, returning an absolute post URL or `""`.

- [ ] **Step 1: Teach the test harness to walk ancestors**

The stub's `closest` only matches `video` on itself, and `querySelector`
always returns null, so neither can express a post card. Replace both methods
in `tests/extension_media_tab.test.js`:

```js
  closest(selector) {
    const parts = String(selector).split(",").map((s) => s.trim());
    let node = this;
    while (node) {
      for (const part of parts) {
        const attr = part.match(/^\[([^\]=]+)\]$/);
        if (attr) {
          if (node.getAttribute(attr[1]) != null) return node;
        } else if (/^[a-z][a-z0-9-]*$/i.test(part)) {
          if (node.tagName === part.toUpperCase()) return node;
        }
      }
      node = node.parentNode;
    }
    return null;
  }

  querySelector(selector) {
    const match = String(selector).match(/^a\[href\*="([^"]+)"\]$/);
    if (!match) return null;
    const needle = match[1];
    const walk = (node) => {
      for (const child of node.children) {
        const href = child.tagName === "A" ? child.getAttribute("href") : null;
        if (href && href.includes(needle)) return child;
        const found = walk(child);
        if (found) return found;
      }
      return null;
    };
    return walk(this);
  }
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/extension_media_tab.test.js`:

```js
function redditCard({ tag = "SHREDDIT-POST", attrs = {}, link = "" } = {}) {
  const card = new StubNode(tag);
  for (const [name, value] of Object.entries(attrs)) card[name] = value;
  if (link) {
    const anchor = new StubNode("A");
    anchor.href = link;
    card.appendChild(anchor);
  }
  return card;
}

function feedHarness(cards) {
  const videos = [];
  for (const card of cards) {
    const video = blobVideo({ top: 200 });
    card.appendChild(video);
    videos.push(video);
  }
  const harness = loadMediaTab({ href: "https://www.reddit.com/", videos });
  // The cards have to be reachable as ancestors of their videos.
  for (const card of cards) harness.body.appendChild(card);
  return { harness, videos };
}

test("a new-Reddit feed card resolves its own permalink", async () => {
  const card = redditCard({ attrs: { permalink: "/r/aww/comments/abc123/a_title/" } });
  const { harness, videos } = feedHarness([card]);

  await clickPill(harness, videos[0]);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.equal(download.url, "https://www.reddit.com/r/aww/comments/abc123/a_title/");
});

test("an old-Reddit feed card resolves its own permalink", async () => {
  const card = redditCard({
    tag: "DIV",
    attrs: { "data-permalink": "/r/aww/comments/abc123/a_title/" },
  });
  const { harness, videos } = feedHarness([card]);

  await clickPill(harness, videos[0]);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.equal(download.url, "https://www.reddit.com/r/aww/comments/abc123/a_title/");
});

test("a card with only a comments link still resolves", async () => {
  const card = redditCard({ tag: "ARTICLE", link: "/r/aww/comments/abc123/a_title/" });
  const { harness, videos } = feedHarness([card]);

  await clickPill(harness, videos[0]);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.equal(download.url, "https://www.reddit.com/r/aww/comments/abc123/a_title/");
});

test("a feed of several posts resolves the clicked one", async () => {
  const first = redditCard({ attrs: { permalink: "/r/aww/comments/first/one/" } });
  const second = redditCard({ attrs: { permalink: "/r/aww/comments/second/two/" } });
  const third = redditCard({ attrs: { permalink: "/r/aww/comments/third/three/" } });
  const { harness, videos } = feedHarness([first, second, third]);

  await clickPill(harness, videos[1]);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.equal(
    download.url, "https://www.reddit.com/r/aww/comments/second/two/",
    "the pill must download the post it was clicked on"
  );
});

test("a video in no recognisable card still reports no video", async () => {
  const video = blobVideo({ top: 200 });
  const harness = loadMediaTab({ href: "https://www.reddit.com/", videos: [video] });

  const host = await clickPill(harness, video);

  assert.equal(harness.sent.find((m) => m.type === "downloadMedia"), undefined);
  const pill = host.shadowRoot.children.find((n) => n.className === "cove-pill");
  assert.equal(pill.children[0].textContent, "No video found");
});

test("a thread page sends the playlist and never the permalink", async () => {
  // Inside a thread the player names its own stream, which is the path that
  // already works. The permalink must not displace it.
  const card = redditCard({ attrs: { permalink: "/r/aww/comments/abc123/a_title/" } });
  const video = blobVideo({ top: 200 });
  video["data-hls-url"] = "https://v.redd.it/abc123/HLSPlaylist.m3u8";
  card.appendChild(video);
  const harness = loadMediaTab({
    href: "https://www.reddit.com/r/aww/comments/abc123/a_title/",
    videos: [video],
  });
  harness.body.appendChild(card);

  await clickPill(harness, video);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.equal(download.url, "https://v.redd.it/abc123/HLSPlaylist.m3u8");
});
```

- [ ] **Step 3: Run the tests and watch them fail**

Run: `rtk proxy node --test tests/extension_media_tab.test.js`
Expected: the five permalink tests FAIL (no `downloadMedia` message is sent, so
`download` is `undefined`). "a video in no recognisable card" and the thread
test PASS already.

- [ ] **Step 4: Add postPermalink to the content script**

In `extension/content/media-tab.js`, above `extractorPageUrl`:

```js
  const REDDIT_HOSTS = [
    "reddit.com", "old.reddit.com", "new.reddit.com",
    "sh.reddit.com", "np.reddit.com",
  ];
  const REDDIT_POST_PATH = /^\/r\/[^/]+\/comments\/[^/]+/;

  function isRedditHost() {
    return REDDIT_HOSTS.includes(
      location.hostname.toLowerCase().replace(/^www\./, "")
    );
  }

  // The feed's player is MSE, so nothing on the page names the media. The post
  // it belongs to does, though, and yt-dlp can resolve that - which also muxes
  // Reddit's separate audio track. Strictly ancestor-scoped: a feed holds many
  // posts, and reaching across to another card downloads the wrong video.
  function postPermalink(video) {
    if (!video || !isRedditHost()) return "";
    const card = video.closest("shreddit-post, [data-permalink], article");
    if (!card) return "";
    let href =
      (card.getAttribute && (card.getAttribute("permalink") ||
                             card.getAttribute("data-permalink"))) || "";
    if (!href && card.querySelector) {
      const link = card.querySelector('a[href*="/comments/"]');
      href = (link && link.getAttribute("href")) || "";
    }
    if (!href) return "";
    try {
      const url = new URL(href, location.origin);
      return REDDIT_POST_PATH.test(url.pathname) ? url.href : "";
    } catch {
      return "";
    }
  }
```

- [ ] **Step 5: Use it, after everything that names the media directly**

In `videoUrl`:

```js
  function videoUrl(video) {
    return extractorPageUrl() || candidateUrl(video) || postPermalink(video);
  }
```

In `onPillClick`, the url line:

```js
    const url = extractorPageUrl() || currentUrl || candidateUrl(activeVideo) ||
      postPermalink(activeVideo);
```

- [ ] **Step 6: Teach the background the same rule**

In `extension/media.js`, replace `extractorPageUrl` (lines 16-26):

```js
const REDDIT_HOSTS = [
  "reddit.com", "old.reddit.com", "new.reddit.com",
  "sh.reddit.com", "np.reddit.com",
];
const REDDIT_POST_PATH = /^\/r\/[^/]+\/comments\/[^/]+/;

function extractorPageUrl(value) {
  try {
    const url = new URL(value || "");
    const host = url.hostname.toLowerCase().replace(/^www\./, "");
    if (host === "youtu.be" && url.pathname.length > 1) return url.href;
    if (REDDIT_HOSTS.includes(host)) {
      return REDDIT_POST_PATH.test(url.pathname) ? url.href : "";
    }
    if (!["youtube.com", "m.youtube.com", "music.youtube.com"].includes(host)) return "";
    if (url.pathname === "/watch" && url.searchParams.get("v")) return url.href;
    if (/^\/(?:shorts|live|embed)\/[^/]+/.test(url.pathname)) return url.href;
  } catch {}
  return "";
}
```

- [ ] **Step 7: Run the tests and watch them pass**

Run: `rtk proxy node --test tests/extension_media_tab.test.js`
Expected: PASS, all tests.

Run: `rtk proxy node --test tests/extension_background.test.js`
Expected: PASS, 56 tests. `extractorPageUrl` is shared, so this proves YouTube
handling is unchanged.

- [ ] **Step 8: Commit**

```bash
git add extension/content/media-tab.js extension/media.js tests/extension_media_tab.test.js
git commit -m "Send the Reddit post permalink when the feed names no media"
```

---

### Task 3: A player's stream never comes from another player

**Files:**
- Modify: `extension/content/media-tab.js:65-73` (`embeddedStreamUrl`)
- Test: `tests/extension_media_tab.test.js`

**Interfaces:**
- Consumes: the StubNode `closest` from Task 2 Step 1.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

Append to `tests/extension_media_tab.test.js`:

```js
test("a player does not borrow another player's stream", async () => {
  // The fallback used to be document-wide, so on a page with several players
  // every one of them resolved to the first player's stream - a download that
  // looks like it worked and fetches the wrong video.
  const owner = new StubNode("DIV");
  owner["data-hls-url"] = "https://v.redd.it/first/HLSPlaylist.m3u8";
  const ownerVideo = blobVideo({ top: 100 });
  owner.appendChild(ownerVideo);

  const bare = new StubNode("DIV");
  const bareVideo = blobVideo({ top: 400 });
  bare.appendChild(bareVideo);

  const harness = loadMediaTab({
    href: "https://example.test/feed",
    videos: [ownerVideo, bareVideo],
  });
  harness.body.appendChild(owner);
  harness.body.appendChild(bare);

  const host = await clickPill(harness, bareVideo);

  assert.equal(
    harness.sent.find((m) => m.type === "downloadMedia"), undefined,
    "a player with no stream of its own must not claim one"
  );
  const pill = host.shadowRoot.children.find((n) => n.className === "cove-pill");
  assert.equal(pill.children[0].textContent, "No video found");
});
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `rtk proxy node --test tests/extension_media_tab.test.js`
Expected: FAIL - a `downloadMedia` message is sent carrying
`https://v.redd.it/first/HLSPlaylist.m3u8`, the other player's stream.

- [ ] **Step 3: Scope the lookup to the player itself**

In `extension/content/media-tab.js`, in `embeddedStreamUrl`:

```js
  function embeddedStreamUrl(video) {
    // Reddit's embed exposes both DASH and HLS URLs in the player container,
    // but normally requests only DASH. Prefer HLS because Cove can download
    // and merge that stream directly.
    //
    // Ancestor-scoped on purpose. A document-wide lookup returns the first
    // player on the page, so on a feed every video resolved to the first
    // post's stream and downloaded the wrong video without any sign of error.
    const player = video.closest("[data-hls-url]");
    const hlsUrl = player ? player.getAttribute("data-hls-url") : "";
    return isHttpUrl(hlsUrl) ? hlsUrl : "";
  }
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `rtk proxy node --test tests/extension_media_tab.test.js`
Expected: PASS, all tests.

- [ ] **Step 5: Commit**

```bash
git add extension/content/media-tab.js tests/extension_media_tab.test.js
git commit -m "Scope the embedded stream lookup to the player it belongs to"
```

---

### Task 4: Ship it in the 1.4.7 bundle

**Files:**
- Modify: `dist/` (generated, gitignored)

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: `dist/cove-firefox-1.4.7.zip` and `dist/cove-chrome-1.3.8.zip` carrying the change.

- [ ] **Step 1: Run every suite**

```bash
QT_QPA_PLATFORM=offscreen python -m pytest tests/ -q
rtk proxy node --test tests/extension_media_tab.test.js
rtk proxy node --test tests/extension_background.test.js
rtk proxy node --test tests/extension_diagnostics.test.js
```

Expected: Python `1754 passed, 12 skipped`. JS: media-tab 28 + 7 new = 35 pass,
background 56 pass, diagnostics 44 pass. Zero failures anywhere.

- [ ] **Step 2: Rebuild the bundles**

Run: `python scripts/build_extension.py`
Expected: four lines reporting firefox v1.4.7, chrome v1.3.8 and the two zips.

- [ ] **Step 3: Verify Chrome took none of it**

```bash
test -e dist/chrome/content && echo "BAD: content/ in chrome" || echo "ok: no content/"
test -e dist/chrome/media.js && echo "BAD: media.js in chrome" || echo "ok: no media.js"
QT_QPA_PLATFORM=offscreen python -m pytest tests/test_extension_bundle.py -q
```

Expected: both `ok:` lines, and 10 passed. The Chrome bundle carries no video
handling, which is what its store listing depends on.

- [ ] **Step 4: Confirm the bundle matches source**

```bash
diff -q extension/content/media-tab.js dist/firefox/content/media-tab.js
diff -q extension/media.js dist/firefox/media.js
```

Expected: no output from either.

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "Rebuild the extension bundles with the Reddit feed support"
```

Note: `dist/` is gitignored, so this commit carries only tracked changes. If
nothing is staged, skip it.

---

## Manual verification, before any AMO upload

Not automatable and still outstanding from the 1.4.7 bump:

1. Load `dist/firefox/` in Firefox as a temporary add-on.
2. Right-click a video: "Download with Cove" must appear.
3. Hover a video: the pill must appear.
4. On the Reddit feed, click the pill on a video and confirm it downloads with
   sound.
5. Inside a thread, click the pill and confirm it still downloads.

Step 4 is the new behaviour. Step 5 is the path this change must not have
broken. If step 4 reports "No video found", the card selectors in
`postPermalink` do not match the live markup - capture the card's HTML and
adjust that one function.
