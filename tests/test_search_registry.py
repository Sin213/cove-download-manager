"""The static built-in source registry.

The registry is a tuple written out in this file's source, not a discovery
mechanism: Cove must never gain a search source from the filesystem, an entry
point or a downloaded config. These tests pin both the order and the absence
of any registration hook.
"""
import ast

import pytest

from cove.search import registry
from cove.search.models import Category
from cove.search.service import _MAX_POOL_THREADS
from cove.search.sources.base import Source
from cove.search.sources.fitgirl import FitGirlSource
from cove.search.sources.goggames import GogGamesSource
from cove.search.sources.nekobt import NekoBtSource
from cove.search.sources.rutor import RutorSource
from cove.search.sources.subsplease import SubsPleaseSource

# The sources Cove shipped before the provider expansion that are still active,
# in the order they were approved with. Pirate Bay sat second here until 3.6.0
# temporarily deactivated it; the four that remain keep their relative order,
# because registry order is the aggregator's tie-break and reordering it
# changes which row wins.
_ORIGINAL_ACTIVE = ["yts", "nyaa", "fitgirl", "subsplease"]

# The whole active registry: the surviving pre-expansion prefix untouched, then
# the three reviewed adapters in the order they were implemented and approved.
_EXPECTED_IDS = _ORIGINAL_ACTIVE + ["nekobt", "goggames", "rutor"]

# Deactivated for 3.6.0, not removed: apibay.org stopped answering Cove, curl
# and a browser User-Agent alike, with DNS, TCP and TLS all succeeding and no
# successor host documented. No Cove defect was ever proven, so the adapter and
# its deterministic tests stay for a later reactivation - only the production
# registration goes.
_DEACTIVATED = "piratebay"


def test_registry_order_is_deterministic():
    assert [source.id for source in registry.SOURCES] == _EXPECTED_IDS


def test_a_new_source_is_appended_rather_than_given_priority():
    """Order is the aggregator's tie-break, so an addition must not reorder.

    The pre-expansion sources still active keep the precedence they were
    approved with, and the expansion's three go after them.
    """
    ids = [source.id for source in registry.SOURCES]
    assert ids[:4] == _ORIGINAL_ACTIVE
    assert ids[4:] == ["nekobt", "goggames", "rutor"]


def test_the_surviving_original_sources_keep_their_exact_precedence():
    """Characterisation guard: the pre-expansion prefix may never be reordered.

    Deactivating Pirate Bay closes the gap it left; it must not shuffle the
    sources around it. This fails if an addition is slotted in among the
    sources already shipped, or if a survivor changes places.
    """
    ids = [source.id for source in registry.SOURCES]
    assert ids[:4] == _ORIGINAL_ACTIVE


def test_pirate_bay_is_not_an_active_source():
    """The 3.6.0 product decision, stated where the registry is stated.

    Deactivation is registry-only: nothing here says the adapter is gone, and
    the guard below says the opposite.
    """
    assert _DEACTIVATED not in [source.id for source in registry.SOURCES]
    for category in Category:
        ids = [s.id for s in registry.sources_for(category)]
        assert _DEACTIVATED not in ids, category


def test_the_pirate_bay_adapter_survives_its_deactivation():
    """Reactivation guard: deactivated is not deleted.

    The adapter and its deterministic fixture tests stay in the tree so 3.6.0's
    decision can be reversed by re-registering it, not re-implementing it.
    """
    from cove.search.sources.piratebay import PirateBaySource

    source = PirateBaySource()
    assert source.id == _DEACTIVATED
    assert source.categories == (Category.MOVIES, Category.TV)


def test_registry_holds_source_instances():
    for source in registry.SOURCES:
        assert isinstance(source, Source)
        assert source.label and source.homepage
        assert source.enabled_default is True


def test_source_ids_are_unique():
    ids = [source.id for source in registry.SOURCES]
    assert len(set(ids)) == len(ids)


def test_all_returns_every_enabled_source_in_registry_order():
    assert [s.id for s in registry.sources_for(Category.ALL)] == _EXPECTED_IDS


def test_movies_selects_the_movie_sources():
    assert [s.id for s in registry.sources_for(Category.MOVIES)] == ["yts", "rutor"]


def test_tv_selects_the_tv_sources():
    """Rutor is the sole TV source for 3.6.0, so TV must not come back empty."""
    assert [s.id for s in registry.sources_for(Category.TV)] == ["rutor"]


def test_anime_selects_the_anime_sources():
    """Anime has three sources, and Nyaa keeps the precedence it shipped with."""
    assert [s.id for s in registry.sources_for(Category.ANIME)] == [
        "nyaa",
        "subsplease",
        "nekobt",
    ]


def test_games_selects_the_games_source():
    assert [s.id for s in registry.sources_for(Category.GAMES)] == [
        "fitgirl",
        "goggames",
    ]


def test_fitgirl_is_the_registered_adapter_and_appears_once():
    fitgirl = [s for s in registry.SOURCES if s.id == "fitgirl"]
    assert len(fitgirl) == 1
    assert isinstance(fitgirl[0], FitGirlSource)


def test_fitgirl_stays_out_of_the_categories_it_does_not_serve():
    for category in (Category.MOVIES, Category.TV, Category.ANIME):
        ids = [s.id for s in registry.sources_for(category)]
        assert "fitgirl" not in ids, category


