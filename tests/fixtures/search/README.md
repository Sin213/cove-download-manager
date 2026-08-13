# Search source fixtures

Hand-written payloads that mirror the shape each indexer's public endpoint
returns. They were trimmed down from single manual captures of

- YTS `/api/v2/list_movies.json`
- apibay `/q.php`
- Nyaa `/?page=rss`
- FitGirl `/?s=` and one repack page
- SubsPlease `/api/?f=search&tz=UTC&s=` (contract verified 2026-08-12)

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
