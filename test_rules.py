#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "meshtastic",
#     "paho-mqtt",
#     "pypubsub",
#     "textual",
# ]
# ///
"""Tests for bot_dual.py and bot_server.py: mostly their pure helpers - the per-channel rules.txt
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
import builtins
import contextlib
import datetime as _dt
import inspect
import io
import pathlib
import subprocess
import sys
import tempfile
import time
import threading
import types

sys.path.insert(0, str(pathlib.Path(__file__).parent))

# bot_dual.py is the module under test: bot.py is kept frozen as the original
# monitor, and bot_server.py is generated from bot_dual by stripping the UI.
# Imported as `bot` so every existing check reads unchanged.
import bot_dual as bot  # noqa: E402
import bot_server  # noqa: E402
import lora_params  # noqa: E402
from meshtastic.protobuf import config_pb2, mesh_pb2  # noqa: E402
from pubsub import pub  # noqa: E402

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


def stub_interface(channel_names, node_ids):
    """Enough of an interface for _populate_targets: channels and a node db."""
    channels = [
        types.SimpleNamespace(index=i, settings=types.SimpleNamespace(name=name))
        for i, name in enumerate(channel_names)
    ]
    return types.SimpleNamespace(
        localNode=types.SimpleNamespace(channels=channels),
        nodes={node_id: {} for node_id in node_ids},
    )


async def _repopulate_count():
    from textual.widgets import ListItem, ListView

    app = bot.MeshtasticTUI()
    async with app.run_test() as pilot:
        app.my_id = "!me"
        app.interface = stub_interface(["", "EDGE_ATS"], ["!aaa", "!bbb", "!me"])
        logged = []
        app._log_system = logged.append

        def loaded():
            return [ln for ln in logged if ln.startswith("載入 ")]

        app._populate_targets()
        await pilot.pause()
        rows = lambda: len(app.query_one("#target-list", ListView).query(ListItem))  # noqa: E731
        check("first sync counts the rows it added", loaded()[-1], "載入 4 個頻道/node")
        check("and that is what the list holds", rows(), 4)

        # A reconnect repopulates. clear() queues the old rows for removal but
        # leaves them in .children until the loop catches up, so counting the
        # widget here would report old + new.
        logged.clear()
        app._populate_targets()
        await pilot.pause()
        check("a repopulate does not double the count", loaded()[-1], "載入 4 個頻道/node")
        check("and does not duplicate the rows", rows(), 4)


def test_repopulate_count():
    print("repopulating the target list after a reconnect")
    asyncio.run(_repopulate_count())


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


def bar_app(**over):
    """A stand-in self for _status_bar_text: it reads only these figures."""
    import time as _time

    state = dict(
        started_at=_time.monotonic(),
        packet_count=0,
        received_count=0,
        sent_typed_count=0,
        sent_auto_count=0,
        link_down=False,
        reconnect_attempt=0,
        reconnect_total=0,
    )
    state.update(over)
    return types.SimpleNamespace(**state)


def bar_text(**over):
    return bot.MeshtasticTUI._status_bar_text(bar_app(**over))


def test_status_bar():
    print("run-time formatting")
    check("zero", bot.format_elapsed(0), "0:00:00")
    check("seconds", bot.format_elapsed(9), "0:00:09")
    check("a minute", bot.format_elapsed(61), "0:01:01")
    check("an hour", bot.format_elapsed(3661), "1:01:01")
    # Hours are not wrapped into days: two days and change reads as 51:03:12,
    # which compares against a log timestamp without arithmetic.
    check("past a day", bot.format_elapsed(183792), "51:03:12")
    check("fractional seconds truncate", bot.format_elapsed(59.9), "0:00:59")
    check("negative is clamped", bot.format_elapsed(-5), "0:00:00")
    # Distinct from format_uptime, which reports the node's uptime: minute
    # precision, and "--" when the device has not told us. Naming this one
    # format_uptime too silently shadowed it - both call sites got the wrong one.
    check("the node's formatter is untouched", bot.format_uptime(None), "--")
    check("...still minute precision", bot.format_uptime(3661), "01:01")
    check("...still days for long spans", bot.format_uptime(183792), "2d 03:03")

    print("the status bar line")
    # Uptime itself is checked above; here the figures matter, so match on the
    # parts that do not move.
    text = bar_text()
    check("starts at zero packets", "封包[/bold] 0" in text, True)
    check("starts at zero received", "收[/bold] 0" in text, True)
    check("starts at zero sent", "發[/bold] 0 ([dim]自動 0[/dim])" in text, True)
    check("no reconnect segment before any outage", "重連" in text, False)

    text = bar_text(packet_count=1284, received_count=42, sent_typed_count=2, sent_auto_count=5)
    # Packets dwarf messages - most traffic is position/nodeinfo/telemetry.
    check("packet count shown", "封包[/bold] 1284" in text, True)
    check("received count shown", "收[/bold] 42" in text, True)
    # 7 is the total, of which 5 were the bot - not 5 on top of 7.
    check("sent is the total, auto in brackets", "發[/bold] 7 ([dim]自動 5[/dim])" in text, True)

    print("while the link is down")
    text = bar_text(link_down=True, reconnect_attempt=3, reconnect_total=8)
    check("shows this outage's attempt, in red", "[red]重連中 第 3 次[/red]" in text, True)
    check("not the session total", "重連 8 次" in text, False)

    print("after it recovers")
    text = bar_text(link_down=False, reconnect_attempt=3, reconnect_total=8)
    check("shows the session total, dimmed", "[dim]重連 8 次[/dim]" in text, True)
    check("no longer says 重連中", "重連中" in text, False)


def test_packet_count():
    print("every packet counts, not just the text ones")

    def receiving_app():
        """A stand-in self for on_receive, enough for the counting paths."""
        app = types.SimpleNamespace(
            packet_count=0, received_count=0, last_signal=None,
            my_id="!me", history={}, target=None,
        )
        app._track_signal = lambda pkt: bot.MeshtasticTUI._track_signal(app, pkt)
        app._mark_unread = lambda target: None
        app._log_system = lambda line: None
        # on_receive hands its UI work off; running it is not what is under test.
        app.call_from_thread = lambda fn, *a: None
        app._should_auto_reply = lambda info: False
        return app

    # A position packet carries no text, so parse_incoming returns None and
    # on_receive stops early - but the packet still arrived.
    app = receiving_app()
    bot.MeshtasticTUI.on_receive(
        app, {"decoded": {"portnum": "POSITION_APP"}, "fromId": "!them"}, None
    )
    check("a position packet is counted", app.packet_count, 1)
    check("but is not a received message", app.received_count, 0)

    # Nothing decoded at all - encrypted for a channel this node has no key
    # for. Still traffic, still proof the radio is hearing something.
    bot.MeshtasticTUI.on_receive(app, {"fromId": "!them"}, None)
    check("an undecoded packet is counted", app.packet_count, 2)

    # A text message is both.
    app = receiving_app()
    bot.MeshtasticTUI.on_receive(
        app,
        {
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi"},
            "fromId": "!them", "toId": bot.BROADCAST_ADDR, "id": 1,
        },
        types.SimpleNamespace(nodes={}),
    )
    check("a text packet counts as a packet", app.packet_count, 1)
    check("...and as a received message", app.received_count, 1)

    # Our own echo is a packet, but not something we received from the mesh.
    app = receiving_app()
    bot.MeshtasticTUI.on_receive(
        app,
        {
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi"},
            "fromId": "!me", "toId": bot.BROADCAST_ADDR, "id": 2,
        },
        types.SimpleNamespace(nodes={}),
    )
    check("our own echo is a packet", app.packet_count, 1)
    check("...but not a received message", app.received_count, 0)


async def _status_bar_widget():
    from textual.widgets import Label

    app = bot.MeshtasticTUI()
    async with app.run_test() as pilot:
        bar = app.query_one("#status-bar", Label)
        await pilot.pause()
        check("the bar is one line tall", bar.size.height, 1)
        check("it starts filled in", "執行" in bar.render().plain, True)

        app.packet_count = 1284
        app.received_count = 12
        app.sent_typed_count = 1
        app.sent_auto_count = 2
        app._render_status_bar()
        await pilot.pause()
        plain = bar.render().plain
        check("the packet count reaches the widget", "封包 1284" in plain, True)
        check("counts reach the widget", "收 12" in plain, True)
        check("sent total reaches the widget", "發 3 (自動 2)" in plain, True)

        app.link_down = True
        app.reconnect_attempt = 4
        app._render_status_bar()
        await pilot.pause()
        check("the outage shows on the bar", "重連中 第 4 次" in bar.render().plain, True)


def test_status_bar_widget():
    print("the status bar as a real widget")
    asyncio.run(_status_bar_widget())


def link_app(interface="iface", closing=False, link_down=False):
    """A stand-in self for the link-loss handlers.

    They read self.interface / self._closing / self.link_down and hand the UI
    work off, so a namespace is enough. call_from_thread records the callback
    it was given instead of running it, which is how the "did it schedule the
    UI handler?" checks stay synchronous.
    """
    app = types.SimpleNamespace(
        interface=interface,
        _closing=closing,
        link_down=link_down,
        connected_key="tcp:192.168.0.247",
        scheduled=[],
        logged=[],
        rendered=0,
        reconnects=0,
    )
    app.call_from_thread = lambda fn, *a: app.scheduled.append(getattr(fn, "__name__", str(fn)))
    app._log_system = app.logged.append
    app._render_local_status = lambda: setattr(app, "rendered", app.rendered + 1)
    app.reconnect_loop = lambda: setattr(app, "reconnects", app.reconnects + 1)

    # Named rather than a lambda so call_from_thread records "_link_lost".
    def _link_lost():
        bot.MeshtasticTUI._link_lost(app)

    app._link_lost = _link_lost
    return app


def test_connection_lost():
    print("a dropped link is noticed, reported and retried")

    app = link_app()
    bot.MeshtasticTUI.on_connection_lost(app, "iface")
    check("a loss on the current interface is handled", app.scheduled, ["_link_lost"])

    # The library calls _disconnected() for any interface, including one we
    # have already moved off - that is not our current link going down.
    app = link_app()
    bot.MeshtasticTUI.on_connection_lost(app, "some-old-interface")
    check("a loss on a stale interface is ignored", app.scheduled, [])

    # on_unmount closes the interface deliberately, which also fires the event.
    app = link_app(closing=True)
    bot.MeshtasticTUI.on_connection_lost(app, "iface")
    check("our own shutdown is not treated as a loss", app.scheduled, [])

    print("what the UI handler does")
    app = link_app()
    bot.MeshtasticTUI._link_lost(app)
    check("the link is marked down", app.link_down, True)
    check("it says so, in red", any("連線中斷" in ln and "red" in ln for ln in app.logged), True)
    check("the status pane is redrawn", app.rendered, 1)
    check("a reconnect is started", app.reconnects, 1)

    # A second event for the same outage must not start a second loop.
    app = link_app(link_down=True)
    bot.MeshtasticTUI._link_lost(app)
    check("an already-down link starts nothing more", app.reconnects, 0)
    check("and logs nothing more", app.logged, [])

    print("reconnect backoff")
    delays = [bot.MeshtasticTUI._reconnect_delay(n) for n in range(1, 8)]
    check("quick at first, then a steady poll", delays, [1, 2, 5, 10, 30, 30, 30])
    check("attempt 0 is treated as the first", bot.MeshtasticTUI._reconnect_delay(0), 1)


def test_default_rules_template():
    """Check DEFAULT_RULES, the template written when no rules.txt exists.

    Deliberately not the rules.txt sitting next to this file: that one is live
    operator config, so reading it made the suite fail whenever the rules were
    edited - which is the normal thing to do to it.
    """
    print("DEFAULT_RULES template")
    with_rules(bot.DEFAULT_RULES)
    shipped = bot.load_rules()
    check(
        "a [DM] section, a channel one, and the exclusions",
        sorted(shipped),
        ["!exclude", "DM", "EDGE_ATS"],
    )
    check(
        "the public channels are excluded out of the box",
        sorted(shipped["!exclude"]),
        ["Emergency!", "LongFast", "MediumFast", "MeshTW", "SignalTest"],
    )
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
        MARKUP=bot.MeshtasticTUI.MARKUP,
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
        MARKUP=bot.MeshtasticTUI.MARKUP,
        # Tallied for the status bar every time a reply goes out.
        sent_auto_count=0,
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
    # Both replies above went out, so the status bar's auto tally saw both.
    check("both replies reached the status bar tally", app.sent_auto_count, 2)


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


def _text_packet(text, to_id, pkt_id, from_num=0xF2DCBABE, from_id="!them", channel=3):
    """One TEXT_MESSAGE_APP packet shaped the way the library delivers them."""
    return {
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text},
        "from": from_num,
        "fromId": from_id,
        "toId": to_id,
        "channel": channel,
        "id": pkt_id,
        "rxSnr": 6.5,
        "rxRssi": -92,
    }


def test_packet_node_id():
    print("packet endpoint ids survive an unsynced node database")
    call = bot.packet_node_id

    check(
        "a resolved id is used as-is",
        call({"fromId": "!f2dcbabe", "from": 0xF2DCBABE}, "fromId", "from"),
        "!f2dcbabe",
    )
    # The case this exists for: the library sets the key to None when the node
    # number is not in its database yet, which is the normal state for the first
    # messages after connecting. A plain .get() with a default never fires,
    # because the key is present - it just holds None.
    check(
        "None falls back to the node number",
        call({"fromId": None, "from": 0xF2DCBABE}, "fromId", "from"),
        "!f2dcbabe",
    )
    check(
        "a missing key falls back too",
        call({"from": 0x1D7E2212}, "fromId", "from"),
        "!1d7e2212",
    )
    # Eight hex digits always, so a low node number is not silently shortened
    # into something that no longer matches the id the node reports for itself.
    check("padded to eight hex digits", call({"from": 0xABCDEF}, "fromId", "from"), "!00abcdef")
    check("nothing to work from", call({}, "fromId", "from"), None)
    check("a zero node number is not an id", call({"from": 0}, "fromId", "from"), None)
    # toId goes through the same helper, and broadcast keeps its symbolic form
    # rather than being rewritten as "!ffffffff".
    check(
        "broadcast toId is left alone",
        call({"toId": bot.BROADCAST_ADDR, "to": 0xFFFFFFFF}, "toId", "to"),
        bot.BROADCAST_ADDR,
    )

    print("parse_incoming shows a real id, never a placeholder")
    unresolved = _text_packet("ping", bot.BROADCAST_ADDR, 1, from_num=0x1EF840CA, from_id=None)
    info = bot.parse_incoming(unresolved, "!me")
    check("from_id rebuilt from the node number", info["from_id"], "!1ef840ca")
    check("target is the channel it arrived on", info["target"], ("channel", 3))
    # And a packet with no usable sender at all still parses rather than raising.
    bare = {"decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi"}, "toId": bot.BROADCAST_ADDR}
    check("no sender information at all", bot.parse_incoming(bare, "!me")["from_id"], "?")


def test_format_plain():
    print("plain rendering, for a stream that is not a RichLog")
    info = {
        "from_id": "!f2dcbabe",
        "when": "12:34:56",
        "transport": "LoRa",
        "snr": 6.5,
        "rssi": -92,
        "text": "ping",
    }
    check(
        "markup, for the TUI",
        bot.format_incoming_line(info, "Bug2"),
        "[dim]12:34:56[/dim] [bold]Bug2[/bold][dim]\\[!f2dcbabe][/dim](LoRa snr=6.5 rssi=-92): ping",
    )
    check(
        "plain, for the server",
        bot.format_incoming_line(info, "Bug2", markup=False),
        "12:34:56 Bug2[!f2dcbabe](LoRa snr=6.5 rssi=-92): ping",
    )
    # No name resolved, so the id is not printed twice.
    check(
        "plain, unnamed sender",
        bot.format_incoming_line(info, "!f2dcbabe", markup=False),
        "12:34:56 !f2dcbabe(LoRa snr=6.5 rssi=-92): ping",
    )


def _fake_server(rules, out):
    """A ServerBot wired to a stub interface, with `sent` collecting sendText."""
    with_rules(rules)
    sent = []
    server = bot.ServerBot(out=out)
    server.my_id = "!me"
    channels = [
        types.SimpleNamespace(index=0, settings=types.SimpleNamespace(name="")),
        types.SimpleNamespace(index=3, settings=types.SimpleNamespace(name="EDGE_ATS")),
    ]
    # Real protobuf messages rather than stand-ins: every field then exists
    # with the firmware's own default, and lora_params runs its actual
    # derivation instead of reading back whatever a namespace happened to hold.
    cfg = bot.config_pb2.Config
    lora = cfg.LoRaConfig(
        region=cfg.LoRaConfig.RegionCode.Value("TW"),
        modem_preset=cfg.LoRaConfig.ModemPreset.Value("MEDIUM_FAST"),
        use_preset=True,
        tx_power=30,
        config_ok_to_mqtt=True,
    )
    server.transport = "ble"
    server.peer = "Bug2_1ca6"
    server.interface = types.SimpleNamespace(
        nodes={
            "!them": {"user": {"shortName": "Bug2"}},
            "!me": {
                "deviceMetrics": {
                    "uptimeSeconds": 9723,
                    "batteryLevel": 94,
                    "voltage": 4.012,
                    "channelUtilization": 3.2,
                }
            },
        },
        localNode=types.SimpleNamespace(
            channels=channels,
            localConfig=types.SimpleNamespace(
                lora=lora,
                device=cfg.DeviceConfig(role=cfg.DeviceConfig.Role.Value("CLIENT")),
                position=cfg.PositionConfig(
                    gps_mode=cfg.PositionConfig.GpsMode.Value("NOT_PRESENT")
                ),
            ),
        ),
        # The node sends this during the config download, so the server has it
        # without asking - see on_config_synced.
        metadata=types.SimpleNamespace(firmware_version="2.7.11.4e40e6f"),
        sendText=lambda text, **kw: sent.append((text, kw)),
        getMyUser=lambda: {"id": "!me", "longName": "BUG1119", "shortName": "BUG1"},
    )
    return server, sent


def test_server_bot_replies():
    print("server mode answers the same rules as the UI")
    out = io.StringIO()
    server, sent = _fake_server("[DM]\nhi=dm-hello\n\n[EDGE_ATS]\nping=pong\n", out)

    server.on_receive(_text_packet("ping", bot.BROADCAST_ADDR, 101), server.interface)
    check("a channel rule replies to the channel", [kw for _, kw in sent], [{"channelIndex": 3}])
    check("the reply text", sent[0][0].splitlines()[0], "BOT: pong")
    check("counted as received", server.received_count, 1)
    check("counted as an auto-reply", server.sent_auto_count, 1)

    sent.clear()
    server.on_receive(_text_packet("hi", "!me", 102), server.interface)
    check("a DM rule replies as a DM", [kw for _, kw in sent], [{"destinationId": "!them"}])
    check("the DM reply text", sent[0][0].splitlines()[0], "BOT: dm-hello")

    # The gates are inherited, so this checks they are actually reached.
    sent.clear()
    server.on_receive(_text_packet("ping", bot.BROADCAST_ADDR, 101), server.interface)
    check("the same packet id is not answered twice", sent, [])
    server.on_receive(_text_packet("BOT: pong", bot.BROADCAST_ADDR, 103), server.interface)
    check("a bot reply is never answered", sent, [])
    server.on_receive(
        _text_packet("ping", bot.BROADCAST_ADDR, 104, from_id="!me"), server.interface
    )
    check("our own echo is not answered", sent, [])

    log = out.getvalue()
    print("the log is plain text, not markup")
    for tag in ("[bold]", "[dim]", "[/dim]", "[yellow]", "[/yellow]"):
        check(f"no {tag}", tag in log, False)
    check("records the incoming message", "Bug2[!them](LoRa snr=6.5 rssi=-92): ping" in log, True)
    check("records the reply it sent", "-> auto-reply: BOT: pong" in log, True)
    # The reply's own bracketed detail line must survive verbatim, since it is
    # what was actually transmitted.
    check("keeps the reply's detail brackets", "[12:" in log or "from=Bug2" in log, True)
    check("one reply folded onto one line", "auto-reply: BOT: pong [" in log, True)


def test_server_bot_config_sync():
    print("server mode reports what it is working with on connect")
    out = io.StringIO()
    server, _ = _fake_server("[EDGE_ATS]\nping=pong\n\n[NOPE]\nx=y\n", out)
    server.on_config_synced(server.interface)
    log = out.getvalue()
    check("names the node", "BUG1119 !me" in log, True)
    check("lists the channels", "#3 EDGE_ATS" in log, True)
    check("counts the rules", "[EDGE_ATS]=1" in log, True)
    # A section matching no channel here can never fire; saying so beats leaving
    # a typo'd channel name silently dead.
    check("warns about an unmatched section", "NOPE" in log, True)


def test_server_heartbeat_and_stop():
    print("server heartbeat and shutdown")
    out = io.StringIO()
    server, _ = _fake_server("[*]\nping=pong\n", out)
    server.received_count = 7
    server.sent_auto_count = 3
    server.reconnect_total = 2
    line = server._heartbeat_line()
    for expected in ("已連線", "收訊 7", "自動回覆 3", "重連 2"):
        check(f"heartbeat mentions {expected}", expected in line, True)
    server.link_down = True
    check("heartbeat says so when the link is down", "連線中斷" in server._heartbeat_line(), True)
    # stop() has to be safe from a signal handler, so it only sets flags.
    server.stop()
    check("stop marks closing", server._closing, True)
    check("stop releases the wait", server._stopped.is_set(), True)


def test_bounded_history():
    print("the server's reply history cannot grow without bound")
    history = bot._BoundedHistory()
    limit = bot._BoundedHistory.LIMIT
    for i in range(limit + 20):
        history.setdefault(("channel", 3), []).append(i)
    check("bounded per target", len(history[("channel", 3)]), limit)
    check("keeps the newest", history[("channel", 3)][-1], limit + 19)
    check("drops the oldest", 0 in history[("channel", 3)], False)
    # Separate targets do not share the bound.
    history.setdefault(("node", "!x"), []).append("only")
    check("a second target is independent", list(history[("node", "!x")]), ["only"])


def test_resolve_server_target():
    print("server mode device selection")
    call = bot.resolve_server_target
    # One named target is taken as the answer. This is what makes --daemon
    # possible: no terminal is needed to pick.
    check(
        "a single --host",
        call("192.168.0.247", None, None),
        (bot.TRANSPORT_TCP, "192.168.0.247"),
    )
    check(
        "a single --port",
        call(None, "/dev/cu.usbmodem2101", None),
        (bot.TRANSPORT_SERIAL, "/dev/cu.usbmodem2101"),
    )
    check(
        "a single --ble",
        call(None, None, "Meshtastic_1a2b"),
        (bot.TRANSPORT_BLE, "Meshtastic_1a2b"),
    )

    # More than one candidate means asking. Both the scan and input are stubbed:
    # the point under test is the choosing, not bluetooth.
    scanned = [types.SimpleNamespace(name="Meshtastic_aabb")]
    original_scan = bot.meshtastic.ble_interface.BLEInterface.scan
    original_input = builtins.input
    bot.meshtastic.ble_interface.BLEInterface.scan = staticmethod(lambda: scanned)
    try:
        answers = iter(["9", "not a number", "3"])
        builtins.input = lambda _: next(answers)
        got = call("192.168.0.247", "/dev/tty0", None)
        check(
            "rejects bad answers, then takes the third row",
            got,
            (bot.TRANSPORT_BLE, "Meshtastic_aabb"),
        )

        # Ctrl-D at the prompt is an answer too: give up rather than loop.
        def raise_eof(_):
            raise EOFError

        builtins.input = raise_eof
        check("EOF at the prompt gives up", call("h1", "/dev/tty0", None), None)

        # Nothing named and nothing found - say so instead of prompting for a
        # choice out of an empty list.
        scanned.clear()
        check("no devices at all", call(None, None, None), None)

        # A failing scan must not take the process down with it.
        def boom():
            raise RuntimeError("bluetooth off")

        bot.meshtastic.ble_interface.BLEInterface.scan = staticmethod(boom)
        check("a failed scan still returns the named target", call(None, None, None), None)
    finally:
        bot.meshtastic.ble_interface.BLEInterface.scan = original_scan
        builtins.input = original_input


def test_server_shares_the_engine():
    print("both front ends really do share one rules engine")
    # Not a behavioural check but a structural one: if the server ever stops
    # inheriting ReplyEngine, every rule test above would still pass while the
    # two modes silently drifted apart.
    for name in (
        "_reply_sections",
        "_channel_sections",
        "_channel_name",
        "_should_auto_reply",
        "_maybe_auto_reply",
        "_distance_to",
        "_describe_peer",
        "_reconnect_delay",
    ):
        # Compare the underlying functions: a classmethod fetched from two
        # classes gives two bound objects even when the code behind them is one.
        def impl(cls):
            attr = getattr(cls, name)
            return getattr(attr, "__func__", attr)

        check(f"{name} is one implementation", impl(bot.ServerBot) is impl(bot.MeshtasticTUI), True)
    check("the TUI renders markup", bot.MeshtasticTUI.MARKUP, True)
    check("the server does not", bot.ServerBot.MARKUP, False)


def test_detached_argv():
    print("the background server is relaunched, not forked")
    call = bot.detached_argv

    argv = call((bot.TRANSPORT_SERIAL, "/dev/cu.usbmodem2101"), None, 600)
    check("runs this script", argv[1].endswith("bot_dual.py"), True)
    check("stays headless", "--server" in argv, True)
    check("carries the chosen device", argv[argv.index("--port") + 1], "/dev/cu.usbmodem2101")
    check("carries the heartbeat", argv[argv.index("--heartbeat") + 1], "600")
    # The property that matters most: a child that daemonised again would spawn
    # a child of its own, and so on without end.
    check("never forwards --daemon", "--daemon" in argv, False)
    # And nothing that would make it stop and prompt.
    check("no --wifi", "--wifi" in argv, False)
    check("--here omitted when unset", "--here" in argv, False)

    check(
        "tcp uses --host",
        call((bot.TRANSPORT_TCP, "192.168.0.247"), None, 0)[3:5],
        ["--host", "192.168.0.247"],
    )
    check(
        "ble uses --ble",
        call((bot.TRANSPORT_BLE, "Meshtastic_1a2b"), None, 0)[3:5],
        ["--ble", "Meshtastic_1a2b"],
    )
    # --here has to survive, or a detached server silently loses the distances.
    with_here = call((bot.TRANSPORT_TCP, "h"), (25.033, 121.5654), 300)
    check("passes --here through", with_here[with_here.index("--here") + 1], "25.033,121.5654")
    check(
        "and it parses back",
        bot.parse_latlon(with_here[with_here.index("--here") + 1]),
        (25.033, 121.5654),
    )

    print("the child argv is one this parser accepts")
    # Round-trip it: a flag renamed on one side and not the other would leave
    # --daemon spawning a child that dies on startup, in the background, unseen.
    argv = call((bot.TRANSPORT_SERIAL, "/dev/tty0"), (1.0, 2.0), 42)
    proc = subprocess.run(
        [sys.executable, argv[1], "--help"], capture_output=True, text=True, timeout=120
    )
    check("--help works", proc.returncode, 0)
    for flag in ("--server", "--port", "--heartbeat", "--here"):
        check(f"{flag} is a real option", flag in proc.stdout, True)


def test_bot_server_is_generated_not_rewritten():
    print("bot_server.py is bot_dual.py with the UI removed")
    # Compared as source text, which is the only check that actually catches
    # drift: two copies that merely behave alike today will not stay alike.
    shared_functions = [
        "load_rules",
        "find_reply",
        "parse_incoming",
        "packet_node_id",
        "node_label",
        "format_incoming_line",
        "build_reply_text",
        "node_position",
        "haversine_m",
        "format_distance",
        "format_elapsed",
        "parse_latlon",
        "_split_device_key",
        "_split_host_port",
        "open_interface",
        "list_devices",
        "resolve_server_target",
        "detached_argv",
        "spawn_detached",
    ]
    drifted = []
    for name in shared_functions:
        if inspect.getsource(getattr(bot, name)) != inspect.getsource(
            getattr(bot_server, name)
        ):
            drifted.append(name)
    check("every shared function is identical", drifted, [])

    drifted_classes = []
    for name in ("ReplyEngine", "ServerBot", "_BoundedHistory"):
        if inspect.getsource(getattr(bot, name)) != inspect.getsource(
            getattr(bot_server, name)
        ):
            drifted_classes.append(name)
    check("every shared class is identical", drifted_classes, [])

    print("and the differences are the intended ones")
    check("bot_server carries no UI class", hasattr(bot_server, "MeshtasticTUI"), False)
    check("bot_dual does", hasattr(bot, "MeshtasticTUI"), True)
    # The one behavioural difference: what the detached child is told.
    check("bot_dual tells its child to stay headless", bot.HEADLESS_FLAGS, ["--server"])
    check("bot_server needs no such flag", bot_server.HEADLESS_FLAGS, [])
    check(
        "so bot_server's child argv has no --server",
        "--server" in bot_server.detached_argv((bot_server.TRANSPORT_TCP, "h"), None, 60),
        False,
    )
    check(
        "and it launches bot_server.py",
        bot_server.detached_argv((bot_server.TRANSPORT_TCP, "h"), None, 60)[1].endswith(
            "bot_server.py"
        ),
        True,
    )
    # Not needing a UI toolkit is the point of the file existing. Checked on the
    # source, not sys.modules: this suite imports bot_dual, which does load it.
    # lora_params is deliberately not in this list - it is a module in this
    # repo rather than something to install, and the server derives the node's
    # frequency and bandwidth through it now that the status block is shared.
    ui_imports = [
        line
        for line in inspect.getsource(bot_server).splitlines()
        if line.startswith(("import textual", "from textual"))
    ]
    check("bot_server imports no UI toolkit", ui_imports, [])


def test_bot_server_replies():
    print("bot_server answers on its own, not only through bot_dual")
    path = pathlib.Path(tempfile.mkdtemp()) / "rules.txt"
    path.write_text("[EDGE_ATS]\nping=pong\n", encoding="utf-8")
    original = bot_server.RULES_FILE
    bot_server.RULES_FILE = path
    try:
        out = io.StringIO()
        sent = []
        server = bot_server.ServerBot(out=out)
        server.my_id = "!me"
        channels = [
            types.SimpleNamespace(index=3, settings=types.SimpleNamespace(name="EDGE_ATS"))
        ]
        server.interface = types.SimpleNamespace(
            nodes={"!them": {"user": {"shortName": "Bug2"}}},
            localNode=types.SimpleNamespace(channels=channels),
            sendText=lambda text, **kw: sent.append((text, kw)),
            getMyUser=lambda: {"id": "!me", "longName": "BUG1119"},
        )
        server.on_receive(
            _text_packet("ping", bot_server.BROADCAST_ADDR, 501), server.interface
        )
        check("replies to the channel", [kw for _, kw in sent], [{"channelIndex": 3}])
        check("with the rule's text", sent[0][0].splitlines()[0], "BOT: pong")
        check("logging plain text", "[bold]" in out.getvalue(), False)
        # An unresolved sender must render as a real id here too.
        sent.clear()
        server.on_receive(
            _text_packet(
                "ping", bot_server.BROADCAST_ADDR, 502, from_num=0x1EF840CA, from_id=None
            ),
            server.interface,
        )
        check("unresolved sender shows its node number", "!1ef840ca" in out.getvalue(), True)
        check("and never the word None", "None" in out.getvalue(), False)
    finally:
        bot_server.RULES_FILE = original


def test_config_sync_before_adopt():
    print("config sync can arrive before run() has stored the interface")
    # This is the real ordering, not a hypothetical: the library publishes
    # connection.established from its own thread while open_interface() is still
    # constructing, so on_config_synced ran with self.interface still None and
    # _known_channel_sections raised AttributeError - killing the handler, and
    # with it the rule-coverage warning, on every single startup.
    out = io.StringIO()
    server, _ = _fake_server("[EDGE_ATS]\nping=pong\n\n[TYPO]\nx=y\n", out)
    interface = server.interface
    server.interface = None  # exactly the state the race leaves behind

    server.on_config_synced(interface)

    check("adopts the interface that published", server.interface is interface, True)
    check("still reports the node", "BUG1119" in out.getvalue(), True)
    # The warning is what the crash used to eat.
    check("still warns about the unmatched section", "TYPO" in out.getvalue(), True)
    check("no traceback text in the log", "Traceback" in out.getvalue(), False)


def test_shutdown_is_bounded():
    print("shutdown does not wait forever on a hanging close()")
    out = io.StringIO()
    server, _ = _fake_server("[*]\nping=pong\n", out)
    server.CLOSE_TIMEOUT = 1  # keep the test quick; the shape is what matters

    closing = threading.Event()

    class HangingInterface:
        """close() that never returns - measured behaviour of BLE teardown."""

        def close(self):
            closing.set()
            threading.Event().wait()  # forever

    started = time.monotonic()
    server._shutdown(HangingInterface())
    took = time.monotonic() - started

    check("close() was actually attempted", closing.is_set(), True)
    check("gave up rather than hanging", took < 5, True)
    check("waited about the deadline", 0.9 <= took < 3, True)
    check("said so in the log", "介面關閉逾時" in out.getvalue(), True)
    check("marked itself closing", server._closing, True)

    # A close that raises must not stop the shutdown either.
    out2 = io.StringIO()
    server2, _ = _fake_server("[*]\nping=pong\n", out2)

    class BrokenInterface:
        def close(self):
            raise RuntimeError("already gone")

    server2._shutdown(BrokenInterface())
    check("a raising close is swallowed", "already gone" in out2.getvalue(), True)
    check("and shutdown still completed", server2._closing, True)

    # A well-behaved close returns at once and logs nothing extra.
    out3 = io.StringIO()
    server3, _ = _fake_server("[*]\nping=pong\n", out3)
    closed = []

    class GoodInterface:
        def close(self):
            closed.append(True)

    started = time.monotonic()
    server3._shutdown(GoodInterface())
    check("a clean close is immediate", time.monotonic() - started < 1, True)
    check("and it did close", closed, [True])
    check("with no timeout message", "逾時" in out3.getvalue(), False)


def test_stop_wakes_the_wait():
    print("stop() releases the serve loop")
    # Verified separately against a live signal: Event.set() from a same-thread
    # signal handler does wake Event.wait(). This pins the contract stop()
    # relies on, so a future rewrite that swaps the Event for a sleep fails here
    # rather than in the field.
    out = io.StringIO()
    server, _ = _fake_server("[*]\nping=pong\n", out)
    check("not stopped to begin with", server._stopped.is_set(), False)

    def stopper():
        time.sleep(0.2)
        server.stop()

    threading.Thread(target=stopper, daemon=True).start()
    started = time.monotonic()
    woke = server._stopped.wait(10)
    check("the wait returned early", woke, True)
    check("and quickly", time.monotonic() - started < 3, True)
    check("closing is set", server._closing, True)


def test_list_devices():
    print("--list prints what could be connected to")
    original_scan = bot.meshtastic.ble_interface.BLEInterface.scan
    original_ports = bot.meshtastic.util.findPorts
    try:
        bot.meshtastic.ble_interface.BLEInterface.scan = staticmethod(
            lambda: [
                types.SimpleNamespace(name="Bug2_1ca6", address="AA:BB:CC:DD:EE:FF"),
                types.SimpleNamespace(name="Meshtastic_9f9c", address=None),
            ]
        )
        bot.meshtastic.util.findPorts = lambda *a, **k: ["/dev/cu.usbmodem2101"]

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = bot.list_devices()
        out = buffer.getvalue()

        check("exits 0", code, 0)
        check("counts the BLE nodes", "BLE 節點 (2):" in out, True)
        # Printed as the flag you would pass, so a line is copy-pasteable.
        check("names are ready to paste", "  --ble Bug2_1ca6    AA:BB:CC:DD:EE:FF" in out, True)
        check("an address-less device still lists", "  --ble Meshtastic_9f9c\n" in out, True)
        check("lists serial ports too", "  --port /dev/cu.usbmodem2101" in out, True)
        check("counts them", "USB serial (1):" in out, True)
        # It connects to nothing, so nothing about a node should appear.
        check("does not claim to connect", "已連線" in out, False)

        print("and says so when there is nothing")
        bot.meshtastic.ble_interface.BLEInterface.scan = staticmethod(lambda: [])
        bot.meshtastic.util.findPorts = lambda *a, **k: []
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = bot.list_devices()
        out = buffer.getvalue()
        check("still exits 0 - an empty list is not an error", code, 0)
        check("explains the empty BLE list", "沒有節點在廣播" in out, True)
        check("explains the empty port list", "沒有接上的裝置" in out, True)

        print("a failed scan is reported, not raised")
        def boom():
            raise RuntimeError("bluetooth off")

        bot.meshtastic.ble_interface.BLEInterface.scan = staticmethod(boom)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = bot.list_devices()
        check("exits non-zero", code, 1)
        # The message goes to stderr, so stdout stays parseable by a script.
        check("nothing misleading on stdout", buffer.getvalue().strip(), "")
    finally:
        bot.meshtastic.ble_interface.BLEInterface.scan = original_scan
        bot.meshtastic.util.findPorts = original_ports

    print("bot_server has it too, from the same source")
    check(
        "one implementation",
        inspect.getsource(bot.list_devices) == inspect.getsource(bot_server.list_devices),
        True,
    )


def test_server_packet_count():
    print("the server counts every packet too, and says so in the heartbeat")
    out = io.StringIO()
    server, sent = _fake_server("[EDGE_ATS]\nping=pong\n", out)

    # A position packet: not a message, but very much traffic. This is the whole
    # point of the count - text is rare, this is not.
    position = {
        "decoded": {"portnum": "POSITION_APP", "position": {"latitudeI": 250000000}},
        "from": 0xF2DCBABE,
        "fromId": "!them",
        "toId": bot.BROADCAST_ADDR,
        "channel": 1,
        "id": 601,
    }
    server.on_receive(position, server.interface)
    check("a position packet is counted", server.packet_count, 1)
    check("but is not a received message", server.received_count, 0)
    check("and nothing was replied to", sent, [])

    # An undecodable packet - a channel this node has no key for - still arrived.
    server.on_receive({"from": 0x1EF840CA, "toId": bot.BROADCAST_ADDR, "id": 602}, server.interface)
    check("an undecoded packet is counted", server.packet_count, 2)

    server.on_receive(_text_packet("ping", bot.BROADCAST_ADDR, 603), server.interface)
    check("a text packet counts as a packet", server.packet_count, 3)
    check("...and as a received message", server.received_count, 1)
    check("...and got its reply", len(sent), 1)

    # Our own echo is traffic on the link, but not something we received.
    server.on_receive(
        _text_packet("hello", bot.BROADCAST_ADDR, 604, from_id="!me"), server.interface
    )
    check("our own echo is a packet", server.packet_count, 4)
    check("...but not a received message", server.received_count, 1)

    line = server._heartbeat_line()
    check("the heartbeat carries the packet count", "封包 4" in line, True)
    check("and still the message counts", "收訊 1" in line, True)
    # Order matters only in that packets come first, being the coarse figure.
    check("packets before 收訊", line.index("封包") < line.index("收訊"), True)


def test_packet_count_in_all_three_files():
    print("all three entry points count packets, not just the one that added it")
    # bot.py is frozen by policy and no longer imported here, so the only thing
    # holding it in step is a check on its source. Without this, updating the
    # status bar in one file and not the other is invisible until someone
    # notices a missing column.
    import pathlib as _pathlib

    root = _pathlib.Path(bot.__file__).parent
    for name, expect_bar in (
        ("bot.py", True),
        ("bot_dual.py", True),
        ("bot_server.py", False),  # no status bar; it has the heartbeat instead
    ):
        text = (root / name).read_text(encoding="utf-8")
        check(f"{name} has the counter", "self.packet_count = 0" in text, True)
        check(f"{name} increments it", "self.packet_count += 1" in text, True)
        if expect_bar:
            check(f"{name} shows it in the status bar", "[bold]封包[/bold]" in text, True)
    for name in ("bot_dual.py", "bot_server.py"):
        text = (root / name).read_text(encoding="utf-8")
        check(f"{name} shows it in the heartbeat", "f\" 封包 {self.packet_count}\"" in text, True)


async def _device_rows_widgets():
    from textual.widgets import Label, ListItem, ListView

    def rows(app):
        """(row name, rendered text) for every device row, in order."""
        return [
            (item.name, str(item.query_one(Label).render()))
            for item in app.query_one("#device-list", ListView).query(ListItem)
        ]

    # on_mount starts a scan and, with targets named on the command line, a
    # connection - neither of which may touch real hardware here.
    original_open = bot.open_interface
    original_scan = bot.meshtastic.ble_interface.BLEInterface.scan

    def no_hardware(*_args, **_kwargs):
        raise RuntimeError("no hardware in tests")

    bot.open_interface = no_hardware
    bot.meshtastic.ble_interface.BLEInterface.scan = staticmethod(lambda: [])
    try:
        app = bot.MeshtasticTUI(
            tcp_host="192.168.0.247",
            serial_port="/dev/cu.usbmodem2101",
            ble_address="Bug2_1ca6",
        )
        async with app.run_test() as pilot:
            # Reaching here at all is the regression: DEVICE_MARKS had no entry
            # for "ble", so a --ble target raised KeyError('ble') inside
            # _rebuild_device_list while the first frame was being drawn, and
            # the app exited before showing anything.
            await pilot.pause()
            check("the app started with a --ble target", app.is_running, True)

            app._rebuild_device_list(
                [
                    types.SimpleNamespace(name="Bug2_1ca6"),
                    types.SimpleNamespace(name="bug_530c"),
                ]
            )
            await pilot.pause()
            listed = rows(app)

            check(
                "one row per distinct device",
                [name for name, _ in listed],
                [
                    "tcp:192.168.0.247",
                    "serial:/dev/cu.usbmodem2101",
                    "ble:Bug2_1ca6",
                    "ble:bug_530c",
                ],
            )
            text = dict(listed)
            check("tcp is marked", text["tcp:192.168.0.247"].startswith("◆ "), True)
            # Serial shows the basename: the full path overflows a 28-column pane.
            check("serial shows the basename", text["serial:/dev/cu.usbmodem2101"], "▣ cu.usbmodem2101  SERIAL")
            check("the --ble target is marked like any BLE row", text["ble:Bug2_1ca6"], "● Bug2_1ca6  BLE")
            # The scan finds the named node again; it must not be offered twice.
            check(
                "the named node is listed once",
                [name for name, _ in listed].count("ble:Bug2_1ca6"),
                1,
            )
            check("other scanned nodes still appear", text["ble:bug_530c"], "● bug_530c  BLE")
    finally:
        bot.open_interface = original_open
        bot.meshtastic.ble_interface.BLEInterface.scan = original_scan


def test_device_rows():
    print("the device pane lists every transport, including --ble")
    asyncio.run(_device_rows_widgets())

    # The table itself, so a transport added later without a mark fails here
    # rather than at the first frame of a real run.
    marks = bot.MeshtasticTUI.DEVICE_MARKS
    for transport in (bot.TRANSPORT_TCP, bot.TRANSPORT_SERIAL, bot.TRANSPORT_BLE):
        check(f"{transport} has a mark", transport in marks, True)


def _channel_app(channels):
    """A stand-in self for the section-resolution helpers."""
    app = types.SimpleNamespace()
    app._channel_name = lambda i: channels.get(i) or None
    app._channel_sections = lambda i: bot.MeshtasticTUI._channel_sections(app, i)
    app._reply_sections = lambda t, c: bot.MeshtasticTUI._reply_sections(app, t, c)
    return app


def test_exclude_parsing():
    print("[!exclude] is a list of channels, not keyword=reply pairs")
    with_rules(
        "[!exclude]\n"
        "SignalTest\n"
        "Emergency!\n"
        "#0\n"
        "# 這行是註解,不是頻道\n"
        "\n"
        "[EDGE_ATS]\n"
        "ping=pong\n"
    )
    check(
        "bare lines become the channel list",
        sorted(bot.excluded_channels()),
        ["#0", "Emergency!", "SignalTest"],
    )
    # A comment inside the section must stay a comment. Only a bare "#<digits>"
    # is an index - otherwise "# 這行是註解" would be read as a channel.
    check(
        "a real comment is not a channel",
        any("註解" in c for c in bot.excluded_channels()),
        False,
    )
    check("the other sections are untouched", bot.load_rules()["EDGE_ATS"], {"ping": "pong"})
    # It must never be reachable as a rules section, or a message reading
    # "SignalTest" would be answered with an empty string.
    check("it holds no answerable rules", bot.find_reply("SignalTest", ["!exclude"]), None)

    print("indexes are normalised, so #00 and #0 are one channel")
    with_rules("[!exclude]\n#00\n")
    check("padded index", sorted(bot.excluded_channels()), ["#0"])

    print("no section, no exclusions")
    with_rules("[EDGE_ATS]\nping=pong\n")
    check("nothing excluded", bot.excluded_channels(), set())
    check("is_excluded says no", bot.is_excluded("EDGE_ATS", 3), False)


def test_is_excluded():
    print("a channel matches by name or by index")
    with_rules("[!exclude]\nSignalTest\n#0\n")
    check("by name", bot.is_excluded("SignalTest", 1), True)
    check("by index", bot.is_excluded(None, 0), True)
    # Either form is enough: #1 is SignalTest, named in the list.
    check("named channel found by its name alone", bot.is_excluded("SignalTest", 99), True)
    check("an unlisted channel", bot.is_excluded("EDGE_ATS", 3), False)
    check("an unlisted index", bot.is_excluded(None, 4), False)
    # Exact matching, like every other section name in this file.
    check("case matters", bot.is_excluded("signaltest", 1), False)
    check("no name and no index", bot.is_excluded(None, None), False)


def test_exclude_drops_star():
    print("[*] does not apply to an excluded channel")
    with_rules("[!exclude]\nSignalTest\n#0\n\n[*]\nping=pong\n")
    channels = {0: "", 1: "SignalTest", 3: "EDGE_ATS"}
    app = _channel_app(channels)

    check("an excluded named channel", app._channel_sections(1), ["SignalTest", "#1"])
    # The primary is normally unnamed, so its index is the only way to name it -
    # and the primary is usually the public channel most worth excluding.
    check("an excluded unnamed primary", app._channel_sections(0), ["#0"])
    check("an ordinary channel keeps [*]", app._channel_sections(3), ["EDGE_ATS", "#3", "*"])

    print("so a [*] rule fires only where it is allowed to")
    check("not on the excluded channel", bot.find_reply("ping", app._channel_sections(1)), None)
    check("not on the excluded primary", bot.find_reply("ping", app._channel_sections(0)), None)
    check("but yes elsewhere", bot.find_reply("ping", app._channel_sections(3)), "pong")

    print("an explicit rule for an excluded channel still works")
    # Excluding a channel silences the blanket default there, not the channel.
    with_rules("[!exclude]\nSignalTest\n\n[SignalTest]\nstatus=ok\n\n[*]\nping=pong\n")
    sections = app._channel_sections(1)
    check("its own rule answers", bot.find_reply("status", sections), "ok")
    check("the blanket rule still does not", bot.find_reply("ping", sections), None)


def test_exclude_leaves_dms_alone():
    print("[!exclude] is about broadcast traffic, so DMs are unaffected")
    with_rules("[!exclude]\nSignalTest\n#0\n\n[DM]\nhi=private hello\n\n[*]\nping=pong\n")
    channels = {0: "", 1: "SignalTest", 3: "EDGE_ATS"}
    app = _channel_app(channels)

    # A DM is addressed to us personally; that it happened to travel on an
    # excluded channel says nothing about whether we should answer it.
    check(
        "a DM on an excluded channel keeps [*]",
        app._reply_sections(("node", "!them"), 1),
        ["DM", "SignalTest", "#1", "*"],
    )
    check(
        "and on the excluded primary",
        app._reply_sections(("node", "!them"), 0),
        ["DM", "#0", "*"],
    )
    check(
        "an ordinary channel is unchanged",
        app._reply_sections(("node", "!them"), 3),
        ["DM", "EDGE_ATS", "#3", "*"],
    )
    check(
        "a DM with no known channel",
        app._reply_sections(("node", "!them"), None),
        ["DM", "*"],
    )
    # And the broadcast side of the same channel is still silenced.
    check(
        "the channel itself stays excluded",
        app._reply_sections(("channel", 1), 1),
        ["SignalTest", "#1"],
    )

    print("end to end")
    check("[*] answers a DM on an excluded channel",
          bot.find_reply("ping", app._reply_sections(("node", "!them"), 1)), "pong")
    check("but not a broadcast on it",
          bot.find_reply("ping", app._reply_sections(("channel", 1), 1)), None)
    check("[DM] still wins where it matches",
          bot.find_reply("hi", app._reply_sections(("node", "!them"), 1)), "private hello")


def test_exclude_coverage_report():
    print("what the coverage report says about exclusions")
    known = {bot.ALL_CHANNELS, "#0", "#1", "SignalTest", "#3", "EDGE_ATS"}

    logged = coverage_report(
        "[!exclude]\nSignalTest\n\n[*]\nping=pong\n\n[EDGE_ATS]\n@A=Alpha\n", known
    )
    blob = "\n".join(logged)
    check("names the excluded channels", "[*] 不適用於這些頻道: SignalTest" in blob, True)
    # The [*] warning changes tone: with exclusions in place it is a statement
    # of fact, not a caution about answering the whole mesh.
    check("[*] is reported as narrowed", "但已排除 1 個: SignalTest" in blob, True)
    check("no scolding about public channels", "包含公共頻道" in blob, False)
    check("[!exclude] is not reported as a dead section", "對不上這台的任何頻道,該區規則" in blob, False)

    print("an exclusion matching no channel is called out")
    # This is the failure that looks exactly like success: the entry is there,
    # it reads fine, and it excludes nothing at all.
    logged = coverage_report("[!exclude]\nLongFast\nSignalTest\n\n[*]\nping=pong\n", known)
    blob = "\n".join(logged)
    check("warns about the one that matches nothing", "LongFast" in blob and "沒有排除到任何東西" in blob, True)
    check("and does not warn about the one that matches", "SignalTest,？" in blob, False)

    print("without [!exclude], the old warning stands")
    logged = coverage_report("[*]\nping=pong\n", known)
    check("still cautions about public channels", "包含公共頻道" in "\n".join(logged), True)


def test_exclude_shipped_defaults():
    print("the section ships in rules.txt and in the built-in template")
    import pathlib as _pathlib

    root = _pathlib.Path(bot.__file__).parent
    wanted = ["SignalTest", "Emergency!", "LongFast", "MediumFast", "MeshTW"]

    # DEFAULT_RULES, not the rules.txt next to this file: that one is live
    # operator config, and this feature's whole point is that lines get deleted
    # from it. Asserting on it would fail the suite for doing the intended thing.
    check("the template has the section", "[!exclude]" in bot.DEFAULT_RULES, True)
    for channel in wanted:
        check(f"the template excludes {channel}", f"\n{channel}\n" in bot.DEFAULT_RULES, True)
    # And that the template it writes actually parses back to those channels,
    # rather than merely containing the words.
    with_rules(bot.DEFAULT_RULES)
    check("and they parse back", sorted(bot.excluded_channels()), sorted(wanted))
    check("and documents it", "channels [*] must NOT fire on" in bot.DEFAULT_RULES, True)

    # All three programs read the same rules.txt, so one of them answering on a
    # channel the others stay quiet on would be the worst of both.
    for name in ("bot.py", "bot_dual.py", "bot_server.py"):
        text = (root / name).read_text(encoding="utf-8")
        check(f"{name} knows the section", 'EXCLUDE_SECTION = "!exclude"' in text, True)
        check(f"{name} applies it", "if not is_excluded(name, index):" in text, True)


# The rules.txt shown in README.md under "自動回覆規則", verbatim minus the
# trailing comments. Kept here so the worked table beside it in the README is
# checked rather than asserted - a documented example that quietly stops being
# true is worse than no example.
README_EXAMPLE = """\
[!exclude]
#0
SignalTest

