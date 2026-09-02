#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "meshtastic",
#     "pypubsub",
#     "textual",
# ]
# ///
"""Tests for bot.py: mostly its pure helpers - the per-channel rules.txt
parser, the device-key / host-port parsing behind BLE-vs-TCP connections - plus
one headless run of the App itself, for the unread bold that only shows up once
real widgets are involved.

Run with:
    ./test_rules.py

Deliberately pytest-free so it runs anywhere bot.py itself runs; the shebang
resolves the same three dependencies bot.py declares, which this suite needs
because it imports bot (and meshtastic's protobufs) directly. Importing bot is
safe: the TUI only starts under __main__.
"""

# Deferred annotation evaluation, matching bot.py so this suite imports on
# Python 3.9 too.
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
import types

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import bot  # noqa: E402
import lora_params  # noqa: E402
from meshtastic.protobuf import config_pb2  # noqa: E402

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


def unread_app(target, unread=(), markup=None):
    """A stand-in self for the unread-bold helpers.

    They touch only self.target, self.unread, self._target_markup and
    self._restyle_target, so a namespace avoids a running Textual App. The
    repaint is recorded rather than performed, since the real one walks widgets.
    """
    repainted = []
    app = types.SimpleNamespace(
        target=target,
        unread=set(unread),
        _target_markup=dict(markup or {"channel:0": "# (unnamed #0)", "channel:3": "# EDGE_ATS",
                                      "node:!f2dcbabe": "@ Bug2 [dim]!f2dcbabe[/dim] --"}),
        _target_key=bot.MeshtasticTUI._target_key,
    )
    app._restyle_target = repainted.append
    app._styled_target = lambda key: bot.MeshtasticTUI._styled_target(app, key)
    return app, repainted


def test_unread_bold():
    print("unread targets are bolded in the targets pane")

    check("channel row name", bot.MeshtasticTUI._target_key(("channel", 3)), "channel:3")
    check("node row name", bot.MeshtasticTUI._target_key(("node", "!f2dcbabe")), "node:!f2dcbabe")

    print("a message for a target you are not watching")
    app, repainted = unread_app(target=("channel", 0))
    bot.MeshtasticTUI._mark_unread(app, ("channel", 3))
    check("the target is marked unread", app.unread, {"channel:3"})
    check("its row is repainted", repainted, ["channel:3"])
    check("the watched target is untouched", "channel:0" in app.unread, False)

    print("a message for the target already on screen")
    app, repainted = unread_app(target=("channel", 3))
    bot.MeshtasticTUI._mark_unread(app, ("channel", 3))
    check("it is not marked unread", app.unread, set())
    check("and nothing is repainted", repainted, [])

    print("a DM marks the node row too")
    app, repainted = unread_app(target=("channel", 0))
    bot.MeshtasticTUI._mark_unread(app, ("node", "!f2dcbabe"))
    check("the node is marked unread", app.unread, {"node:!f2dcbabe"})

    print("selecting a bold target clears it")
    app, repainted = unread_app(target=("channel", 0), unread={"channel:3"})
    bot.MeshtasticTUI._clear_unread(app, ("channel", 3))
    check("the mark is gone", app.unread, set())
    check("the row is repainted back to normal", repainted, ["channel:3"])

    print("clearing a target that was never unread does no work")
    app, repainted = unread_app(target=("channel", 0))
    bot.MeshtasticTUI._clear_unread(app, ("channel", 3))
    check("still nothing unread", app.unread, set())
    check("no repaint", repainted, [])

    print("the markup a row is drawn with")
    app, _ = unread_app(target=("channel", 0), unread={"channel:3"})
    check("unread is bolded", app._styled_target("channel:3"), "[bold]# EDGE_ATS[/bold]")
    check("read is left alone", app._styled_target("channel:0"), "# (unnamed #0)")
    # The node id keeps its own dim tag inside the bold - nesting, not replacing.
    app, _ = unread_app(target=("channel", 0), unread={"node:!f2dcbabe"})
    check(
        "a node row keeps its dim id",
        app._styled_target("node:!f2dcbabe"),
        "[bold]@ Bug2 [dim]!f2dcbabe[/dim] --[/bold]",
    )

    print("a node heard for the first time has no row yet")
    # The list is built once, on config sync, so _restyle_target has to cope
    # with a key it has never drawn - without a widget query it cannot serve.
    stub = types.SimpleNamespace(_target_markup={}, unread={"node:!newnode"})
    bot.MeshtasticTUI._restyle_target(stub, "node:!newnode")
    check("repainting an undrawn row is a no-op, not a crash", True, True)
    # And the mark survives, so a rebuild would show it.
    check("the unread mark is still recorded", stub.unread, {"node:!newnode"})