def test_fitgirl_arrives_through_a_plain_static_import():
    """Registration is an ordinary import, not something resolved at runtime."""
    source = (registry.__file__ or "").replace(".pyc", ".py")
    tree = ast.parse(open(source, encoding="utf-8").read())
    imported = {
        (node.module, alias.name)
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert ("cove.search.sources.fitgirl", "FitGirlSource") in imported


def test_subsplease_is_the_registered_adapter_and_appears_once():
    subsplease = [s for s in registry.SOURCES if s.id == "subsplease"]
    assert len(subsplease) == 1
    assert isinstance(subsplease[0], SubsPleaseSource)


def test_subsplease_stays_out_of_the_categories_it_does_not_serve():
    for category in (Category.MOVIES, Category.TV, Category.GAMES):
        ids = [s.id for s in registry.sources_for(category)]
        assert "subsplease" not in ids, category


def test_subsplease_arrives_through_a_plain_static_import():
    """Registration is an ordinary import, not something resolved at runtime."""
    source = (registry.__file__ or "").replace(".pyc", ".py")
    tree = ast.parse(open(source, encoding="utf-8").read())
    imported = {
        (node.module, alias.name)
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert ("cove.search.sources.subsplease", "SubsPleaseSource") in imported


def test_sources_for_defaults_to_all():
    assert registry.sources_for() == list(registry.SOURCES)


def test_no_source_declares_the_all_pseudo_category():
    for source in registry.SOURCES:
        assert Category.ALL not in source.categories


def test_registry_has_no_dynamic_registration_surface():
    for name in ("register", "register_source", "load", "load_plugins", "discover"):
        assert not hasattr(registry, name)


def _imported_names():
    """Every (module, name) the registry pulls in through a plain import."""
    source = (registry.__file__ or "").replace(".pyc", ".py")
    tree = ast.parse(open(source, encoding="utf-8").read())
    return {
        (node.module, alias.name)
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }


# --- the three adapters activated by the provider expansion -------------------
#
# Each was implemented, tested and reviewed as a closed slice of its own. What
# these pin is only the activation: that the registry ships the reviewed object
# itself, once, in the agreed position and category.

_ACTIVATED = (
    ("nekobt", NekoBtSource, "cove.search.sources.nekobt", "NekoBtSource"),
    ("goggames", GogGamesSource, "cove.search.sources.goggames", "GogGamesSource"),
    ("rutor", RutorSource, "cove.search.sources.rutor", "RutorSource"),
)


@pytest.mark.parametrize(
    "source_id, cls", [(sid, cls) for sid, cls, _, _ in _ACTIVATED]
)
def test_an_activated_source_is_the_reviewed_adapter_and_appears_once(source_id, cls):
    """The registry holds the reviewed class itself - no wrapper, no subclass.

    `type(...) is cls` rather than isinstance: a proxy or a subclass slipped in
    at registration would be an unreviewed implementation running in its place.
    """
    matches = [s for s in registry.SOURCES if s.id == source_id]
    assert len(matches) == 1
    assert type(matches[0]) is cls


@pytest.mark.parametrize(
    "source_id, cls, module, name", _ACTIVATED
)
def test_an_activated_source_arrives_through_a_plain_static_import(
    source_id, cls, module, name
):
    """Registration is an ordinary import, not something resolved at runtime."""
    assert (module, name) in _imported_names()


@pytest.mark.parametrize(
    "source_id, categories",
    [
        ("nekobt", (Category.ANIME,)),
        ("goggames", (Category.GAMES,)),
        ("rutor", (Category.MOVIES, Category.TV)),
    ],
)
def test_an_activated_source_stays_out_of_the_categories_it_does_not_serve(
    source_id, categories
):
    for category in (Category.MOVIES, Category.TV, Category.ANIME, Category.GAMES):
        ids = [s.id for s in registry.sources_for(category)]
        if category in categories:
            assert source_id in ids, category
        else:
            assert source_id not in ids, category


def test_sktorrent_is_not_a_registered_source():
    """Characterisation guard for a rejected candidate.

    SkTorrent was dropped from the expansion: its public search exposes no
    proven info hash and no magnet, and the value it does show is only a record
    key. Nothing may register it back without that being reopened.
    """
    assert "sktorrent" not in [source.id for source in registry.SOURCES]
    assert "sktorrent" not in {module for module, _ in _imported_names() if module}


def test_the_registry_does_not_outgrow_the_search_ceiling():
    """The expansion must not make the registry the binding constraint.

    This is the ceiling, not the width a pool actually gets. The service sizes
    its pool to the fanout while the fanout fits under the ceiling, and lets
    the ceiling win past it, so what is pinned here is that the seven active
    sources stay inside the ceiling and activation therefore introduces no
    queueing of its own. The widths that follow from this are pinned in
    tests/test_search_service.py and tests/test_search_pool_capacity.py.
    """
    assert len(registry.sources_for(Category.ALL)) == 7
    assert len(registry.SOURCES) <= _MAX_POOL_THREADS


def test_registry_does_not_scan_or_import_dynamically():
    source = (registry.__file__ or "").replace(".pyc", ".py")
    text = open(source, encoding="utf-8").read()
    for forbidden in ("importlib", "pkgutil", "entry_points", "glob", "iglob", "exec("):
        assert forbidden not in text
