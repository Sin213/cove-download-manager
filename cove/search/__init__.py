"""Cove's built-in torrent search.

Every indexer Cove can search is a Cove-maintained adapter shipped inside this
package: there is no plugin loader, no downloaded code and no subprocess. A
source only ever turns untrusted remote text into validated
:class:`~cove.search.models.SearchResult` values.
"""
