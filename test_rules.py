#!/usr/bin/env python3
"""Tests for the per-channel rules.txt parser in bot.py.

Run with:
    python3 test_rules.py

Deliberately dependency-free (no pytest) so it runs anywhere bot.py itself
runs. Importing bot is safe: the TUI only starts under __main__.
"""

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import bot  # noqa: E402

_failures: list[str] = []


def check(label, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + f"{label}: got {got!r}")
    if not ok:
        _failures.append(f"{label}: got {got!r}, want {want!r}")


def with_rules(text: str):
    """Point bot.RULES_FILE at a throwaway rules.txt holding `text`."""
    path = pathlib.Path(tempfile.mkdtemp()) / "rules.txt"
    path.write_text(text, encoding="utf-8")
    bot.RULES_FILE = path


def test_shipped_rules():
    print("shipped rules.txt")
    shipped = bot.load_rules()
    check("only the EDGE_ATS section", sorted(shipped), ["EDGE_ATS"])
    edge = ["EDGE_ATS", "#3", bot.ALL_CHANNELS]
    check("ping", bot.find_reply("ping", edge), "pong")
    # Regression: the reply is taken literally, so a quoted value would leak
    # its quote characters into the outgoing message.
    check("test reply has no stray quotes", bot.find_reply("please test this", edge), "Bot: Hello!")
    check("another channel gets nothing", bot.find_reply("ping", ["LongFast", "#0", bot.ALL_CHANNELS]), None)


def test_sections_and_precedence():
    print("sections and precedence")
    with_rules(
        "# comment\n"
        "[EDGE_ATS]\n"
        "ping=edge-pong\n"
        "\n"
        "[#0]\n"
        "ping=primary-pong\n"
        "\n"
        "[*]\n"
        "ping=global-pong\n"
        "hello=hi everyone\n"
    )
    check("channel name beats [*]", bot.find_reply("PING", ["EDGE_ATS", "#3", "*"]), "edge-pong")
    check("#index section resolves", bot.find_reply("ping", ["#0", "*"]), "primary-pong")
    check("unknown channel falls back to [*]", bot.find_reply("ping", ["Nope", "#7", "*"]), "global-pong")
    check("[*] key not shadowed still fires", bot.find_reply("say Hello there", ["EDGE_ATS", "#3", "*"]), "hi everyone")
    check("no keyword matches", bot.find_reply("nothing here", ["EDGE_ATS", "*"]), None)


def test_legacy_flat_file():
    print("legacy flat file (no section headers)")
    with_rules("# old format\nping=pong\nhelp=x\n")
    check("parsed as all-channels", bot.load_rules(), {"*": {"ping": "pong", "help": "x"}})
    check("matches any channel", bot.find_reply("ping", ["Whatever", "#5", "*"]), "pong")


def test_header_edge_cases():
    print("header and value edge cases")
    with_rules("[EDGE_ATS]\nkey=has=equals=in=value\n[  spaced  ]\na=b\n[]\nc=d\n")
    rules = bot.load_rules()
    check("value keeps its later '='", rules["EDGE_ATS"]["key"], "has=equals=in=value")
    check("header whitespace trimmed", rules.get("spaced"), {"a": "b"})
    check("empty header means all channels", rules.get("*"), {"c": "d"})


def test_empty_section_is_kept():
    print("declared but empty section")
    with_rules("[EDGE_ATS]\n")
    check("section present with no rules", bot.load_rules(), {"EDGE_ATS": {}})
    check("nothing matches", bot.find_reply("ping", ["EDGE_ATS", "*"]), None)


if __name__ == "__main__":
    original = bot.RULES_FILE
    try:
        test_shipped_rules()
        test_sections_and_precedence()
        test_legacy_flat_file()
        test_header_edge_cases()
        test_empty_section_is_kept()
    finally:
        bot.RULES_FILE = original

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for failure in _failures:
            print(" -", failure)
        sys.exit(1)
    print("all checks passed")