[DM]
ping=pong (私訊)

[EDGE_ATS]
ping=pong
help=指令: ping

[#0]
status=ok

[*]
hello=hi
"""


def test_readme_worked_example():
    print("the README's worked example answers what the README says it does")
    with_rules(README_EXAMPLE)
    app = _channel_app({0: "", 1: "SignalTest", 3: "EDGE_ATS"})

    def answer(kind, index, text):
        target = ("channel", index) if kind == "channel" else ("node", "!them")
        return bot.find_reply(text, app._reply_sections(target, index))

    # Every row of the table in README.md, in the same order.
    rows = [
        ("#0 廣播 hello", ("channel", 0, "hello"), None),
        ("#0 廣播 status", ("channel", 0, "status"), "ok"),
        ("SignalTest 廣播 hello", ("channel", 1, "hello"), None),
        ("EDGE_ATS 廣播 hello", ("channel", 3, "hello"), "hi"),
        ("EDGE_ATS 廣播 ping", ("channel", 3, "ping"), "pong"),
        ("EDGE_ATS 廣播 help", ("channel", 3, "help"), "指令: ping"),
        ("走 #0 的 DM hello", ("dm", 0, "hello"), "hi"),
        ("走 #0 的 DM ping", ("dm", 0, "ping"), "pong (私訊)"),
        ("走 EDGE_ATS 的 DM ping", ("dm", 3, "ping"), "pong (私訊)"),
    ]
    for label, (kind, index, text), expected in rows:
        check(label, answer(kind, index, text), expected)

    # The row most likely to be misread, called out in the README as well:
    # excluding a channel silences the blanket rule there, not the channel.
    check(
        "excluding a channel does not silence its own rules",
        answer("channel", 0, "status") is not None and answer("channel", 0, "hello") is None,
        True,
    )

    print("and the README really does contain that example")
    import pathlib as _pathlib

    readme = (_pathlib.Path(bot.__file__).parent / "README.md").read_text(encoding="utf-8")
    # Compared line by line, ignoring the trailing "# ..." comments the README
    # adds for readability - those are prose, the rules are what must match.
    for line in README_EXAMPLE.splitlines():
        if not line.strip():
            continue
        check(f"README shows {line!r}", any(l.split("#")[0].strip() == line or l.startswith(line) for l in readme.splitlines()), True)


def test_when_falls_back_to_our_clock():
    print("a node with no clock no longer renders as ??:??:??")
    base = {
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "Q"},
        "from": 0xF2DCBABE,
        "fromId": "!f2dcbabe",
        "toId": bot.BROADCAST_ADDR,
        "channel": 3,
        "id": 1,
    }

    # rxTime is the node's own clock. One that has never had a GPS fix or a
    # phone connected reports 0, which is the normal state for a bench node - so
    # this was every message, not an edge case.
    for label, packet in (
        ("absent", dict(base)),
        ("zero", dict(base, rxTime=0)),
    ):
        when = bot.parse_incoming(packet, "!me")["when"]
        check(f"rxTime {label}: marked as derived", when.startswith("~"), True)
        check(f"rxTime {label}: is a clock time", len(when), 9)
        check(f"rxTime {label}: no question marks", "?" in when, False)
        # Within a minute of now - the packet is parsed as it arrives, which is
        # what makes our clock an honest stand-in for the node's.
        now = _dt.datetime.now().strftime("%H:%M")
        check(f"rxTime {label}: close to now", when[1:6] == now, True)

    print("and a node that does report one is believed")
    stamped = bot.parse_incoming(dict(base, rxTime=1772800000), "!me")["when"]
    check("no ~ when it was reported", stamped.startswith("~"), False)
    check(
        "the node's time is used verbatim",
        stamped,
        _dt.datetime.fromtimestamp(1772800000).strftime("%H:%M:%S"),
    )

    print("the reply carries it too, since that line describes the message")
    info = bot.parse_incoming(dict(base), "!me")
    reply = bot.build_reply_text("Quebec", {**info, "from_name": "Bug2", "distance_m": None})
    check("first line is the rule's reply", reply.splitlines()[0], "BOT: Quebec")
    check("the detail line is marked derived", reply.splitlines()[1].startswith("[~"), True)
    check("and has no question marks", "?" in reply, False)


def _server_report(rules_text, channels):
    """What ServerBot logs on config sync, with `channels` as {index: name}."""
    with_rules(rules_text)
    logged = []
    server = bot.ServerBot()
    server.log = logged.append
    server.interface = types.SimpleNamespace(
        getMyUser=lambda: {"id": "!me", "longName": "BUG1119"},
        localNode=types.SimpleNamespace(
            channels=[
                types.SimpleNamespace(index=i, settings=types.SimpleNamespace(name=n))
                for i, n in channels.items()
            ]
        ),
    )
    server.on_config_synced(server.interface)
    return logged


def test_server_report_understands_exclusions():
    print("the server's config-sync report handles [!exclude]")
    channels = {0: "", 1: "SignalTest", 3: "EDGE_ATS"}
    logged = _server_report(
        "[!exclude]\nSignalTest\nLongFast\n\n[*]\nping=pong\nhello=hi\n", channels
    )
    blob = "\n".join(logged)

    # This is the bug as reported: the server warned that a working exclusion
    # matched no channel, because only the TUI's copy of the report had been
    # taught about the section.
    check(
        "no longer calls [!exclude] a dead section",
        "這些區段對應不到本機頻道,不會生效: !exclude" in blob,
        False,
    )
    # And it was counted among the rules, so five excluded channels read as five
    # loaded keywords.
    check("not counted as rules", "[!exclude]=" in blob, False)
    check("the real rules are still counted", "[*]=2" in blob, True)

    check("says what [*] does not cover", "[*] 不適用於這些頻道: LongFast, SignalTest" in blob, True)
    check(
        "and which entries excluded nothing",
        "LongFast" in blob and "沒有排除到任何東西" in blob,
        True,
    )
    # SignalTest does match a channel here, so it must not be in that warning.
    warning = next((l for l in logged if "沒有排除到任何東西" in l), "")
    check("the working entry is not in the warning", "SignalTest" in warning, False)

    print("a genuinely unknown section is still reported")
    logged = _server_report("[!exclude]\nSignalTest\n\n[TYPOO]\nx=y\n", channels)
    blob = "\n".join(logged)
    check("the typo is caught", "TYPOO" in blob and "不會生效" in blob, True)
    check("but the exclusion is not", "!exclude" not in blob.split("不會生效")[-1], True)

    print("and with no exclusions at all it says nothing about them")
    logged = _server_report("[*]\nping=pong\n", channels)
    blob = "\n".join(logged)
    check("no exclusion line", "不適用於這些頻道" in blob, False)
    check("no exclusion warning", "沒有排除到任何東西" in blob, False)


def test_urllib3_warning_filtered():
    print("the LibreSSL warning is filtered, in all three programs")
    # It printed on every single start. Nothing here makes an HTTPS request
    # through urllib3 - it arrives as a dependency of meshtastic - so there was
    # nothing to act on, just noise above the first line of real output.
    import pathlib as _pathlib

    root = _pathlib.Path(bot.__file__).parent
    for name in ("bot.py", "bot_dual.py", "bot_server.py"):
        text = (root / name).read_text(encoding="utf-8")
        check(
            f"{name} filters it",
            'warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")' in text,
            True,
        )
        # Before the import that triggers it, or the warning is already out.
        # The first thing to reach urllib3 is the dependency check, not the
        # `import meshtastic` further down - checking only against the latter
        # is why this passed while the warning still printed on every start.
        # Matched on the call rather than the dotted name, which also appears
        # in the comment explaining the ordering.
        check(
            f"{name} filters before the dependency check imports meshtastic",
            text.index("urllib3 v2 only supports OpenSSL")
            < text.index("importlib.import_module(_module_name)"),
            True,
        )
        check(
            f"{name} filters before importing meshtastic",
            text.index("urllib3 v2 only supports OpenSSL") < text.index("\nimport meshtastic"),
            True,
        )
        # Filtered by message, not by category: a different urllib3 warning
        # should still be seen.
        check(f"{name} does not silence urllib3 wholesale", 'module="urllib3"' in text, False)


class _StubBrokerClient:
    """Stands in for paho, recording what the bridge asked the broker to do.

    Only the handful of calls MqttProxy makes, so a signature it gets wrong
    fails here rather than against a live broker.
    """

    def __init__(self):
        self.published = []
        self.subscribed = []
        self.connects = []
        self.tls = False
        self.credentials = None
        self.disconnected = False
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None

    def tls_set(self, *args, **kwargs):
        self.tls = True

    def username_pw_set(self, username, password=None):
        self.credentials = (username, password)

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    def connect(self, host, port, keepalive=60):
        self.connects.append((host, port, keepalive))

    def loop_forever(self):
        threading.Event().wait()  # a connected client sits here until it drops

    def disconnect(self):
        self.disconnected = True


class _QuietMqttProxy(bot.MqttProxy):
    """MqttProxy with the reconnect thread neutered.

    start() otherwise leaves a thread reaching for a broker for the rest of the
    suite. The pacing that thread exists for is checked directly, in
    test_mqtt_reconnect_pacing.
    """

    supervised = False

    def _supervise(self):
        self.supervised = True


def _mqtt_config(**over):
    """The MQTT module config as the node reports it - the real values read off
    the hardware, so a default that drifts shows up as a changed expectation."""
    fields = dict(
        enabled=True,
        proxy_to_client_enabled=True,
        address="mqtt.meshtastic.org",
        username="meshdev",
        password="large4cats",
        root="msh/TW",
        tls_enabled=True,
    )
    fields.update(over)
    return types.SimpleNamespace(**fields)


def _mqtt_channel(index, name, downlink=True):
    return types.SimpleNamespace(
        index=index,
        settings=types.SimpleNamespace(name=name, downlink_enabled=downlink),
    )


def _mqtt_bot(config=None, channels=None, out=None):
    """A stand-in for the bot the bridge hangs off.

    MqttProxy only ever touches .interface, .my_id and .log, which is the point
    of it holding the bot rather than the interface: the interface object is
    replaced on every link reconnect.
    """
    out = out if out is not None else io.StringIO()
    to_radio = []
    interface = types.SimpleNamespace(
        localNode=types.SimpleNamespace(
            moduleConfig=types.SimpleNamespace(mqtt=config or _mqtt_config()),
            channels=channels if channels is not None else [_mqtt_channel(3, "EDGE_ATS")],
        ),
        sendMqttClientProxyMessage=lambda topic, data: to_radio.append((topic, data)),
    )
    holder = types.SimpleNamespace(
        interface=interface,
        my_id="!f2dcbabe",
        log=lambda text: print(text, file=out),
    )
    return holder, to_radio, out


def _started_proxy(config=None, channels=None, out=None):
    """A bridge already started against a stub broker, plus everything to
    inspect afterwards."""
    holder, to_radio, out = _mqtt_bot(config, channels, out)
    stub = _StubBrokerClient()
    proxy = _QuietMqttProxy(holder, client_factory=lambda settings: stub)
    proxy.start()
    return proxy, stub, holder, to_radio, out


def _proxy_message(topic, data=None, text=None, retained=False):
    """A real MqttClientProxyMessage, not a namespace: the payload is a oneof
    and WhichOneof is what the bridge selects on."""
    message = mesh_pb2.MqttClientProxyMessage()
    message.topic = topic
    if text is None:
        message.data = data if data is not None else b""
    else:
        message.text = text
    message.retained = retained
    return message


def test_mqtt_broker_settings():
    print("the broker is read off the node, not written down here")
    # The operator changes these on the device. A bridge with its own copy would
    # keep publishing to yesterday's broker, so this reads the node's config -
    # these are the values the real node reports today.
    settings = bot.mqtt_broker_settings(_mqtt_config())
    check("host from the node", settings["host"], "mqtt.meshtastic.org")
    check("tls means 8883", settings["port"], bot.MQTT_TLS_PORT)
    check("credentials from the node", settings["username"], "meshdev")
    check("root from the node", settings["root"], "msh/TW")
    check("tls flag from the node", settings["tls"], True)

    print("without tls it is the plain port")
    plain = bot.mqtt_broker_settings(_mqtt_config(tls_enabled=False))
    check("port", plain["port"], bot.MQTT_PORT)

    print("an address may name its own port")
    # The device has no port setting at all, so "host:port" in the address is
    # the only way to reach a broker that is not on 1883/8883.
    named = bot.mqtt_broker_settings(_mqtt_config(address="broker.lan:1884"))
    check("host", named["host"], "broker.lan")
    check("port wins over the tls default", named["port"], 1884)

    print("blank fields fall back to what the firmware falls back to")
    blank = bot.mqtt_broker_settings(
        _mqtt_config(address="", username="", password="", root="")
    )
    check("address", blank["host"], bot.MQTT_DEFAULT_ADDRESS)
    check("root", blank["root"], bot.MQTT_DEFAULT_ROOT)
    # A blank address discards the stored credentials as well, which is what
    # PubSubConfig does: credentials for a broker the operator did not name are
    # credentials for the wrong broker.
    kept = bot.mqtt_broker_settings(_mqtt_config(address="", username="me", password="pw"))
    check("username is not carried onto the default broker", kept["username"], bot.MQTT_DEFAULT_USERNAME)
    check("nor the password", kept["password"], bot.MQTT_DEFAULT_PASSWORD)
    # But a named broker keeps a deliberately empty username - an open broker
    # is a real configuration.
    anon = bot.mqtt_broker_settings(_mqtt_config(address="broker.lan", username="", password=""))
    check("a named broker may have no credentials", anon["username"], "")

    print("a trailing slash on the root does not double up")
    # The firmware pastes the root onto a path that already starts with one, so
    # "msh/TW/" there would give "msh/TW//2/e/" here.
    slashed = bot.mqtt_broker_settings(_mqtt_config(root="msh/TW/"))
    check("root", slashed["root"], "msh/TW")


def test_mqtt_uplink_reaches_the_broker():
    print("a proxy message from the radio is published to the broker")
    proxy, stub, holder, _, out = _started_proxy()

    print("the connection is set up from the node's settings")
    check("tls was switched on", stub.tls, True)
    check("the node's credentials were used", stub.credentials, ("meshdev", "large4cats"))
    check("callbacks are wired", stub.on_message is not None, True)
    check("the reconnect thread was started", proxy.supervised, True)

    # Nothing may be sent before the broker accepts us: paho refuses a
    # subscribe with no connection, so the set is sent from on_connect.
    check("nothing subscribed before connect", stub.subscribed, [])
    proxy._on_connect(stub, None, {}, 0, None)
    check(
        "the channel and PKI topics are subscribed on connect",
        sorted(topic for topic, _ in stub.subscribed),
        ["msh/TW/2/e/EDGE_ATS/+", "msh/TW/2/e/PKI/+"],
    )
    check("at the firmware's QoS", {qos for _, qos in stub.subscribed}, {1})

    proxy.on_proxy_message(
        _proxy_message("msh/TW/2/e/EDGE_ATS/!f2dcbabe", data=b"\x01\x02"), holder.interface
    )
    check(
        "published verbatim",
        stub.published,
        [("msh/TW/2/e/EDGE_ATS/!f2dcbabe", b"\x01\x02", 0, False)],
    )
    check("counted, not logged", proxy.up_count, 1)

    print("the retained flag and the text variant survive")
    # The payload is a oneof: reading the wrong arm of a union gives whichever
    # bytes happen to alias it, which for a text message is its own characters
    # read as a length.
    proxy.on_proxy_message(
        _proxy_message("msh/TW/2/map/", text="hello", retained=True), holder.interface
    )
    check("text is sent as bytes", stub.published[-1][1], b"hello")
    check("retained is passed through", stub.published[-1][3], True)

    print("the primary channel's topic is learned from what the node publishes")
    # The firmware names the unnamed primary after the modem preset, a value the
    # config does not carry - so it is taken from the topic the node used.
    proxy.on_proxy_message(
        _proxy_message("msh/TW/2/e/MediumFast/!f2dcbabe", data=b"x"), holder.interface
    )
    check(
        "subscribed to it as it appears",
        ("msh/TW/2/e/MediumFast/+", 1) in stub.subscribed,
        True,
    )
    # Once is enough - the node publishes on it constantly.
    proxy.on_proxy_message(
        _proxy_message("msh/TW/2/e/MediumFast/!f2dcbabe", data=b"y"), holder.interface
    )
    check(
        "and not again for every packet",
        [t for t, _ in stub.subscribed].count("msh/TW/2/e/MediumFast/+"),
        1,
    )

    print("nothing is learned when no channel wants downlink")
    # The node discards an envelope whose channel has downlink_enabled off, so
    # subscribing to one only spends link bandwidth.
    quiet, quiet_stub, quiet_holder, _, _ = _started_proxy(
        channels=[_mqtt_channel(3, "EDGE_ATS", downlink=False)]
    )
    quiet._on_connect(quiet_stub, None, {}, 0, None)
    quiet.on_proxy_message(
        _proxy_message("msh/TW/2/e/MediumFast/!f2dcbabe", data=b"x"), quiet_holder.interface
    )
    check("no subscriptions at all", quiet_stub.subscribed, [])
    check("but the uplink still goes out", len(quiet_stub.published), 1)

    print("one line per state change, and none per message")
    log = out.getvalue()
    check("said it started", "MQTT 橋接啟動" in log, True)
    check("said it connected", "MQTT 已連線" in log, True)
    # Four messages went through above. A line each would bury the log on a mesh
    # moving hundreds of packets a minute.
    check("no line per message", log.count("\n"), 2)
    check("the volume is in the heartbeat instead", "上行 4" in proxy.heartbeat_fragment(), True)


def test_mqtt_downlink_reaches_the_radio():
    print("a broker message is handed back to the node")
    proxy, stub, holder, to_radio, _ = _started_proxy()
    proxy._on_connect(stub, None, {}, 0, None)

    proxy._on_message(
        stub, None, types.SimpleNamespace(topic="msh/TW/2/e/EDGE_ATS/!other", payload=b"\x08\x01")
    )
    # Handed over untouched: the node decodes and filters it itself
    # (onReceiveProto), so there is nothing useful to inspect on the way.
    check("forwarded verbatim", to_radio, [("msh/TW/2/e/EDGE_ATS/!other", b"\x08\x01")])
    check("counted", proxy.down_count, 1)

    print("a downlink arriving after the link went away is dropped, not raised")
    # The bridge outlives any one interface: a reconnect replaces the object,
    # and between the two there is none.
    holder.interface = None
    proxy._on_message(stub, None, types.SimpleNamespace(topic="t", payload=b"z"))
    check("nothing forwarded", len(to_radio), 1)
    check("and it was not counted as an error", proxy.error_count, 0)


def test_mqtt_is_off_without_the_flag():
    print("no --mqtt, no bridge - and nothing else changes")
    # A bridge that started itself would take a mesh the operator may consider
    # private and begin republishing it to whatever broker the device names,
    # which for an untouched config is the public one.
    out = io.StringIO()
    server, _ = _fake_server("[EDGE_ATS]\nping=pong\n", out)
    check("no bridge object at all", server.mqtt, None)

    server.interface.localNode.moduleConfig = types.SimpleNamespace(mqtt=_mqtt_config())
    server.on_config_synced(server.interface)
    log = out.getvalue()
    bridge_lines = [
        line for line in log.splitlines() if line.split(" ", 2)[2].startswith("MQTT")
    ]
    check("config sync says nothing about the bridge", bridge_lines, [])
    # Not by staying quiet about the node's own setting, though: that field is
    # in the status block whether or not anything is bridging.
    check("the node's own ok-to-mqtt is still reported", "OK to MQTT=是" in log, True)
    # The heartbeat columns a running server is read by must not move.
    check("the heartbeat is unchanged", "MQTT" in server._heartbeat_line(), False)
    check("the rules report still happened", "[EDGE_ATS]=1" in log, True)

    print("with --mqtt the same sync starts it")
    out2 = io.StringIO()
    server2, _ = _fake_server("[EDGE_ATS]\nping=pong\n", out2)
    server2.interface.localNode.moduleConfig = types.SimpleNamespace(mqtt=_mqtt_config())
    server2.interface.localNode.channels = [_mqtt_channel(3, "EDGE_ATS")]
    stub = _StubBrokerClient()
    server2.mqtt = _QuietMqttProxy(server2, client_factory=lambda settings: stub)
    server2.on_config_synced(server2.interface)
    # The whole line, not just its prefix: a start that failed logs
    # "MQTT 橋接啟動失敗", which a substring test would happily accept.
    check("the bridge started", "MQTT 橋接啟動: mqtts://" in out2.getvalue(), True)
    check("and the heartbeat now carries it", "MQTT" in server2._heartbeat_line(), True)

    print("--mqtt is forwarded to the background copy")
    # The child is the process that actually serves. Dropping the flag would
    # leave --daemon --mqtt running a server with no bridge and nothing saying so.
    argv = bot.detached_argv((bot.TRANSPORT_BLE, "Bug2_1ca6"), None, 600, True)
    check("child gets --mqtt", "--mqtt" in argv, True)
    check(
        "and does not when it was not asked for",
        "--mqtt" in bot.detached_argv((bot.TRANSPORT_BLE, "Bug2_1ca6"), None, 600),
        False,
    )


def test_mqtt_respects_the_device_settings():
    print("the device can refuse the bridge, and says why")
    # Both of these are device settings, so the fix is on the device - worth a
    # plain line, since --mqtt was asked for and nothing would be relayed.
    proxy, _, _, _, out = _started_proxy(_mqtt_config(enabled=False))
    check("no client was built", proxy._client, None)
    check("mqtt.enabled is named", "mqtt.enabled" in out.getvalue(), True)

    proxy, _, _, _, out = _started_proxy(_mqtt_config(proxy_to_client_enabled=False))
    check("no client was built", proxy._client, None)
    check("proxy_to_client_enabled is named", "proxy_to_client_enabled" in out.getvalue(), True)

    print("and a bridge that never started relays nothing rather than raising")
    holder, to_radio, _ = _mqtt_bot()
    proxy.on_proxy_message(_proxy_message("t", data=b"x"), holder.interface)
    check("nothing published, nothing raised", proxy.up_count, 0)
    check("and no error recorded either", proxy.error_count, 0)


def test_mqtt_failures_stay_inside_the_bridge():
    print("a broker that fails must not take the bot with it")
    proxy, stub, holder, to_radio, out = _started_proxy()
    proxy._on_connect(stub, None, {}, 0, None)

    def explode(*args, **kwargs):
        raise RuntimeError("broker gone")

    # The uplink runs on meshtastic's publishing thread - the thread that hands
    # every received packet to on_receive. An exception loose there would stop
    # the bot answering messages at all.
    stub.publish = explode
    proxy.on_proxy_message(_proxy_message("t", data=b"x"), holder.interface)
    check("the exception did not escape", True, True)
    check("it was counted", proxy.error_count, 1)
    check("and named once", "broker gone" in out.getvalue(), True)

    print("the same failure repeating does not repeat the log line")
    before = out.getvalue().count("broker gone")
    for _ in range(50):
        proxy.on_proxy_message(_proxy_message("t", data=b"x"), holder.interface)
    check("still one line", out.getvalue().count("broker gone"), before)
    check("but every failure is counted", proxy.error_count, 51)
    check("and the heartbeat shows it", "錯誤 51" in proxy.heartbeat_fragment(), True)

    print("a radio that fails on the way back is contained too")
    # This one runs on paho's network thread, which the downlink needs.
    holder.interface = types.SimpleNamespace(sendMqttClientProxyMessage=explode)
    proxy._on_message(stub, None, types.SimpleNamespace(topic="t", payload=b"z"))
    check("not raised", True, True)
    check("not counted as delivered", proxy.down_count, 0)

    print("a refused connect is reported, not read as success")
    # paho's v2 callbacks hand over a ReasonCode object, which has no __bool__
    # and is therefore truthy even for Success. Measured against the real
    # broker: `if reason_code:` logged "broker 拒絕連線: Success" and then
    # relayed nothing at all.
    check(
        "a Success-like reason code is not a failure",
        bot.mqtt_connect_failed(types.SimpleNamespace(is_failure=False)),
        False,
    )
    check(
        "a failing one is",
        bot.mqtt_connect_failed(types.SimpleNamespace(is_failure=True)),
        True,
    )
    check("a plain 0 is still accepted", bot.mqtt_connect_failed(0), False)
    out3 = io.StringIO()
    refused, refused_stub, _, _, out3 = _started_proxy(out=out3)
    refused._on_connect(refused_stub, None, {}, types.SimpleNamespace(is_failure=True), None)
    check("not marked connected", refused.connected, False)
    check("nothing subscribed", refused_stub.subscribed, [])
    check("said so", "拒絕連線" in out3.getvalue(), True)


def test_mqtt_uplink_survives_pubsub_delivery():
    print("the guarded handler is still one pubsub will deliver to")
    # The uplink is reached through pub.sendMessage, and pypubsub inspects a
    # listener's signature before accepting it. A wrapper that hid the argument
    # names would be rejected at subscribe time - which is the whole feature
    # failing, on a code path no direct call exercises.
    proxy, stub, holder, _, _ = _started_proxy()
    proxy._on_connect(stub, None, {}, 0, None)
    try:
        pub.sendMessage(
            "meshtastic.mqttclientproxymessage",
            proxymessage=_proxy_message("msh/TW/2/e/EDGE_ATS/!x", data=b"via-pubsub"),
            interface=holder.interface,
        )
        check("the message arrived", [p[1] for p in stub.published], [b"via-pubsub"])
    finally:
        proxy.stop()
    check("stop unsubscribes", proxy._stopped.is_set(), True)
    pub.sendMessage(
        "meshtastic.mqttclientproxymessage",
        proxymessage=_proxy_message("msh/TW/2/e/EDGE_ATS/!x", data=b"after-stop"),
        interface=holder.interface,
    )
    check("and nothing arrives afterwards", len(stub.published), 1)


class _RecordingEvent:
    """Stands in for the stop event, so _supervise's pacing can be read off
    instead of waited out."""

    def __init__(self, stop_after):
        self.waits = []
        self._stop_after = stop_after

    def is_set(self):
        return len(self.waits) >= self._stop_after

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.is_set()

    def set(self):
        self._stop_after = 0


def test_mqtt_reconnect_pacing():
    print("the broker reconnect uses the same backoff table as the link")
    proxy, stub, holder, _, out = _started_proxy()

    def refuse(*args, **kwargs):
        raise OSError("connection refused")

    stub.connect = refuse
    proxy._stopped = _RecordingEvent(stop_after=6)
    # The real one: _QuietMqttProxy replaces _supervise so start() cannot leave
    # a thread behind, and this is the test that method exists for.
    bot.MqttProxy._supervise(proxy)
    check(
        "1, 2, 5, 10, then 30 a time",
        proxy._stopped.waits,
        [1, 2, 5, 10, 30, 30],
    )
    check(
        "which is ReplyEngine's table",
        proxy._stopped.waits[:5],
        list(bot.ReplyEngine.RECONNECT_DELAYS),
    )
    # An outage is one log line, not one per retry: a broker that is down stays
    # down for hours, and the heartbeat is what says it still is.
    check("reported once", out.getvalue().count("連不上 broker"), 1)
    check("and not marked connected", proxy.connected, False)

    print("a connect that worked starts the table over")
    # Otherwise the next outage would inherit the last one's 30 seconds, and a
    # momentary blip would take half a minute to recover from.
    proxy2, stub2, _, _, _ = _started_proxy()
    attempts = []

    def once_then_refuse(host, port, keepalive=60):
        attempts.append(host)
        if len(attempts) > 1:
            raise OSError("connection refused")

    stub2.connect = once_then_refuse
    stub2.loop_forever = lambda: None  # a drop, right after connecting
    proxy2._stopped = _RecordingEvent(stop_after=3)
    bot.MqttProxy._supervise(proxy2)
    check("waited 1s, connected, then 1s again", proxy2._stopped.waits, [1, 1, 2])


def test_mqtt_shutdown_is_bounded():
    print("the broker disconnect gets a deadline, like the interface close")
    # Same reason ServerBot.CLOSE_TIMEOUT exists: a teardown that can block is a
    # process that needs SIGKILL, and this is the least important thing a
    # shutdown waits on.
    proxy, stub, _, _, out = _started_proxy()
    proxy.STOP_TIMEOUT = 1  # keep the test quick; the shape is what matters
    hanging = threading.Event()

    def never_returns():
        hanging.set()
        threading.Event().wait()

    stub.disconnect = never_returns
    started = time.monotonic()
    proxy.stop()
    took = time.monotonic() - started
    check("disconnect was attempted", hanging.is_set(), True)
    check("gave up rather than hanging", took < 5, True)
    check("waited about the deadline", 0.9 <= took < 3, True)
    check("said so", "MQTT 中斷逾時" in out.getvalue(), True)

    print("a disconnect that raises is swallowed")
    proxy2, stub2, _, _, out2 = _started_proxy()

    def raising():
        raise RuntimeError("socket already gone")

    stub2.disconnect = raising
    proxy2.stop()
    check("logged and ignored", "socket already gone" in out2.getvalue(), True)

    print("a clean disconnect is immediate and says nothing extra")
    proxy3, stub3, _, _, out3 = _started_proxy()
    started = time.monotonic()
    proxy3.stop()
    check("immediate", time.monotonic() - started < 1, True)
    check("it did disconnect", stub3.disconnected, True)
    check("no timeout message", "逾時" in out3.getvalue(), False)

    print("and a bridge that never connected has nothing to stop")
    proxy4, _, _, _, out4 = _started_proxy(_mqtt_config(enabled=False))
    proxy4.stop()
    check("no timeout message", "逾時" in out4.getvalue(), False)


def test_mqtt_shutdown_runs_before_the_interface():
    print("stopping the server stops the bridge, before closing the link")
    # The relay must not still be handing the interface work while it is being
    # torn down - and it needs its own deadline, since a broker socket can hang
    # exactly like a BLE one.
    out = io.StringIO()
    server, _ = _fake_server("[*]\nping=pong\n", out)
    order = []
    server.mqtt = types.SimpleNamespace(stop=lambda: order.append("mqtt"))

    class Interface:
        def close(self):
            order.append("interface")

    server._shutdown(Interface())
    check("bridge first, link second", order, ["mqtt", "interface"])

    print("and a server with no bridge shuts down exactly as before")
    out2 = io.StringIO()
    server2, _ = _fake_server("[*]\nping=pong\n", out2)
    closed = []

    class Interface2:
        def close(self):
            closed.append(True)

    server2._shutdown(Interface2())
    check("the link still closed", closed, [True])
    check("nothing about MQTT", "MQTT" in out2.getvalue(), False)


def test_local_status_rows():
    print("the node's own status, as rows both front ends read")
    server, _ = _fake_server("[*]\nping=pong\n", io.StringIO())
    rows = bot.local_status_rows(server)
    values = {label: value for _group, label, value in rows}

    check("region as its name, not its number", values["Region"], "TW")
    check("role", values["Role"], "CLIENT")
    check("preset", values["Preset"], "MEDIUM_FAST")
    check("an unset slot reads as automatic", values["Slot"], "(Auto)")
    check("tx power carries its unit", values["Tx Power"], "30 dBm")
    check("uptime formatted, not raw seconds", values["Uptime"], "02:42")
    check("battery with its voltage", values["電量"], "94% 4.012V")
    check("channel utilisation", values["Ch.Util"], "3.2%")
    check("ok-to-mqtt as a word", values["OK to MQTT"], "是")
    check("nothing heard yet", values["最近收訊"], "--")
    check("a missing gps module is stated", values["GPS"], "無 GPS 模組")
    check("the link, with the peer", values["連線"], "BLE Bug2_1ca6")
    # The pane adds its own emphasis and the log has none to add, so a value
    # carrying markup would have to be stripped by one of them.
    check("no markup in any value", any("[/" in v for v in values.values()), False)
    # Folding by group only preserves pane order while the groups are
    # contiguous; interleaving them would silently reorder the pane.
    groups = [group for group, _label, _value in rows]
    check("groups are contiguous", groups, sorted(groups, key=groups.index))
    # Not connected is not "all fields blank" - it is no fields.
    server.interface = None
    check("nothing to report before a link", bot.local_status_rows(server), [])


def test_local_status_marks_derived_values():
    print("a derived frequency is marked, a reported one is not")
    server, _ = _fake_server("[*]\nping=pong\n", io.StringIO())
    lora = server.interface.localNode.localConfig.lora

    def freq():
        return dict(
            (label, value) for _g, label, value in bot.local_status_rows(server)
        )["頻率"]

    # Slot left to the firmware, which picks it from a hash of the channel
    # name. lora_params refuses to reproduce that hash rather than print a
    # confidently wrong number, and the row has to say so.
    check("an automatic slot cannot be derived", freq(), "無法推導")

    lora.channel_num = 20
    check("a real slot derives, and the ~ says it was derived", freq()[0], "~")

    lora.override_frequency = 923.5
    check("a frequency the node reports gets no ~", freq(), "923.500 MHz")
    check("bandwidth is derived from the preset",
          dict((l, v) for _g, l, v in bot.local_status_rows(server))["Bandwidth"],
          "~250 kHz")


def test_local_status_lines_fold_by_group():
    print("the server folds those rows into one line per group")
    server, _ = _fake_server("[*]\nping=pong\n", io.StringIO())
    lines = bot.local_status_lines(server)
    check("one line per group", len(lines), 6)
    check("the node line", lines[0], "節點: Region=TW 韌體=查詢中... Role=CLIENT")
    check("every line names its group", all(":" in line for line in lines), True)
    # A group holding a single field named after itself would otherwise read
    # "連線: 連線=BLE Bug2_1ca6".
    check("no doubled label", lines[-1], "連線: BLE Bug2_1ca6")


def test_local_status_is_logged_at_startup():
    print("server mode prints the local status when it connects")
    out = io.StringIO()
    server, _ = _fake_server("[*]\nping=pong\n", out)
    server.on_config_synced(server.interface)
    log = out.getvalue()
    for expected in (
        "節點: Region=TW",
        "無線電: Preset=MEDIUM_FAST",
        "裝置: Uptime=02:42",
        "定位: GPS=無 GPS 模組",
        "連線: BLE Bug2_1ca6",
    ):
        check(f"logged {expected}", expected in log, True)
    # The node hands its metadata over during the config download, so the
    # version is there without an admin round-trip of our own.
    check("firmware version came off the download", "韌體=2.7.11.4e40e6f" in log, True)
    # The block sits between the node's name and its channels, where the rest
    # of the startup lines are.
    lines = [line.split(" ", 2)[2] for line in log.splitlines()]

    def first(prefix):
        return next(i for i, line in enumerate(lines) if line.startswith(prefix))

    check("after the node name", first("節點:") > first("設定同步完成"), True)
    check("and before the channels", first("節點:") < first("頻道:"), True)
    check("the whole block is together", first("連線:") + 1, first("頻道:"))


def test_local_status_cannot_stop_the_server():
    print("a status field in an unexpected shape cannot stop startup")
    out = io.StringIO()
    server, _ = _fake_server("[*]\nping=pong\n", out)
    # Whatever surprises us - a firmware that omits a config block, a library
    # that renames one. The banner is not worth the server.
    server.interface.localNode.localConfig = None
    server.on_config_synced(server.interface)
    log = out.getvalue()
    check("the node was still reported", "設定同步完成" in log, True)
    check("the failure was named", "本機狀態讀取失敗" in log, True)
    check("and startup carried on to the channels", "#3 EDGE_ATS" in log, True)
    check("and to the rules", "[*]=1" in log, True)


def test_local_status_is_in_both_files():
    print("the pane and the server read one field list, not two")
    for name in ("local_status_rows", "local_status_lines", "format_uptime"):
        check(f"bot_server has {name}", hasattr(bot_server, name), True)
        check(
            f"{name} is identical",
            inspect.getsource(getattr(bot, name))
            == inspect.getsource(getattr(bot_server, name)),
            True,
        )
    # lora_params and format_uptime used to be stripped out of bot_server as
    # UI-only. They are shared now, and a generator that still dropped them
    # would leave a NameError on the startup path.
    check("bot_server can derive lora values", hasattr(bot_server, "lora_params"), True)
    pane = inspect.getsource(bot.MeshtasticTUI._render_local_status)
    check("the pane calls the shared function", "local_status_rows(self)" in pane, True)
    check("the pane has no field list of its own", "config_pb2" in pane, False)
    check("nor its own uptime formatting", "format_uptime" in pane, False)


def test_ctrlc_during_a_ble_connect_cannot_hang():
    print("Ctrl-C during a connect leaves, instead of handing the exit to atexit")
    # meshtastic registers client.disconnect with atexit (ble_interface.py) and
    # only unregisters it in close(). Anything that reaches interpreter shutdown
    # without a completed close() runs that handler, it dispatches onto an
    # asyncio loop that is going away, and the process hangs until SIGKILL.
    #
    # A BLE connect takes about half a minute. The handlers used to be
    # installed after open_interface(), so a Ctrl-C in that window was a plain
    # KeyboardInterrupt inside the library - which is the whole of the
    # "sometimes": the atexit handler only exists once the client does.
    src = inspect.getsource(bot_server.ServerBot.run)
    i_abort = src.find("_abort_before_connect")
    i_open = src.find("interface = open_interface")
    i_graceful = src.find("signal.signal(sig, self.stop)")
    check("a handler is installed before the connect", 0 <= i_abort < i_open, True)
    check("and the graceful one replaces it after", i_open < i_graceful, True)
    # os._exit rather than sys.exit or a raise: the atexit handler is the thing
    # that hangs, so the exit has to skip atexit entirely.
    abort = src[i_abort : src.find("for sig in", i_abort)]
    check("it leaves without running atexit", "os._exit" in abort, True)
    check("both signals are covered", src.count("signal.SIGINT, signal.SIGTERM"), 2)


def test_a_timed_out_close_does_not_hand_the_exit_to_atexit():
    print("a close that times out is reported, and the exit skips atexit too")
    # The deadline used to bound only the waiting. close() is also where the
    # library unregisters its atexit handler, so a close that never finished
    # left that handler in place and the process hung on the way out anyway.
    out = io.StringIO()
    server, _ = _fake_server("[*]\nping=pong\n", out)
    server.CLOSE_TIMEOUT = 1  # keep it quick; the shape is what matters
    hanging = threading.Event()

    class Hangs:
        def close(self):
            hanging.set()
            threading.Event().wait()

    started = time.monotonic()
    closed = server._shutdown(Hangs())
    took = time.monotonic() - started
    check("close was attempted", hanging.is_set(), True)
    check("gave up rather than hanging", took < 5, True)
    check("waited about the deadline", 0.9 <= took < 3, True)
    check("said so", "介面關閉逾時" in out.getvalue(), True)
    check("and reported that it did not close", closed, False)

    # A close that works reports the opposite, so run() does not leave hard for
    # no reason - os._exit skips flushes and any other atexit a user added.
    class Closes:
        def __init__(self):
            self.called = False

        def close(self):
            self.called = True

    ok = Closes()
    check("a working close is reported clean", server._shutdown(ok), True)
    check("and was actually called", ok.called, True)


def test_channel_label_carries_the_name():
    print("a channel log line names the channel, not just its number")
    server, _ = _fake_server("[*]\nping=pong\n", io.StringIO())
    # The number leads so a log stays greppable by it; rules.txt is written in
    # names, so a line saying only "channel:3" means looking the number up.
    check("named channel", bot.channel_label(server.interface, 3), "3(EDGE_ATS)")
    # Nothing invented for the unnamed primary. The firmware substitutes the
    # modem preset's display name when it builds MQTT topics, but that is the
    # firmware's substitution and would read here as a name someone set.
    check("unnamed channel", bot.channel_label(server.interface, 0), "0")
    check("channel this node does not have", bot.channel_label(server.interface, 9), "9")

    # A label is not worth a dropped message.
    class Broken:
        pass

    check("a channel table in a shape we did not expect",
          bot.channel_label(Broken(), 3), "3")

    both = inspect.getsource(bot.channel_label) == inspect.getsource(
        bot_server.channel_label
    )
    check("one implementation in both files", both, True)


def test_incoming_line_uses_the_channel_label():
    print("the name reaches the actual log line")
    out = io.StringIO()
    server, _ = _fake_server("[EDGE_ATS]\nping=pong\n", out)
    server.on_receive(_text_packet("ping", bot.BROADCAST_ADDR, 501), server.interface)
    log = out.getvalue()
    check("the line carries the name", "channel:3(EDGE_ATS)" in log, True)
    # The old prefix still matches, so anything grepping by number keeps working.
    check("and still starts with the number", "channel:3(" in log, True)
    check("the reply happened too", "auto-reply" in log, True)

    # A direct message is keyed by node id, which has no channel to name.
    out2 = io.StringIO()
    server2, _ = _fake_server("[DM]\nping=pong\n", out2)
    server2.on_receive(_text_packet("ping", "!me", 502), server2.interface)
    check("a DM is unchanged", "node:!them" in out2.getvalue(), True)


def test_mqtt_bridge_is_in_both_files():
    print("the bridge exists in bot_dual and in the generated bot_server")
    # bot_server.py is generated by stripping the UI. A feature that lands in
    # bot_dual and not in the file people actually deploy is invisible until
    # someone tries to use it there.
    check("bot_dual has the bridge", hasattr(bot, "MqttProxy"), True)
    check("bot_server has it too", hasattr(bot_server, "MqttProxy"), True)
    check(
        "and it is one implementation",
        inspect.getsource(bot.MqttProxy) == inspect.getsource(bot_server.MqttProxy),
        True,
    )
    for name in ("mqtt_broker_settings", "mqtt_connect_failed", "_isolated"):
        check(
            f"{name} is identical",
            inspect.getsource(getattr(bot, name))
            == inspect.getsource(getattr(bot_server, name)),
            True,
        )

    import pathlib as _pathlib

    root = _pathlib.Path(bot.__file__).parent
    for name in ("bot_dual.py", "bot_server.py"):
        text = (root / name).read_text(encoding="utf-8")
        # The uv header is what makes ./bot_server.py work without a pip
        # install, and the generator edits that header, so it is the one place
        # a new dependency can silently be dropped.
        check(f"{name} declares paho-mqtt", '#     "paho-mqtt",' in text, True)
        check(f"{name} has the flag", '"--mqtt",' in text, True)
        check(f"{name} passes it to ServerBot", "mqtt=args.mqtt" in text, True)
        check(f"{name} forwards it to the child", 'argv.append("--mqtt")' in text, True)
    # bot.py is frozen as the original monitor and has no server to bridge from.
    frozen = (root / "bot.py").read_text(encoding="utf-8")
    check("bot.py is left alone", "--mqtt" in frozen, False)

    # paho is imported on first use, not at module import: a bot started without
    # --mqtt must not need a package it will never touch, which is also why it
    # is not in _REQUIRED_MODULES.
    check("paho is not a hard import", "import paho" in inspect.getsource(bot), False)
    check("nor a required module", "paho" in str(bot._REQUIRED_MODULES), False)
if __name__ == "__main__":
    original = bot.RULES_FILE
    try:
        test_default_rules_template()
        test_status_bar()
        test_status_bar_widget()
        test_packet_count()
        test_connection_lost()
        test_unread_bold()
        test_unread_bold_widgets()
        test_repopulate_count()
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
        test_packet_node_id()
        test_format_plain()
        test_server_bot_replies()
        test_server_bot_config_sync()
        test_server_heartbeat_and_stop()
        test_bounded_history()
        test_resolve_server_target()
        test_server_shares_the_engine()
        test_detached_argv()
        test_bot_server_is_generated_not_rewritten()
        test_bot_server_replies()
        test_config_sync_before_adopt()
        test_shutdown_is_bounded()
        test_stop_wakes_the_wait()
        test_list_devices()
        test_server_packet_count()
        test_packet_count_in_all_three_files()
        test_device_rows()
        test_exclude_parsing()
        test_is_excluded()
        test_exclude_drops_star()
        test_exclude_leaves_dms_alone()
        test_exclude_coverage_report()
        test_exclude_shipped_defaults()
        test_readme_worked_example()
        test_when_falls_back_to_our_clock()
        test_server_report_understands_exclusions()
        test_urllib3_warning_filtered()
        test_mqtt_broker_settings()
        test_mqtt_uplink_reaches_the_broker()
        test_mqtt_downlink_reaches_the_radio()
        test_mqtt_is_off_without_the_flag()
        test_mqtt_respects_the_device_settings()
        test_mqtt_failures_stay_inside_the_bridge()
        test_mqtt_uplink_survives_pubsub_delivery()
        test_mqtt_reconnect_pacing()
        test_mqtt_shutdown_is_bounded()
        test_mqtt_shutdown_runs_before_the_interface()
        test_mqtt_bridge_is_in_both_files()
        test_local_status_rows()
        test_local_status_marks_derived_values()
        test_local_status_lines_fold_by_group()
        test_local_status_is_logged_at_startup()
        test_local_status_cannot_stop_the_server()
        test_local_status_is_in_both_files()
        test_ctrlc_during_a_ble_connect_cannot_hang()
        test_a_timed_out_close_does_not_hand_the_exit_to_atexit()
        test_channel_label_carries_the_name()
        test_incoming_line_uses_the_channel_label()
    finally:
        bot.RULES_FILE = original

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for failure in _failures:
            print(" -", failure)
        sys.exit(1)
    print("all checks passed")