async def _unread_bold_widgets():
    from textual.widgets import Label, ListItem, ListView

    def styles(app, key):
        """The style names a target row actually renders with. Textual resolves
        a Label's markup into a Content whose spans carry them, so this reads
        the rendered result rather than the markup handed in."""
        for item in app.query_one("#target-list", ListView).query(ListItem):
            if item.name == key:
                return sorted({str(s.style) for s in item.query_one(Label).render().spans})
        return None

    app = bot.MeshtasticTUI()
    async with app.run_test() as pilot:
        listview = app.query_one("#target-list", ListView)
        # Build rows the way _populate_targets would, without needing a radio.
        app._add_target(listview, "channel:0", "# (unnamed #0)")
        app._add_target(listview, "channel:3", "# EDGE_ATS")
        app._add_target(listview, "node:!f2dcbabe", "@ Bug2 [dim]!f2dcbabe[/dim] --")
        await pilot.pause()
        app.target = ("channel", 0)

        check("nothing starts bold", styles(app, "channel:3"), [])
        check("a node row starts dim only", styles(app, "node:!f2dcbabe"), ["dim"])

        app._mark_unread(("channel", 3))
        await pilot.pause()
        check("an unwatched channel goes bold", styles(app, "channel:3"), ["bold"])

        app._mark_unread(("channel", 0))
        await pilot.pause()
        check("the watched channel stays normal", styles(app, "channel:0"), [])

        app._mark_unread(("node", "!f2dcbabe"))
        await pilot.pause()
        check(
            "a DM bolds the node and keeps its dim id",
            styles(app, "node:!f2dcbabe"),
            ["bold", "dim"],
        )

        app._on_target_selected("channel:3")
        await pilot.pause()
        check("selecting it clears the bold", styles(app, "channel:3"), [])

        app._on_target_selected("channel:0")
        await pilot.pause()
        check("it stays normal after switching away", styles(app, "channel:3"), [])
        check("a target never visited keeps its bold", styles(app, "node:!f2dcbabe"), ["bold", "dim"])


def test_unread_bold_widgets():
    print("unread bold through real Textual widgets")
    # The only test that runs the App: _restyle_target walks the target list
    # and calls Label.update, which a stand-in self cannot exercise. Headless,
    # so it still needs no hardware.
    asyncio.run(_unread_bold_widgets())


def coverage_report(rules_text: str, channels: set) -> list[str]:
    """Run _report_rule_coverage with a stand-in self, returning what it logged.

    The method only reaches for _log_system and _known_channel_sections, so a
    namespace avoids building a real Textual App - and `channels` stands in for
    what the connected node actually has configured.
    """
    with_rules(rules_text)
    logged: list[str] = []
    app = types.SimpleNamespace(
        _log_system=logged.append,
        _known_channel_sections=lambda: channels,
    )
    bot.MeshtasticTUI._report_rule_coverage(app)
    return logged


