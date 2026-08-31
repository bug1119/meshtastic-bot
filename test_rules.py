#!/usr/bin/env python3
"""Tests for bot.py's pure helpers: the per-channel rules.txt parser and the
device-key / host-port parsing behind BLE-vs-TCP connections.

Run with:
    python3 test_rules.py

Deliberately dependency-free (no pytest) so it runs anywhere bot.py itself
runs. Importing bot is safe: the TUI only starts under __main__.
"""

import pathlib
import sys
import tempfile
import types

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


def test_split_device_key():
    print("device-list key parsing")
    check("explicit ble", bot._split_device_key("ble:BUG1"), (bot.TRANSPORT_BLE, "BUG1"))
    check("explicit tcp", bot._split_device_key("tcp:Meshtastic.local"), (bot.TRANSPORT_TCP, "Meshtastic.local"))
    # A bare address predates the prefix scheme; treat it as BLE rather than
    # silently failing to connect.
    check("bare address means ble", bot._split_device_key("BUG1"), (bot.TRANSPORT_BLE, "BUG1"))
    # BLE names can themselves be MAC addresses, so an unknown prefix must not
    # be swallowed as a transport.
    check("mac-like name kept whole", bot._split_device_key("AA:BB:CC:DD:EE:FF"),
          (bot.TRANSPORT_BLE, "AA:BB:CC:DD:EE:FF"))
    check("prefixed mac keeps its colons", bot._split_device_key("ble:AA:BB:CC:DD:EE:FF"),
          (bot.TRANSPORT_BLE, "AA:BB:CC:DD:EE:FF"))


def test_split_host_port():
    print("host:port parsing")
    check("bare host uses default port", bot._split_host_port("Meshtastic.local"),
          ("Meshtastic.local", bot.DEFAULT_TCP_PORT))
    check("explicit default port", bot._split_host_port("192.168.0.247:4403"), ("192.168.0.247", 4403))
    check("custom port", bot._split_host_port("192.168.0.247:9999"), ("192.168.0.247", 9999))
    # IPv6 literals carry several colons - splitting them would corrupt the host.
    check("ipv6 left alone", bot._split_host_port("fe80::1"), ("fe80::1", bot.DEFAULT_TCP_PORT))
    check("non-numeric port left alone", bot._split_host_port("host:nope"),
          ("host:nope", bot.DEFAULT_TCP_PORT))


class _FakeSocket:
    def __init__(self, peer):
        self._peer = peer

    def getpeername(self):
        if self._peer is None:
            raise OSError("not connected")
        return self._peer


def _describe(iface, transport, tcp_host="Meshtastic.local"):
    """Call _describe_peer with a stand-in self.

    The method only reads self.tcp_host, so a namespace avoids constructing a
    real Textual App just to test address formatting.
    """
    return bot.MeshtasticTUI._describe_peer(
        types.SimpleNamespace(tcp_host=tcp_host), iface, transport
    )


def test_describe_peer():
    print("connection address shown in the status pane")
    tcp = types.SimpleNamespace(socket=_FakeSocket(("192.168.0.247", 4403)), hostname="Meshtastic.local")
    check("tcp shows the resolved ip", _describe(tcp, bot.TRANSPORT_TCP), "192.168.0.247")

    # A hostname was given but the socket knows the real address - report the IP.
    odd_port = types.SimpleNamespace(socket=_FakeSocket(("192.168.0.247", 9999)), hostname="h")
    check("non-default port is appended", _describe(odd_port, bot.TRANSPORT_TCP), "192.168.0.247:9999")

    # IPv6 peers come back as 4-tuples; only host and port should be used.
    v6 = types.SimpleNamespace(socket=_FakeSocket(("fe80::1", 4403, 0, 0)), hostname="h")
    check("ipv6 peer", _describe(v6, bot.TRANSPORT_TCP), "fe80::1")

    dead = types.SimpleNamespace(socket=_FakeSocket(None), hostname="Meshtastic.local")
    check("dead socket falls back to hostname", _describe(dead, bot.TRANSPORT_TCP), "Meshtastic.local")

    bare = types.SimpleNamespace(socket=None, hostname=None)
    check("no socket or hostname falls back to --host", _describe(bare, bot.TRANSPORT_TCP), "Meshtastic.local")

    ble = types.SimpleNamespace(client=types.SimpleNamespace(address="AA:BB:CC:DD:EE:FF"))
    check("ble reads client.address", _describe(ble, bot.TRANSPORT_BLE), "AA:BB:CC:DD:EE:FF")

    check("ble without a client", _describe(types.SimpleNamespace(client=None), bot.TRANSPORT_BLE), None)


def test_display_width():
    print("display width (CJK counts double)")
    check("ascii", bot.display_width("Region: TW"), 10)
    check("cjk label", bot.display_width("頻率: 依 Region/Slot"), 20)
    # The wording this replaced was 25 wide and wrapped in a 24-column pane.
    check("the old wrapping string", bot.display_width("頻率: 依 Region/Slot 自動"), 25)
    check("fits the pane", bot.display_width("頻率: 依 Region/Slot") <= bot.STATUS_PANE_WIDTH, True)

    # The 連線 line: short IPs fit on one row, long ones must not.
    check("short ip fits", bot.display_width("連線: TCP 192.168.0.247") <= bot.STATUS_PANE_WIDTH, True)
    check("long ip does not fit", bot.display_width("連線: TCP 192.168.100.247") <= bot.STATUS_PANE_WIDTH, False)
    check("hostname does not fit", bot.display_width("連線: TCP Meshtastic.local") <= bot.STATUS_PANE_WIDTH, False)
    check("ble mac does not fit", bot.display_width("連線: BLE AA:BB:CC:DD:EE:FF") <= bot.STATUS_PANE_WIDTH, False)


if __name__ == "__main__":
    original = bot.RULES_FILE
    try:
        test_shipped_rules()
        test_sections_and_precedence()
        test_legacy_flat_file()
        test_header_edge_cases()
        test_empty_section_is_kept()
        test_split_device_key()
        test_split_host_port()
        test_describe_peer()
        test_display_width()
    finally:
        bot.RULES_FILE = original

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for failure in _failures:
            print(" -", failure)
        sys.exit(1)
    print("all checks passed")
