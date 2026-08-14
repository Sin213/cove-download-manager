"""The static built-in source registry.

The registry is a tuple written out in this file's source, not a discovery
mechanism: Cove must never gain a search source from the filesystem, an entry
point or a downloaded config. These tests pin both the order and the absence
of any registration hook.
"""
import ast

from cove.search import registry
from cove.search.models import Category
from cove.search.sources.base import Source
from cove.search.sources.fitgirl import FitGirlSource
from cove.search.sources.subsplease import SubsPleaseSource


def test_registry_order_is_deterministic():
    assert [source.id for source in registry.SOURCES] == [
        "yts",
        "piratebay",
        "nyaa",
        "fitgirl",
        "subsplease",
    ]


def test_a_new_source_is_appended_rather_than_given_priority():
    """Order is the aggregator's tie-break, so an addition must not reorder.

    SubsPlease is the newest source and goes last; the four that were already
    shipped keep the precedence they were approved with.
    """
    ids = [source.id for source in registry.SOURCES]
    assert ids[:4] == ["yts", "piratebay", "nyaa", "fitgirl"]
    assert ids[-1] == "subsplease"


def test_registry_holds_source_instances():
    for source in registry.SOURCES:
        assert isinstance(source, Source)
        assert source.label and source.homepage
        assert source.enabled_default is True


def test_source_ids_are_unique():
    ids = [source.id for source in registry.SOURCES]
    assert len(set(ids)) == len(ids)


def test_all_returns_every_enabled_source_in_registry_order():
    assert [s.id for s in registry.sources_for(Category.ALL)] == [
        "yts",
        "piratebay",
        "nyaa",
        "fitgirl",
        "subsplease",
    ]


def test_movies_selects_the_movie_sources():
    assert [s.id for s in registry.sources_for(Category.MOVIES)] == ["yts", "piratebay"]


def test_tv_selects_the_tv_sources():
    assert [s.id for s in registry.sources_for(Category.TV)] == ["piratebay"]


def test_anime_selects_the_anime_sources():
    """Anime now has two sources, and Nyaa keeps the precedence it shipped with."""
    assert [s.id for s in registry.sources_for(Category.ANIME)] == [
        "nyaa",
        "subsplease",
    ]


def test_games_selects_the_games_source():
    assert [s.id for s in registry.sources_for(Category.GAMES)] == ["fitgirl"]


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


def test_registry_does_not_scan_or_import_dynamically():
    source = (registry.__file__ or "").replace(".pyc", ".py")
    text = open(source, encoding="utf-8").read()
    for forbidden in ("importlib", "pkgutil", "entry_points", "glob", "iglob", "exec("):
        assert forbidden not in text