def test_rule_coverage_report():
    print("the rule-coverage report logged on connect")
    # What a node with a primary and one named channel reports, mirroring
    # _known_channel_sections: "*" is always in there.
    channels = {bot.ALL_CHANNELS, "#0", "#3", "EDGE_ATS"}

    lines = coverage_report("[EDGE_ATS]\nping=pong\n", channels)
    check("a configured channel is reported", lines, ["自動回覆頻道 [EDGE_ATS]: 1 條規則"])

    lines = coverage_report("[TYPO]\nping=pong\n", channels)
    check("a section matching no channel is flagged", len(lines), 1)
    check("...and says so", "對不上這台的任何頻道" in lines[0], True)

    # The regression: [DM] is chosen by the target being a node, not by
    # matching a channel name, so the "no such channel" check must not claim
    # working DM rules are dead.
    lines = coverage_report("[DM]\nhi=hello there\n", channels)
    check("[DM] is not flagged as an unmatched channel", any("對不上" in ln for ln in lines), False)
    check("[DM] is reported as covering DMs", lines, ["自動回覆私訊: 1 條規則"])

    # DM_SECTION's docstring accepts that a channel really named DM shares the
    # section; when that happens the report should say it covers both.
    lines = coverage_report("[DM]\nhi=hello there\n", channels | {"DM"})
    check("a channel named DM is mentioned too", lines, ["自動回覆私訊 (以及同名的頻道): 1 條規則"])

    lines = coverage_report("[DM]\nhi=hi\n[EDGE_ATS]\nping=pong\n[TYPO]\na=b\n", channels)
    check("all three kinds are reported together", len(lines), 3)
    check("...DM among them", any("私訊" in ln for ln in lines), True)

    lines = coverage_report("hello=hi\n", channels)
    check("a catch-all is warned about", any("所有頻道" in ln for ln in lines), True)

    lines = coverage_report("# nothing but a comment\n", channels)
    check("no rules at all is called out", any("沒有任何規則" in ln for ln in lines), True)


def test_default_rules_template():
    """Check DEFAULT_RULES, the template written when no rules.txt exists.

    Deliberately not the rules.txt sitting next to this file: that one is live
    operator config, so reading it made the suite fail whenever the rules were
    edited - which is the normal thing to do to it.
    """
    print("DEFAULT_RULES template")
    with_rules(bot.DEFAULT_RULES)
    shipped = bot.load_rules()
    check("a [DM] section and a channel one", sorted(shipped), ["DM", "EDGE_ATS"])
    edge = ["EDGE_ATS", "#3", bot.ALL_CHANNELS]
    check("ping", bot.find_reply("ping", edge), "pong")
    # Regression: the reply is taken literally, so a quoted value would leak
    # its quote characters into the outgoing message.
    check("reply text has no stray quotes", bot.find_reply("help", edge), "\u6307\u4ee4: ping")
    # Substring matching used to answer this; exact matching must not.
    check("surrounding words no longer match", bot.find_reply("please ping this", edge), None)
    check("another channel gets nothing", bot.find_reply("ping", ["LongFast", "#0", bot.ALL_CHANNELS]), None)
    # The template shows [DM] answering the same keyword differently.
    check("[DM] answers privately", bot.find_reply("ping", ["DM", "EDGE_ATS", "#3", bot.ALL_CHANNELS]),
          "pong (private)")


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
    check("channel name beats [*]", bot.find_reply("ping", ["EDGE_ATS", "#3", "*"]), "edge-pong")
    check("wrong case does not match", bot.find_reply("PING", ["EDGE_ATS", "#3", "*"]), None)
    check("#index section resolves", bot.find_reply("ping", ["#0", "*"]), "primary-pong")
    check("unknown channel falls back to [*]", bot.find_reply("ping", ["Nope", "#7", "*"]), "global-pong")
    check("[*] key not shadowed still fires", bot.find_reply("hello", ["EDGE_ATS", "#3", "*"]), "hi everyone")
    check("no keyword matches", bot.find_reply("nothing here", ["EDGE_ATS", "*"]), None)


def test_exact_matching():
    print("exact, case-sensitive matching")
    # The real rules file is a NATO alphabet, which is precisely the shape
    # substring matching cannot survive.
    with_rules("[CLSE]\nA=Alpha\nB=Bravo\nZ=Zulu\nping=pong\nhelp=指令: ping\n")
    clse = ["CLSE", "#4", "*"]
    check("exact single letter", bot.find_reply("A", clse), "Alpha")
    check("repeated letter is not the letter", bot.find_reply("AAA", clse), None)
    check("lowercase does not match uppercase rule", bot.find_reply("a", clse), None)
    check("longer word containing it", bot.find_reply("Apple", clse), None)
    # Substring matching answered "Alpha" here, because Bravo contains an "a".
    check("another rule's reply text", bot.find_reply("Bravo", clse), None)
    # And "Echo" for this, because hello contains an "e".
    check("hello no longer draws a letter reply", bot.find_reply("hello", clse), None)
    check("exact word rule", bot.find_reply("ping", clse), "pong")
    check("wrong case on a word rule", bot.find_reply("Ping", clse), None)
    check("substring of a word rule", bot.find_reply("pinging", clse), None)
    check("surrounding whitespace is ignored", bot.find_reply("  A  ", clse), "Alpha")
    check("empty message", bot.find_reply("", clse), None)
    check("whitespace-only message", bot.find_reply("   ", clse), None)
    check("non-ascii reply still exact", bot.find_reply("help", clse), "指令: ping")


