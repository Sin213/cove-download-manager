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
from cove.search.sources.nyaa import NyaaSource
from cove.search.sources.piratebay import PirateBaySource
from cove.search.sources.subsplease import SubsPleaseSource
from cove.search.sources.yts import YtsSource

SOURCES: tuple[Source, ...] = (
    YtsSource(),
    PirateBaySource(),
    NyaaSource(),
    # Appended, not slotted in: this order is the aggregator's tie-break, so a
    # new source goes last rather than taking precedence from an older one.
    FitGirlSource(),
    # Anime's second source. It goes after Nyaa for the same reason: Nyaa was
    # shipped first and keeps the tie-break it was approved with.
    SubsPleaseSource(),
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
