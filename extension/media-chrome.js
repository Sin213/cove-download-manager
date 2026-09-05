// Chrome's media capability: the absence of site handling, written down.
//
// media-core.js holds the browser-neutral mechanics - filename derivation, the
// pill's native handoff, the media message surface - and resolves an optional
// capability object from globalThis.CoveMediaCapability at call time. Firefox
// publishes media-sites.js there: page extractors, site title rules, HLS
// observation. Chrome publishes this instead.
//
// It supplies none of those hooks, and that is the whole content of the file.
// The Chrome Web Store rejected 1.3.5 under "Malicious and Prohibited
// Products" for facilitating downloads of copyrighted media, naming a video
// platform (scripts/build_extension.py records which). What that rejection
// was about is site handling; a media element whose own address is an
// ordinary HTTP(S) file is the same download Chrome's own "Save video as"
// offers. So the Chrome bundle ships the shared mechanics and the shared
// pill, and a capability with no hooks leaves every site-dependent decision
// in media-core.js at its neutral default:
//
//   no sitePageUrl      - the media element's own address is the only source,
//                         so a page can never be substituted for the media
//   no titleCleanup     - the tab title is used as the shared core sanitises
//                         it, with no site-specific rewriting
//   no rejectExtension  - a direct file's own extension is always usable
//   no pageFallbackUrl  - a context-menu target the browser cannot hand over
//                         is not downloaded at all, rather than replaced
//   no handleMessage    - the shared message surface is the whole surface
//
// The one thing it does supply is a refusal, described at rejectMediaTarget
// below.
//
// It exists rather than simply being left out because "Chrome ships no site
// adapter" is a product boundary, not an accident of packaging: this file is
// what scripts/build_extension.py puts in the Chrome bundle in place of
// media-sites.js, and tests/test_extension_bundle.py asserts that swap in both
// directions. Leaving the global unset would make a missing file and a
// deliberate exclusion look identical.
//
// Loaded by background.js through importScripts, after media-core.js. It
// registers no listener, observes no request and touches no browser API, so
// nothing here needs the worker to be at any particular point in its life -
// and a service worker restart has nothing of it to lose.

// Playlist and manifest suffixes. A media element's src is allowed to name
// one of these, and it then describes a stream rather than being a file.
const MANIFEST_SUFFIXES = [".m3u8", ".m3u", ".mpd"];

// True when this address is an identifiable playlist or manifest.
//
// Enabling the video and audio context targets put a new kind of address
// within reach of the menu. Chrome ships no stream handling, so forwarding a
// playlist would hand over a description of a stream while presenting it as
// the media the user pointed at. background.js consults this before the new
// media action reaches the native host.
//
// The address is parsed rather than searched, so a query string or fragment
// after the suffix does not hide it and a suffix appearing anywhere else in
// the address is not mistaken for one.
//
// Deliberately a negative check on names that can be identified. An address
// with no suffix at all is not one of these, and extensionless direct media
// stays reachable. Being outside this list is not evidence that an address is
// a direct media file: it is only evidence that it is not a manifest this
// build can recognise. A pathname cannot describe an extensionless manifest,
// and it says nothing about what a redirect eventually returns.
function rejectMediaTarget(value) {
  let pathname;
  try {
    pathname = new URL(value).pathname.toLowerCase();
  } catch {
    // Not an address this can read. The http(s) gate downstream decides.
    return false;
  }
  return MANIFEST_SUFFIXES.some((suffix) => pathname.endsWith(suffix));
}

// Assigned onto globalThis rather than declared: a top-level `const` in a
// worker script is not a global property, and media-core.js looks the
// capability up there. Frozen so the shape cannot be extended at runtime.
//
// media-core.js knows none of the keys below, so it keeps every neutral
// default. background.js reads the refusal directly, which is what confines
// it to the browser that publishes it: Firefox's capability has no such key
// and its menu behaves exactly as it did.
globalThis.CoveMediaCapability = Object.freeze({
  rejectMediaTarget,
});