def test_should_auto_reply():
    print("one reply per message, and only from others")
    app = types.SimpleNamespace(
        my_id="!me",
        _replied_ids={},
        REPLIED_ID_LIMIT=bot.MeshtasticTUI.REPLIED_ID_LIMIT,
    )
    call = bot.MeshtasticTUI._should_auto_reply

    check("a message from someone else", call(app, {"from_id": "!them", "id": 1, "text": "ping"}), True)
    # The loop guard: a bot reply must never draw another reply.
    reply = bot.build_reply_text("pong", {"when": "1", "transport": "LoRa", "snr": None,
                                          "rssi": None, "from_id": "!them"})
    check("a bot reply", call(app, {"from_id": "!them", "id": 90, "text": reply}), False)
    check("the prefix alone", call(app, {"from_id": "!them", "id": 91, "text": "BOT: pong"}), False)
    check("prefix after whitespace", call(app, {"from_id": "!them", "id": 92, "text": "  BOT: x"}), False)
    check("prefix must match case", call(app, {"from_id": "!them", "id": 93, "text": "bot: x"}), True)
    # The same packet again: the mesh rebroadcasts, and MQTT can bridge it back.
    check("the same packet a second time", call(app, {"from_id": "!them", "id": 1, "text": "ping"}), False)
    check("a different packet", call(app, {"from_id": "!them", "id": 2, "text": "ping"}), True)
    # Our own outgoing text echoes back; answering it makes the bot self-reply.
    check("our own echo", call(app, {"from_id": "!me", "id": 3, "text": "ping"}), False)
    check("our own echo is not remembered", 3 in app._replied_ids, False)
    # No id to key on: replying twice beats ignoring a real message.
    check("packet with no id", call(app, {"from_id": "!them", "id": None, "text": "ping"}), True)
    check("packet with no id again", call(app, {"from_id": "!them", "id": None, "text": "ping"}), True)

    print("reply ledger stays bounded")
    app2 = types.SimpleNamespace(my_id="!me", _replied_ids={}, REPLIED_ID_LIMIT=8)
    for i in range(50):
        call(app2, {"from_id": "!them", "id": i, "text": "ping"})
    check("bounded to the limit", len(app2._replied_ids), 8)
    check("keeps the newest", 49 in app2._replied_ids, True)
    check("drops the oldest", 0 in app2._replied_ids, False)


def test_dm_sections():
    print("[DM] rules, falling back to the channel")
    channels = {0: "", 3: "EDGE_ATS", 4: "CLSE"}
    app = types.SimpleNamespace()
    app._channel_name = lambda i: channels.get(i) or None
    app._channel_sections = lambda i: bot.MeshtasticTUI._channel_sections(app, i)
    call = bot.MeshtasticTUI._reply_sections

    check("a channel message uses its channel", call(app, ("channel", 3), 3),
          ["EDGE_ATS", "#3", "*"])
    # A DM tries [DM] first, then the channel it arrived on, so a rule can be
    # written for private messages or shared with the channel.
    check("a DM tries [DM] then the channel", call(app, ("node", "!x"), 3),
          ["DM", "EDGE_ATS", "#3", "*"])
    check("a DM on the unnamed primary", call(app, ("node", "!x"), 0), ["DM", "#0", "*"])
    # Typed here, there is no packet and so no channel to fall back to.
    check("a DM with no known channel", call(app, ("node", "!x"), None), ["DM", "*"])


