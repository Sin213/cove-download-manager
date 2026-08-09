# Reddit feed video downloads

Downloading a video from the Reddit feed fails. Inside a thread it works. This
routes the feed case through yt-dlp using the post's own permalink.

## The problem

The feed's player is MSE: `video.currentSrc` is a `blob:`, Reddit exposes no
`data-hls-url` on feed cards, and Cove's stream sniffer only records HLS, so
nothing on the page names the media. The pill used to fall back to the page
address, which handed `https://www.reddit.com/` to aria2 as though it were a
file; Reddit answers unfamiliar clients with 403. That fallback is now gone and
the pill reports "No video found" instead, which is honest but still does not
download the video.

Inside a thread the post's player does expose `data-hls-url`, so a real
playlist URL resolves and ffmpeg downloads it with audio. That path works and
is not changed here.

Reddit keeps DASH audio in a separate file from video, so naming the sniffed
`DASH_720.mp4` would produce silent video. Anything that fixes the feed has to
mux, which is what yt-dlp already does for Reddit.

## Approach

When nothing else resolves, send the post's permalink and let Cove's existing
extractor path handle it:

    pill click
      -> candidateUrl() finds nothing (blob:, no data-hls-url, no sniffed stream)
      -> walk up from the clicked <video> to its post card
      -> read that card's permalink
      -> send https://www.reddit.com/r/<sub>/comments/<id>/...
      -> is_extractor_url() -> yt-dlp -> video and audio muxed

This reuses the mechanism YouTube already uses. No new download path.

## Changes

### 1. `cove/extractor.py` - `is_extractor_url`

Accept `/r/<sub>/comments/<id>` on `reddit.com` and the host variants `www`,
`old`, `new`, `sh` and `np`. Structured like the existing YouTube branch.

Rejected: `/r/<sub>` alone, the front page, user pages, and `v.redd.it` media
links. A bare media link stays a direct download, because that is what someone
pasting one is asking for.

This is app-wide, so pasting a Reddit thread link into Cove starts working too.
Today that downloads HTML or fails with 403.

### 2. `extension/content/media-tab.js` - `postPermalink(video)`

New function. Walks up from the clicked video with `closest()` and reads, in
order:

- `shreddit-post[permalink]` - new Reddit
- `[data-permalink]` - old Reddit
- a scoped `a[href*="/comments/"]` within the post card

Relative values are resolved against `location.origin`. Returns `""` when the
video is not inside a recognisable post card.

Called from `videoUrl` and the click handler *after* `candidateUrl`, so it only
runs when nothing else named the media. Thread pages resolve a playlist first
and never reach it.

**Ancestor-scoped only.** No `document.querySelector`. A feed holds many posts;
a document-wide lookup would download a different post's video, which looks
like a download that worked.

### 3. `extension/media.js` - `extractorPageUrl`

Learns the same Reddit rule, so the background script and the app agree on what
is extractor-backed. Without this the background would keep treating a Reddit
permalink as an ordinary URL.

### 4. `extension/content/media-tab.js` - `embeddedStreamUrl`

Existing bug, adjacent enough to fix here. Line 70 reads:

    video.closest("[data-hls-url]") || document.querySelector("[data-hls-url]")

The second branch returns the first player in the whole document. On a
multi-video page that is the wrong video, downloaded silently and confidently.
It has not bitten on the feed only because Reddit exposes no `data-hls-url`
there at all - and this change is what puts the pill to work on feeds.

Scope it to `closest()`.

## Error handling

No permalink found means the existing "No video found" path. No new failure
mode and no new message.

yt-dlp missing affects only the feed, which fails today regardless. The thread
path does not depend on yt-dlp and is untouched.

## Testing

Extension (`tests/extension_media_tab.test.js`):

- a new-Reddit feed card resolves its permalink
- an old-Reddit feed card resolves its permalink
- a feed of several posts resolves the *clicked* post, not the first
- a video in no recognisable card still reports "No video found"
- a thread page sends the playlist URL and never the permalink
- a relative permalink is resolved against the origin
- YouTube is unchanged
- `embeddedStreamUrl` does not reach across to another player's `data-hls-url`

App (`tests/test_extractor.py`):

- each host variant with a post path is an extractor URL
- `/r/<sub>`, the front page, user pages and `v.redd.it` are not
- YouTube behaviour unchanged

## Shipping

Folds into extension 1.4.7, which has not been submitted anywhere, so one AMO
submission carries the audit fixes, the pill fixes and this.

Chrome is unaffected: `media.js` and `content/` are excluded from that bundle,
and `tests/test_extension_bundle.py` enforces it. The `cove/extractor.py`
change is in the app, not the extension, so it carries no store risk.

Firefox still needs its manual check before upload - load `dist/firefox`
unpacked and confirm the video context menu and the pill both work.
