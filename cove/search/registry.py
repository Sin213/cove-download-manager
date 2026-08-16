"""The built-in sources Cove can search, in the order it presents them.

This tuple is the whole registry. Adding a source means editing this file and
shipping the change - there is no discovery, no registration hook and nothing
loaded from disk, an entry point or the network, so the set of code that can
run during a search is fixed at build time.
"""
from __future__ import annotations

from cove.search.models import Category
from cove.search.sources.base import Source
from cove.search.sources.fitgirl import FitGirlSource
from cove.search.sources.goggames import GogGamesSource
from cove.search.sources.nekobt import NekoBtSource
from cove.search.sources.nyaa import NyaaSource
from cove.search.sources.subsplease import SubsPleaseSource
from cove.search.sources.torrentscsv import TorrentsCsvSource
from cove.search.sources.yts import YtsSource

# Pirate Bay shipped second here and is temporarily deactivated for 3.6.0: its
# documented API host stopped answering Cove entirely - no response bytes at
# all - while DNS, TCP and TLS to it all still succeed, and curl and a browser
# User-Agent stall the same way. No Cove defect was found and no successor host
# is documented, so cove/search/sources/piratebay.py and its tests stay put;
# re-registering it here is all a reactivation takes.
SOURCES: tuple[Source, ...] = (
    YtsSource(),
    NyaaSource(),
    # Appended, not slotted in: this order is the aggregator's tie-break, so a
    # new source goes last rather than taking precedence from an older one.
    FitGirlSource(),
    # Anime's second source. It goes after Nyaa for the same reason: Nyaa was
    # shipped first and keeps the tie-break it was approved with.
    SubsPleaseSource(),
    # The provider expansion, appended in the order the three were reviewed.
    # Each goes behind every source already shipped for the same reason as the
    # two above: the sources Cove was released with keep the tie-break they
    # were approved with, and an addition earns no precedence over them.
    NekoBtSource(),
    GogGamesSource(),
    # Rutor was removed outright rather than deactivated - adapter, fixtures
    # and tests all deleted - and Torrents-CSV takes its Movies/TV seat, so the
    # active count is unchanged. A 1337x adapter was written for this seat and
    # then removed unshipped: 1337x's own domains answer any non-browser client
    # with a Cloudflare managed challenge, and the mirrors that do answer match
    # only the last word of a query. Neither was fixable from an HTTP client.
    TorrentsCsvSource(),
)


def sources_for(category: Category = Category.ALL) -> list[Source]:
    """The enabled sources that can answer `category`, in registry order.

    ALL means every enabled source; any other category means the sources that
    declare it. A category no source covers yet simply yields nothing.
    """
    return [
        source
        for source in SOURCES
        if source.enabled_default and source.serves(category)
    ]