def test_dm_replies():
    print("DM replies")
    with_rules("[DM]\nhi=hello there\nping=dm-pong\n\n[EDGE_ATS]\nping=pong\n@A=Alpha\n")

    check("[DM] rule", bot.find_reply("hi", ["DM", "EDGE_ATS", "#3", "*"]), "hello there")
    # [DM] is searched first, so it wins a keyword the channel also defines.
    check("[DM] beats the channel", bot.find_reply("ping", ["DM", "EDGE_ATS", "#3", "*"]), "dm-pong")
    # Not in [DM], so the channel's rules still answer it.
    check("falls back to the channel", bot.find_reply("@A", ["DM", "EDGE_ATS", "#3", "*"]), "Alpha")
    # And the reverse must not happen: a channel message must not pick up [DM].
    check("[DM] does not leak into a channel", bot.find_reply("hi", ["EDGE_ATS", "#3", "*"]), None)
    check("channel keyword on a channel", bot.find_reply("ping", ["EDGE_ATS", "#3", "*"]), "pong")
    # With no channel known, only [DM] and [*] apply.
    check("channel-only keyword without a channel", bot.find_reply("@A", ["DM", "*"]), None)
    check("[DM] keyword without a channel", bot.find_reply("hi", ["DM", "*"]), "hello there")

    print("a DM reply goes back as a DM")
    sent = []
    app = types.SimpleNamespace(
        my_id="!me", here=None, history={}, _replied_ids={},
        REPLIED_ID_LIMIT=bot.MeshtasticTUI.REPLIED_ID_LIMIT,
        interface=types.SimpleNamespace(nodes={}, sendText=lambda t, **k: sent.append((t, k))),
    )
    channels = {3: "EDGE_ATS"}
    app._channel_name = lambda i: channels.get(i) or None
    app._channel_sections = lambda i: bot.MeshtasticTUI._channel_sections(app, i)
    app._reply_sections = lambda t, c: bot.MeshtasticTUI._reply_sections(app, t, c)
    app._distance_to = lambda n: None

    bot.MeshtasticTUI._maybe_auto_reply(
        app, app.interface, ("node", "!them"), "hi", when="12:34:56",
        from_id="!them", transport="LoRa", snr=None, rssi=None, channel=3,
    )
    check("addressed to the sender, not broadcast", [kw for _, kw in sent], [{"destinationId": "!them"}])
    check("carries the [DM] reply", sent[0][0].splitlines()[0], "BOT: hello there")

    sent.clear()
    bot.MeshtasticTUI._maybe_auto_reply(
        app, app.interface, ("channel", 3), "ping", when="12:34:56",
        from_id="!them", transport="LoRa", snr=None, rssi=None, channel=3,
    )
    check("a channel reply goes to the channel", [kw for _, kw in sent], [{"channelIndex": 3}])


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
    # A device path keeps its slashes; the prefix is stripped, nothing else.
    check(
        "explicit serial",
        bot._split_device_key("serial:/dev/cu.usbmodem2101"),
        (bot.TRANSPORT_SERIAL, "/dev/cu.usbmodem2101"),
    )
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


def _lora(**fields):
    """A real LoRaConfig with `fields` set, so the tables meet the actual types."""
    cfg = config_pb2.Config.LoRaConfig()
    for key, value in fields.items():
        setattr(cfg, key, value)
    return cfg


_REGION = config_pb2.Config.LoRaConfig.RegionCode
_PRESET = config_pb2.Config.LoRaConfig.ModemPreset


def test_derived_bandwidth():
    print("derived bandwidth")
    preset = _lora(use_preset=True, modem_preset=_PRESET.Value("MEDIUM_FAST"), region=_REGION.Value("TW"))
    # A real TW/MEDIUM_FAST node stores bandwidth 250 even though this one reports 0.
    check("preset MEDIUM_FAST", lora_params.bandwidth_khz(preset), 250.0)

    slow = _lora(use_preset=True, modem_preset=_PRESET.Value("LONG_SLOW"), region=_REGION.Value("TW"))
    check("preset LONG_SLOW", lora_params.bandwidth_khz(slow), 125.0)

    # 2.4 GHz widens every preset.
    wide = _lora(use_preset=True, modem_preset=_PRESET.Value("MEDIUM_FAST"), region=_REGION.Value("LORA_24"))
    check("wide-lora widens it", lora_params.bandwidth_khz(wide), 812.5)

    custom = _lora(use_preset=False, bandwidth=62, region=_REGION.Value("TW"))
    check("custom config uses the stored value", lora_params.bandwidth_khz(custom), 62.0)

    custom_zero = _lora(use_preset=False, bandwidth=0, region=_REGION.Value("TW"))
    check("custom config with nothing stored", lora_params.bandwidth_khz(custom_zero), None)


