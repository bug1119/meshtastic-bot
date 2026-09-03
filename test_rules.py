#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "meshtastic",
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
    check("starts at zero received", "收[/bold] 0" in text, True)
    check("starts at zero sent", "發[/bold] 0 ([dim]自動 0[/dim])" in text, True)
    check("no reconnect segment before any outage", "重連" in text, False)

    text = bar_text(received_count=42, sent_typed_count=2, sent_auto_count=5)
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


async def _status_bar_widget():
    from textual.widgets import Label

    app = bot.MeshtasticTUI()
    async with app.run_test() as pilot:
        bar = app.query_one("#status-bar", Label)
        await pilot.pause()
        check("the bar is one line tall", bar.size.height, 1)
        check("it starts filled in", "執行" in bar.render().plain, True)

        app.received_count = 12
        app.sent_typed_count = 1
        app.sent_auto_count = 2
        app._render_status_bar()
        await pilot.pause()
        plain = bar.render().plain
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
    server.interface = types.SimpleNamespace(
        nodes={"!them": {"user": {"shortName": "Bug2"}}},
        localNode=types.SimpleNamespace(channels=channels),
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
    ui_imports = [
        line
        for line in inspect.getsource(bot_server).splitlines()
        if line.startswith(("import textual", "from textual", "import lora_params"))
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


if __name__ == "__main__":
    original = bot.RULES_FILE
    try:
        test_default_rules_template()
        test_status_bar()
        test_status_bar_widget()
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
    finally:
        bot.RULES_FILE = original

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for failure in _failures:
            print(" -", failure)
        sys.exit(1)
    print("all checks passed")
