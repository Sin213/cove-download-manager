# Search source fixtures

Hand-written payloads that mirror the shape each indexer's public endpoint
returns. They were trimmed down from single manual captures of

- YTS `/api/v2/list_movies.json`
- apibay `/q.php`
- Nyaa `/?page=rss`
- FitGirl `/?s=` and one repack page
- SubsPlease `/api/?f=search&tz=UTC&s=` (contract verified 2026-08-12)
- nekoBT `/api/v1/torrents/search?query=` (contract verified 2026-08-14)
- GOG Games `/search?search=` (contract verified 2026-08-14)
- Rutor `/search/0/0/000/0/<query>` (contract verified 2026-08-15)

and then edited by hand: titles and hashes are invented, and no cookies,
tokens, headers or other request metadata were kept. Nothing refreshes these
files automatically - the test suite must never reach an indexer.

Each source has a valid payload, a payload with individually broken rows that
must be dropped, and a structurally unusable payload that must raise
`SourceError(parse)`.

The FitGirl files are HTML rather than a feed, so they come in pairs: a search
page (results, no results, unrecognised) and the repack pages its entries link
to (two magnets, one magnet, no magnet, a malformed magnet first, and an
unrecognised page). They keep only the elements the parser keys on - the body
class, the `article` entries, their `entry-title` link and `entry-date`, and
the `entry-content` region - not the surrounding page.

The SubsPlease files mirror that API's two published shapes: an object keyed by
release name when it matched something, and a bare `[]` when it did not. Show
names, episode numbers and every info hash are invented, the magnets carry a
single `tracker.example` announce URL rather than the real tracker list, and a
drift file renames the `downloads` container so the parser must refuse it
instead of reporting an empty search.

The nekoBT files keep only the envelope the parser reads - `data.results` and
the fields of a row - not the media, similar-media, uploader or debug blocks
the API also sends. Titles and info hashes are invented, the magnets carry a
single `tracker.example` announce URL, `filesize`/`seeders`/`leechers` stay
strings and `uploaded_at` stays epoch milliseconds because that is what the
API publishes, and the unusable file renames `results` to `items` so the
parser must refuse it instead of reporting an empty search.

The GOG Games files keep the `data` list the parser reads plus a trimmed
`links`/`meta` paginator, because the paginator is the reason the endpoint is
easy to get wrong: it echoes a `query=` parameter the API ignores, while the
parameter that actually filters is `search=`. Titles, slugs and info hashes are
invented, `last_update` stays an ISO 8601 instant with a `Z` offset and
`release_timestamp` stays epoch seconds because that is what the API publishes,
one valid row carries a null `infohash` (a catalogue entry with no release
behind it yet, which the parser must skip rather than fail on), and the unusable
file renames `data` to `items` so the parser must refuse it instead of
reporting an empty search.

The Rutor files are HTML and keep only the `id="index"` region: the results
header row the parser recognises the page by, and the result rows under it.
Rutor's page is one table with no per-cell classes, so the fixtures preserve
the two row shapes it actually emits - five cells when a torrent has comments
and four when it does not, the title cell absorbing the difference with a
colspan - because the comment cell is shaped exactly like the size cell and a
parser reading cells by position would confuse them. Titles and info hashes are
invented but one stays Cyrillic so the UTF-8 decode is pinned, the magnets keep
Rutor's own `dn=rutor.info` and two announce URLs (rewritten to `tracker.example`
and `retracker.local`) because the adapter forwards the provider's magnet rather
than rebuilding it, and `265.94&nbsp;GB` / `09&nbsp;Июл&nbsp;26` keep the
non-breaking spaces and the abbreviated Russian month the page publishes. The
unusable file is a page with no results header that still carries a magnet in a
table of the right shape, so zero parsed rows must raise instead of passing for
an empty search.