def test_derived_frequency():
    print("derived frequency")
    # The reference case: a real GAT562 on TW/MEDIUM_FAST/slot 1 has
    # override_frequency pinned to exactly this.
    tw = _lora(
        use_preset=True, modem_preset=_PRESET.Value("MEDIUM_FAST"), region=_REGION.Value("TW"), channel_num=1
    )
    check("TW MEDIUM_FAST slot 1", round(lora_params.frequency_mhz(tw), 4), 920.125)

    tw_slot3 = _lora(
        use_preset=True, modem_preset=_PRESET.Value("MEDIUM_FAST"), region=_REGION.Value("TW"), channel_num=3
    )
    check("TW slot 3 is two slot widths up", round(lora_params.frequency_mhz(tw_slot3), 4), 920.625)
    check("TW slot count", lora_params.slot_count(tw), 20)

    # EU_866's spacing/padding profile: the firmware documents these four exact
    # channels, so they pin the formula's padding and spacing handling.
    eu866 = [
        round(
            lora_params.frequency_mhz(
                _lora(
                    use_preset=True,
                    modem_preset=_PRESET.Value("LITE_FAST"),
                    region=_REGION.Value("EU_866"),
                    channel_num=slot,
                )
            ),
            4,
        )
        for slot in (1, 2, 3, 4)
    ]
    check("EU_866 documented channels", eu866, [865.7, 866.3, 866.9, 867.5])

    override = _lora(use_preset=True, modem_preset=_PRESET.Value("MEDIUM_FAST"), region=_REGION.Value("TW"),
                     channel_num=5, override_frequency=921.5)
    check("override_frequency wins over the slot", lora_params.frequency_mhz(override), 921.5)

    offset = _lora(use_preset=True, modem_preset=_PRESET.Value("MEDIUM_FAST"), region=_REGION.Value("TW"),
                   channel_num=1, frequency_offset=0.5)
    check("frequency_offset is applied", round(lora_params.frequency_mhz(offset), 4), 920.625)

    # Slot 0 means the firmware hashes the channel name to pick one; that is not
    # reproduced, so this must decline rather than guess.
    auto = _lora(use_preset=True, modem_preset=_PRESET.Value("MEDIUM_FAST"), region=_REGION.Value("TW"), channel_num=0)
    check("auto slot cannot be derived", lora_params.frequency_mhz(auto), None)

    # EU_N_868 forces slot 1, so it resolves even with channel_num unset.
    forced = _lora(use_preset=True, modem_preset=_PRESET.Value("NARROW_SLOW"), region=_REGION.Value("EU_N_868"),
                   channel_num=0)
    check("region override slot resolves", lora_params.frequency_mhz(forced) is not None, True)


def test_reply_text():
    print("auto-reply text")
    base = {"when": "12:34:56", "transport": "LoRa", "snr": 6.5, "rssi": -92,
            "from_id": "!abc", "from_name": "Bug2"}
    check(
        "two lines, details bracketed",
        bot.build_reply_text("pong", {**base, "distance_m": 842.4}),
        "BOT: pong\n[12:34:56 from=Bug2 rx=LoRa snr=6.5 rssi=-92 dist=842m]",
    )
    check(
        "no distance field when unknown",
        bot.build_reply_text("pong", base),
        "BOT: pong\n[12:34:56 from=Bug2 rx=LoRa snr=6.5 rssi=-92]",
    )
    check(
        "kilometres for a far node",
        bot.build_reply_text("pong", {**base, "distance_m": 5029.0}),
        "BOT: pong\n[12:34:56 from=Bug2 rx=LoRa snr=6.5 rssi=-92 dist=5.0km]",
    )
    # An explicit None must behave like an absent key, not print "None".
    check(
        "explicit None is omitted",
        bot.build_reply_text("pong", {**base, "distance_m": None}),
        "BOT: pong\n[12:34:56 from=Bug2 rx=LoRa snr=6.5 rssi=-92]",
    )
    # from falls back to the id for a node whose name has not arrived.
    check(
        "unnamed sender falls back to the id",
        bot.build_reply_text("pong", {"when": "12:34:56", "transport": "MQTT",
                                      "snr": None, "rssi": None, "from_id": "!a08b0694"}),
        "BOT: pong\n[12:34:56 from=!a08b0694 rx=MQTT]",
    )
    check("exactly two lines", len(bot.build_reply_text("pong", base).splitlines()), 2)
    check("first line is the rule reply", bot.build_reply_text("pong", base).splitlines()[0], "BOT: pong")

    print("a reply cannot trigger another reply")
    with_rules("[EDGE_ATS]\nping=pong\npong=ping\nA=Alpha\n")
    edge = ["EDGE_ATS", "#3", "*"]
    for rule_reply in ("pong", "ping", "Alpha"):
        text = bot.build_reply_text(rule_reply, base)
        # Two independent guards: the text matches no rule, and it is prefixed.
        check(f"{rule_reply!r} reply matches no rule", bot.find_reply(text, edge), None)
        check(f"{rule_reply!r} reply is prefixed", text.startswith(bot.BOT_REPLY_PREFIX), True)
    # Even a rules file that names the prefix itself cannot start a loop, since
    # _should_auto_reply refuses the prefix before any matching happens.
    check("prefix constant", bot.BOT_REPLY_PREFIX, "BOT: ")


