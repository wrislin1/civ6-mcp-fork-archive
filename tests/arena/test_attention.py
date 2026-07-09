import pytest

from civ_mcp.arena.attention import (
    Directive,
    has_directive_lines,
    parse_directive,
)


def test_plain_directive():
    d = parse_directive("done.\nSKIP: 3\nWAKE IF: GREAT_PERSON_AVAILABLE, CITY_GREW", 5)
    assert d == Directive(skip=3, wake_if=("GREAT_PERSON_AVAILABLE", "CITY_GREW"),
                          unknown_tokens=(), clamped=False)

def test_markdown_variants():
    # models reformat plain markers into markdown (the memory.py lesson)
    for text in ("**SKIP:** 2", "- SKIP: 2", "## SKIP: 2", "*skip*: 2 turns"):
        d = parse_directive(text, 5)
        assert d is not None and d.skip == 2, text

def test_clamping():
    assert parse_directive("SKIP: 99", 5) == Directive(5, (), (), True)
    assert parse_directive("SKIP: 0", 5) == Directive(1, (), (), True)
    assert parse_directive("SKIP: 2", 3).skip == 2

def test_wake_if_without_skip_is_inert():
    assert parse_directive("WAKE IF: CITY_GREW", 5) is None
    assert has_directive_lines("WAKE IF: CITY_GREW")  # ack loop can say "not recognized"

def test_unknown_tokens_dropped_not_fatal():
    d = parse_directive("SKIP: 2\nWAKE IF: CITY_GREW, SCIENCE_OVER_200", 5)
    assert d.wake_if == ("CITY_GREW",)
    assert d.unknown_tokens == ("SCIENCE_OVER_200",)

def test_garbage_no_directive():
    assert parse_directive("SKIP: soon-ish, when quiet", 5) is None
    assert parse_directive("nothing here", 5) is None
    assert not has_directive_lines("nothing here")

def test_prose_mentioning_skip_mid_sentence_does_not_match():
    assert parse_directive("I will skip: nothing important this turn happened", 5) is None

def test_first_parsed_skip_wins():
    # two valid SKIP lines: the first parsed one wins
    assert parse_directive("SKIP: 2\nSKIP: 4", 5).skip == 2
    # a SKIP line with no integer does not block a later parseable one
    assert parse_directive("SKIP: soon\nSKIP: 4", 5).skip == 4
