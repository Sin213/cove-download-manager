# Search source fixtures

Hand-written payloads that mirror the shape each indexer's public endpoint
returns. They were trimmed down from single manual captures of

- YTS `/api/v2/list_movies.json`
- apibay `/q.php`
- Nyaa `/?page=rss`
- FitGirl `/?s=` and one repack page

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