def test_distance_to():
    print("_distance_to")
    here = (25.0339, 121.5645)
    there = {"position": {"latitudeI": 250478000, "longitudeI": 1215170000}}
    stub = types.SimpleNamespace(
        my_id="!me",
        here=here,
        interface=types.SimpleNamespace(nodes={"!me": {}, "!far": there, "!nopos": {}}),
    )
    call = bot.MeshtasticTUI._distance_to
    check("known sender", round(call(stub, "!far")), 5029)
    check("sender without a position", call(stub, "!nopos"), None)
    check("unknown sender", call(stub, "!missing"), None)
    check("our own id is skipped", call(stub, "!me"), None)
    check("no sender", call(stub, None), None)

    # No reference position at either end -> nothing to compute from.
    no_here = types.SimpleNamespace(
        my_id="!me", here=None, interface=types.SimpleNamespace(nodes={"!me": {}, "!far": there})
    )
    check("no reference position", call(no_here, "!far"), None)

    check("no interface", call(types.SimpleNamespace(my_id="!me", here=here, interface=None), "!far"), None)


def test_node_label():
    print("node display names")
    nodes = {
        "!short": {"user": {"shortName": "Bug2", "longName": "BUG2 long"}},
        "!longonly": {"user": {"longName": "Solar Repeater"}},
        "!blank": {"user": {"shortName": "", "longName": ""}},
        "!nouser": {},
        "!nulluser": {"user": None},
    }
    check("short name preferred", bot.node_label(nodes, "!short"), "Bug2")
    check("falls back to long name", bot.node_label(nodes, "!longonly"), "Solar Repeater")
    # Names arrive as separate NodeInfo packets, so a node can be heard from
    # before it is named. Showing the id beats showing nothing.
    check("empty names fall back to the id", bot.node_label(nodes, "!blank"), "!blank")
    check("entry without a user", bot.node_label(nodes, "!nouser"), "!nouser")
    check("entry with a null user", bot.node_label(nodes, "!nulluser"), "!nulluser")
    check("node not in the db", bot.node_label(nodes, "!unknown"), "!unknown")
    check("empty node db", bot.node_label({}, "!x"), "!x")


def test_incoming_line_sender():
    print("message line shows the sender name")
    info = {
        "when": "12:34:56",
        "from_id": "!f2dcbabe",
        "transport": "LoRa",
        "snr": 6.5,
        "rssi": -92,
        "text": "ping",
    }
    check(
        "name then bracketed id",
        bot.format_incoming_line(info, "Bug2"),
        "[dim]12:34:56[/dim] [bold]Bug2[/bold][dim]\\[!f2dcbabe][/dim](LoRa snr=6.5 rssi=-92): ping",
    )
    # The bracket must be escaped or RichLog parses "[!f2dcbabe]" as a style tag.
    check("opening bracket is escaped", "\\[!f2dcbabe]" in bot.format_incoming_line(info, "Bug2"), True)
    # No name resolved, so node_label already fell back to the id - printing it
    # again would read "!f2dcbabe !f2dcbabe".
    check(
        "unnamed sender shows the id once",
        bot.format_incoming_line(info),
        "[dim]12:34:56[/dim] [bold]!f2dcbabe[/bold](LoRa snr=6.5 rssi=-92): ping",
    )
    check(
        "sender equal to the id is not doubled",
        bot.format_incoming_line(info, "!f2dcbabe"),
        "[dim]12:34:56[/dim] [bold]!f2dcbabe[/bold](LoRa snr=6.5 rssi=-92): ping",
    )
    quiet = {**info, "snr": None, "rssi": None}
    check(
        "no signal figures",
        bot.format_incoming_line(quiet, "Bug2"),
        "[dim]12:34:56[/dim] [bold]Bug2[/bold][dim]\\[!f2dcbabe][/dim](LoRa): ping",
    )


