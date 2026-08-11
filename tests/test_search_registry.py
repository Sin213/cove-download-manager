"""The static built-in source registry.

The registry is a tuple written out in this file's source, not a discovery
mechanism: Cove must never gain a search source from the filesystem, an entry
point or a downloaded config. These tests pin both the order and the absence
of any registration hook.
"""
from cove.search import registry
from cove.search.models import Category
from cove.search.sources.base import Source


def test_registry_order_is_deterministic():
    assert [source.id for source in registry.SOURCES] == ["yts", "piratebay", "nyaa"]


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
    ]


def test_movies_selects_the_movie_sources():
    assert [s.id for s in registry.sources_for(Category.MOVIES)] == ["yts", "piratebay"]


def test_tv_selects_the_tv_sources():
    assert [s.id for s in registry.sources_for(Category.TV)] == ["piratebay"]


def test_anime_selects_the_anime_sources():
    assert [s.id for s in registry.sources_for(Category.ANIME)] == ["nyaa"]


def test_games_has_no_built_in_source_yet():
    assert registry.sources_for(Category.GAMES) == []


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
