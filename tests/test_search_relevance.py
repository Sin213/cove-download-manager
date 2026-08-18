"""The pure relevance ranking contract behind Search v2 (S12).

Search ranking is deliberately narrow: the merged list sorts by relevance
first, then by the deterministic order Search always used. Relevance itself is
defined on whole tokens - the query casefolded and split into maximal runs of
Unicode alphanumerics, every other character a boundary - so a title either
matches a token sequence exactly, by prefix, as a contiguous subsequence, or
as a multiset, or it does not match at all.

Nothing here may depend on sources, the registry, configuration or the
SearchResult model: the ranking is a pure function of two strings, and this
file pins that purity so it can never grow a hidden dependency.
"""
import ast
import inspect

import pytest

from cove.search.relevance import relevance_key, tokenize_relevance_text

# --- tokenization -----------------------------------------------------------


def test_an_empty_query_has_no_tokens():
    assert tokenize_relevance_text("") == ()


def test_whitespace_and_punctuation_only_text_has_no_tokens():
    assert tokenize_relevance_text("   ") == ()
    assert tokenize_relevance_text("!? -_") == ()


def test_text_is_casefolded_before_tokenizing():
    assert tokenize_relevance_text("Elden Ring") == ("elden", "ring")
    assert tokenize_relevance_text("ELDEN RING") == ("elden", "ring")


def test_casefold_crosses_unicode_equivalences():
    # ẞ casefolds to ss, so the street name is the same word in either script.
    assert tokenize_relevance_text("STRASSE") == ("strasse",)
    assert tokenize_relevance_text("Straße") == ("strasse",)


def test_non_alphanumerics_are_token_boundaries():
    assert tokenize_relevance_text("The.Ring-Of_Fire") == (
        "the",
        "ring",
        "of",
        "fire",
    )
    assert tokenize_relevance_text("Frieren - Beyond Journey's End") == (
        "frieren",
        "beyond",
        "journey",
        "s",
        "end",
    )


def test_numeric_runs_are_tokens_like_any_other():
    assert tokenize_relevance_text("1080p") == ("1080p",)
    assert tokenize_relevance_text("Blade Runner 2049") == (
        "blade",
        "runner",
        "2049",
    )


def test_short_tokens_are_preserved():
    assert tokenize_relevance_text("Journey's") == ("journey", "s")
    assert tokenize_relevance_text("The Ring") == ("the", "ring")


def test_unicode_alphanumerics_are_tokens():
    assert tokenize_relevance_text("Pokémon") == ("pokémon",)
    assert tokenize_relevance_text("進撃の巨人") == ("進撃の巨人",)
    assert tokenize_relevance_text("東京 2020") == ("東京", "2020")


def test_duplicate_tokens_are_preserved():
    assert tokenize_relevance_text("the the") == ("the", "the")


def test_tokenization_is_deterministic_and_returns_a_tuple():
    first = tokenize_relevance_text("Elden Ring")
    second = tokenize_relevance_text("Elden Ring")

    assert isinstance(first, tuple)
    assert first == second


# --- tiers ------------------------------------------------------------------

# The tier is the whole answer relevance gives the sort: lower is better.
# Tier 0 is an exact token match, tier 1 a prefix, tier 2 a contiguous
# subsequence anywhere in the title, tier 3 every query token present (as a
# multiset) without contiguity, and tier 4 is no match at all - the neutral
# tier that leaves the pre-existing deterministic order untouched.


def _tier(query: str, title: str) -> int:
    return relevance_key(tokenize_relevance_text(query), title)[0]


def test_exact_token_equality_is_tier_0():
    assert _tier("Elden Ring", "Elden Ring") == 0


def test_exact_match_is_case_insensitive():
    assert _tier("ELDEN RING", "Elden Ring") == 0


def test_exact_match_works_across_unicode_casefold_equivalences():
    assert _tier("STRASSE", "Straße") == 0


def test_a_title_starting_with_the_query_is_tier_1():
    assert _tier("Frieren - Beyond Journey", "Frieren - Beyond Journey's End") == 1
    assert _tier("elden ring", "Elden Ring Shadow of the Erdtree") == 1
    # A trailing numeric token does not break the prefix.
    assert _tier("ring", "Ring 2") == 1


def test_a_duplicate_query_must_be_fully_present_for_the_prefix():
    assert _tier("the the", "The The Movie") == 1


def test_the_query_contiguous_anywhere_is_tier_2():
    # The classic whole-token subsequence: "ring" is in "The Ring", not a
    # prefix of it, and never a substring of a word.
    assert _tier("ring", "The Ring") == 2
    assert _tier("elden ring", "The Ultimate Elden Ring Guide") == 2
    assert _tier("beyond journey", "Frieren - Beyond Journey's End") == 2
    assert _tier("2049", "Blade Runner 2049") == 2


def test_every_query_token_present_but_scattered_is_tier_3():
    assert _tier("dark souls", "Dark Knight of Souls City") == 3


def test_multiset_consumption_allows_one_copy_per_title_copy():
    assert _tier("the the", "The Movie The") == 3
    # One copy in the title cannot satisfy a query asking for two.
    assert _tier("the the", "The") == 4


def test_a_missing_query_token_is_tier_4():
    assert _tier("dark souls", "The Dark Knight") == 4


def test_a_completely_unrelated_title_is_tier_4():
    assert _tier("elden ring", "Spring Festival Collection") == 4


def test_a_query_longer_than_the_title_is_tier_4():
    assert _tier("one two three", "One Two") == 4


def test_an_exact_prefix_still_outranks_a_matching_multiset():
    # Both titles contain every query token, but only one starts with them.
    assert _tier("a b", "A B C") == 1
    assert _tier("a b", "B X A") == 3


def test_an_empty_query_is_neutral_tier_4():
    assert relevance_key((), "Anything at all") == (4,)
    assert relevance_key((), "") == (4,)
    assert _tier("", "Elden Ring") == 4


def test_an_empty_title_is_neutral_tier_4():
    assert relevance_key(("elden", "ring"), "") == (4,)
    assert _tier("x", "!!!") == 4
    assert _tier("x", "") == 4


def test_the_relevance_key_is_a_single_value_tuple():
    assert len(relevance_key(("elden", "ring"), "Elden Ring")) == 1


# --- purity ----------------------------------------------------------------


def test_the_relevance_module_imports_nothing_but_the_stdlib():
    """Pin that ranking cannot grow a hidden dependency.

    The module must stay a pure function of two strings: no sources, no
    registry, no configuration, no SearchResult model, no Qt. The import list
    is the contract, so this test names every module it may import.
    """
    import cove.search.relevance as relevance

    tree = ast.parse(inspect.getsource(relevance))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)

    assert imported == ["__future__", "collections"]