def test_node_position():
    print("node position extraction")
    check("no position key", bot.node_position({}), None)
    # What a node with GPS on but no fix actually reports - seen on real hardware.
    check("timestamp only means no fix", bot.node_position({"position": {"time": 1788193370}}), None)
    check("exact 0,0 is a placeholder", bot.node_position({"position": {"latitudeI": 0, "longitudeI": 0}}), None)
    check(
        "real fix is scaled by 1e7",
        bot.node_position({"position": {"latitudeI": 250339000, "longitudeI": 1215645000}}),
        (25.0339, 121.5645),
    )


def test_distance():
    print("distance")
    # One degree of latitude on the mean-radius sphere.
    check("1 degree of latitude", round(bot.haversine_m(0, 0, 1, 0)), 111195)
    check("same point is zero", bot.haversine_m(25.0, 121.0, 25.0, 121.0), 0.0)
    check("symmetric", round(bot.haversine_m(25, 121, 24, 120), 3), round(bot.haversine_m(24, 120, 25, 121), 3))

    print("--here parsing")
    check("plain pair", bot.parse_latlon("25.0339,121.5645"), (25.0339, 121.5645))
    check("negative and spaced", bot.parse_latlon(" -33.9, 18.4 "), (-33.9, 18.4))
    for bad in ("25.0339", "25,121,3", "a,b", "91,0", "0,181"):
        try:
            bot.parse_latlon(bad)
            check(f"rejects {bad!r}", "accepted", "rejected")
        except Exception:
            check(f"rejects {bad!r}", "rejected", "rejected")

    print("distance formatting")
    check("unknown", bot.format_distance(None), "--")
    check("metres below 1 km", bot.format_distance(842.4), "842m")
    check("kilometres", bot.format_distance(5432.0), "5.4km")
    check("exactly 1 km", bot.format_distance(1000.0), "1.0km")


def test_annotations_are_deferred():
    """Guard the Python 3.9 fix.

    3.9 parses `str | None` but cannot evaluate it, so the modules only import
    there while `from __future__ import annotations` keeps every annotation a
    string. Asserting that here fails on any version the moment someone drops
    the future import, instead of waiting for a 3.9 machine to hit it.
    """
    print("annotations are deferred (Python 3.9 compatibility)")
    checked = 0
    for module in (bot, lora_params):
        for name in dir(module):
            obj = getattr(module, name)
            anns = getattr(obj, "__annotations__", None)
            if not isinstance(anns, dict) or not callable(obj):
                continue
            for field, value in anns.items():
                checked += 1
                if not isinstance(value, str):
                    check(f"{module.__name__}.{name}:{field} is a string", type(value).__name__, "str")
    check("every annotation is an unevaluated string", True, True)
    print(f"       ({checked} annotations checked across bot + lora_params)")


if __name__ == "__main__":
    original = bot.RULES_FILE
    try:
        test_default_rules_template()
        test_unread_bold()
        test_unread_bold_widgets()
        test_rule_coverage_report()
        test_sections_and_precedence()
        test_exact_matching()
        test_should_auto_reply()
        test_dm_sections()
        test_dm_replies()
        test_legacy_flat_file()
        test_header_edge_cases()
        test_empty_section_is_kept()
        test_split_device_key()
        test_split_host_port()
        test_describe_peer()
        test_display_width()
        test_annotations_are_deferred()
        test_derived_bandwidth()
        test_derived_frequency()
        test_node_label()
        test_incoming_line_sender()
        test_node_position()
        test_distance()
        test_distance_to()
        test_reply_text()
    finally:
        bot.RULES_FILE = original

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for failure in _failures:
            print(" -", failure)
        sys.exit(1)
    print("all checks passed")
