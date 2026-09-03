#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "meshtastic",
#     "pypubsub",
# ]
# ///
"""Meshtastic auto-reply server - headless, no UI.

Reads rules.txt and answers matching messages on the mesh, logging one line per
event to stdout. This is bot.py's rules engine with the three-pane monitor taken
out: same rules.txt, same exact-match semantics, same [DM] / [channel] / [*]
section precedence, so a rule tested in the UI behaves identically here.

GENERATED from bot_dual.py by make_bot_server.py - do not edit by hand. Change
bot_dual.py and regenerate; test_rules.py compares the shared functions of the
two files and fails if they differ.

    ./bot_server.py --port /dev/cu.usbmodem2101
    ./bot_server.py --host 192.168.0.247 --daemon --log ~/bot.log
    ./bot_server.py --ble Meshtastic_1a2b --heartbeat 0

With no target it lists what it can see and reads a number, then serves. With
--daemon it relaunches itself detached once the device has been chosen, so the
picking still happens on your terminal and nothing else does. Stop it with
SIGTERM (plain kill, or Ctrl-C in the foreground).
"""

# Deferred annotation evaluation, so the `X | None` unions below stay strings
# at runtime instead of being evaluated. Without this the module fails to
# import on Python 3.9, which parses PEP 604 unions but cannot evaluate them.
from __future__ import annotations

import importlib
import sys

# Check dependencies up front so a missing package fails fast with a plain
# "pip install X" hint instead of a raw ImportError/traceback from somewhere
# deep inside textual/meshtastic. meshtastic.ble_interface is checked
# specifically (not just "meshtastic") since it pulls in bleak, which is a
# separate, occasionally-missing install.
_REQUIRED_MODULES = [
    ("pubsub", "pypubsub"),
    ("meshtastic.ble_interface", "meshtastic"),
]
_missing_packages = []
for _module_name, _pip_name in _REQUIRED_MODULES:
    try:
        importlib.import_module(_module_name)
    except ImportError:
        if _pip_name not in _missing_packages:
            _missing_packages.append(_pip_name)
if _missing_packages:
    print("缺少必要的 Python 套件。", file=sys.stderr)
    print("    ./bot.py            # 讓 shebang 交給 uv 自動備環境", file=sys.stderr)
    print(
        f"    pip install {' '.join(_missing_packages)}"
        "    # 或手動裝進當前的 interpreter",
        file=sys.stderr,
    )
    sys.exit(1)

import argparse
import atexit
import datetime
import collections
import math
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from pubsub import pub
import meshtastic
import meshtastic.ble_interface
import meshtastic.serial_interface
import meshtastic.tcp_interface
import meshtastic.util
from meshtastic.protobuf import config_pb2

RULES_FILE = Path(__file__).parent / "rules.txt"

DEFAULT_RULES = """\
# Auto-reply rules, grouped by channel.
#
#   [channel]           the channel the rules below it apply to, given either
#                       as its Meshtastic name ([EDGE_ATS]) or its index
#                       ([#0] - useful for the unnamed primary channel).
#                       [*] applies to every channel except those listed
#                       under [!exclude].
#   [!exclude]          channels [*] must NOT fire on, one per line, by name or
#                       as #index. Blanket rules are how a bot becomes a
#                       nuisance on a busy public channel; an excluded channel
#                       answers only from rules written for it explicitly.
#                       Direct messages are unaffected - a DM is addressed to
#                       you, so [*] still applies to it.
#   [DM]                rules for direct messages. Tried before the channel the
#                       DM arrived on, so a keyword can be answered differently
#                       in private, or left out here and shared with the
#                       channel. A reply to a DM goes back as a DM.
#   keyword=reply text  one rule per line. The whole message must equal the
#                       keyword exactly, case included - only surrounding
#                       whitespace is ignored. So A=Alpha answers "A" but not
#                       "a" and not "AAA". The reply text is taken literally,
#                       so do not quote it.
#
# The first matching rule wins, and a channel's own rules are checked before
# [*]. Blank lines and lines starting with # are ignored.
#
# Messages from other nodes are answered, and so are ones typed on this device,
# so a keyword can be tried without a second radio. Anything starting with
# "BOT: " is never answered - that is what stops a reply drawing a reply. Each
# message is answered at most once, even if the mesh redelivers it.
#
# Rules placed before the first [channel] header apply to EVERY channel, which
# is how the old flat format keeps working - but that will auto-reply on public
# channels too, so prefer an explicit header.

# Busy or public channels, where a blanket [*] rule would answer everybody.
# Delete a line to let [*] apply there again.
[!exclude]
SignalTest
Emergency!
LongFast
MediumFast
MeshTW

[DM]
ping=pong (private)

[EDGE_ATS]
ping=pong
help=指令: ping
"""

BROADCAST_ADDR = "^all"

# rules.txt section that applies to every channel.
ALL_CHANNELS = "*"

# rules.txt section for direct messages. A channel actually named "DM"
# would share it, which is accepted as the price of a readable name.
DM_SECTION = "DM"

# rules.txt section listing channels that [*] must not apply to, one per line
# rather than as keyword=reply pairs. The "!" makes it unmistakably not a
# channel name - Meshtastic names do not start with one, and "!" already reads
# as "not a channel" here because node ids wear it.
EXCLUDE_SECTION = "!exclude"

# Device-list items are keyed "<transport>:<address>", mirroring the "kind:key"
# scheme the targets list already uses, so one list can hold both transports
# and connect_device knows which interface class to build.
TRANSPORT_BLE = "ble"
TRANSPORT_TCP = "tcp"
TRANSPORT_SERIAL = "serial"

# What the detached copy of ourselves must be told to come up headless.
# Nothing to add: this file is only ever the server, so the background copy
# needs no flag to suppress a UI. See bot_dual.py, which passes --server here.
HEADLESS_FLAGS: list[str] = []

# Meshtastic's socket API port. Firmware only accepts one TCP client at a
# time and force-closes the previous one, so connecting here kicks off a
# phone/desktop app already talking to the same node over WiFi.
DEFAULT_TCP_PORT = 4403


def load_rules() -> dict[str, dict[str, str]]:
    """Re-read rules.txt every call so edits take effect without restarting the bot.

    Returns {section: {keyword: reply}}. A section is a channel name, "#<index>",
    ALL_CHANNELS, DM_SECTION, or EXCLUDE_SECTION. Rules appearing before the
    first [header] land in ALL_CHANNELS, which is what keeps a flat pre-sections
    file working.

    EXCLUDE_SECTION holds channel names as keys with empty values, since its
    lines are a list rather than pairs - see excluded_channels().
    """
    if not RULES_FILE.exists():
        RULES_FILE.write_text(DEFAULT_RULES, encoding="utf-8")

    rules: dict[str, dict[str, str]] = {}
    section = ALL_CHANNELS
    for raw_line in RULES_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            # A bare "#0" under [!exclude] is a channel index, not a comment.
            # It has to be: the primary channel is normally unnamed, so its
            # index is the only way to name it at all - and the primary is
            # usually the public one you most want excluded. Anything else
            # beginning with "#" stays a comment, "# 主頻道" included.
            if not (section == EXCLUDE_SECTION and line[1:].isdigit()):
                continue
            # Normalised, so "#00" and "#0" mean the same channel.
            line = f"#{int(line[1:])}"
        # Checked before the "=" test so a header is never mistaken for a rule.
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip() or ALL_CHANNELS
            rules.setdefault(section, {})
            continue
        if "=" not in line:
            # [!exclude] is a list of channels, not keyword=reply pairs, so it
            # is the one section where a bare line means something. Everywhere
            # else a line without "=" is a typo, and skipping it quietly is the
            # long-standing behaviour.
            if section == EXCLUDE_SECTION:
                rules.setdefault(section, {})[line] = ""
            continue
        keyword, _, reply = line.partition("=")
        keyword = keyword.strip()
        if keyword:
            rules.setdefault(section, {})[keyword] = reply.strip()
    return rules


def _split_device_key(device_key: str) -> tuple[str, str]:
    """Split a "<transport>:<address>" device-list key.

    A bare address (no prefix) is treated as BLE so an older saved key, or a
    caller that has not been updated, still connects the way it used to.
    Partitioning from the left keeps IPv6 literals and "host:port" intact.
    """
    transport, sep, address = device_key.partition(":")
    if not sep:
        return TRANSPORT_BLE, device_key
    if transport not in (TRANSPORT_BLE, TRANSPORT_TCP, TRANSPORT_SERIAL):
        return TRANSPORT_BLE, device_key
    return transport, address


# IUGG mean Earth radius. Node separations here are tens of kilometres at most,
# where a spherical model is well inside the error of a LoRa-reported position.
EARTH_RADIUS_M = 6371008.8


def node_position(node: dict) -> tuple[float, float] | None:
    """(latitude, longitude) in degrees from a nodeDB entry, or None.

    Meshtastic stores degrees as integers scaled by 1e7. None covers three cases
    that all mean "no usable fix": no position dict at all, a position dict
    carrying only a timestamp (what a node with GPS enabled but no fix reports),
    and an exact 0,0 - which is a placeholder in practice, not a real position in
    the Gulf of Guinea.
    """
    position = node.get("position") or {}
    lat_i, lon_i = position.get("latitudeI"), position.get("longitudeI")
    if lat_i is None or lon_i is None:
        return None
    if lat_i == 0 and lon_i == 0:
        return None
    return lat_i / 1e7, lon_i / 1e7


def open_interface(transport: str, address: str):
    """Build and connect the meshtastic interface for one transport.

    Shared by the TUI and the one-shot --wifi path so both resolve addresses and
    pick interface classes identically.
    """
    if transport == TRANSPORT_TCP:
        hostname, port = _split_host_port(address)
        return meshtastic.tcp_interface.TCPInterface(hostname=hostname, portNumber=port)
    if transport == TRANSPORT_SERIAL:
        return meshtastic.serial_interface.SerialInterface(devPath=address)
    return meshtastic.ble_interface.BLEInterface(address=address)


def parse_latlon(text: str) -> tuple[float, float]:
    """Parse "lat,lon" for --here, raising argparse's error type on bad input."""
    parts = text.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"expected LAT,LON - got {text!r}")
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected two numbers - got {text!r}") from None
    if not -90 <= lat <= 90:
        raise argparse.ArgumentTypeError(f"latitude out of range: {lat}")
    if not -180 <= lon <= 180:
        raise argparse.ArgumentTypeError(f"longitude out of range: {lon}")
    return lat, lon


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon pairs, in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def format_distance(metres: float | None) -> str:
    """Compact distance for the node list; "--" when it cannot be computed."""
    if metres is None:
        return "--"
    if metres < 1000:
        return f"{metres:.0f}m"
    return f"{metres / 1000:.1f}km"


def _split_host_port(address: str) -> tuple[str, int]:
    """Split "host:port" into its parts, defaulting to Meshtastic's TCP port.

    Only splits a trailing ":<digits>", and only when the remainder has no
    colon of its own, so a bare IPv6 literal passes through untouched.
    """
    host, sep, port = address.rpartition(":")
    if sep and port.isdigit() and ":" not in host and host:
        return host, int(port)
    return address, DEFAULT_TCP_PORT


def excluded_channels() -> set[str]:
    """Channels listed under [!exclude], as names and/or "#index" forms.

    Read from the file on each call, like the rules themselves: editing
    rules.txt takes effect without a restart, and this has to follow the same
    rule or the exclusions would silently lag behind the rules they modify.
    """
    return set(load_rules().get(EXCLUDE_SECTION, {}))


def is_excluded(name: str | None, index: int | None) -> bool:
    """Whether channel `name`/`index` is listed under [!exclude].

    Either form matches, so a channel can be excluded by the name it is
    configured with or by its index - the same two ways a section addresses it.
    """
    excluded = excluded_channels()
    if not excluded:
        return False
    if name and name in excluded:
        return True
    return index is not None and f"#{index}" in excluded


def find_reply(text: str, sections: list[str]) -> str | None:
    """Reply for `text` if some rule's keyword matches it exactly.

    The whole message must equal the whole keyword, case included. Only
    surrounding whitespace is ignored.

    Both halves of that are deliberate. This used to be a case-insensitive
    substring test, which a short-keyword rules file makes unusable: with the
    NATO alphabet (A=Alpha ... Z=Zulu) almost any message contains some single
    letter, so "hello" answered "Echo" and "Bravo" answered "Alpha". Matching
    case as well means "a" no longer triggers the rule written as "A".

    The caller passes the channel's own sections before ALL_CHANNELS, so a
    channel-specific rule beats a catch-all one for the same keyword.
    """
    message = text.strip()
    if not message:
        return None
    rules = load_rules()
    for section in sections:
        # EXCLUDE_SECTION holds channel names with empty replies, so a message
        # that happens to equal one of them would be "answered" with nothing at
        # all - the bot putting an empty line on the mesh. Nothing puts that
        # section in `sections` today; this is what makes it harmless if
        # something ever does.
        if section == EXCLUDE_SECTION:
            continue
        for keyword, reply in rules.get(section, {}).items():
            if keyword.strip() == message:
                return reply
    return None


def format_elapsed(seconds: float) -> str:
    """Seconds as H:MM:SS, for how long this program has been running.

    Deliberately not format_uptime, which reports the *node's* uptime: that one
    stops at minutes and says "--" when the figure is missing, both right for a
    value polled every five seconds and sometimes absent. This one ticks every
    second and always has a value, so it shows seconds and starts at 0:00:00.

    Hours are not wrapped into days either: this runs for days at a time and
    "51:03:12" reads unambiguously, where "2d 3:03:12" needs a moment's
    arithmetic to compare against a log timestamp.
    """
    total = int(max(seconds, 0))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def packet_node_id(packet: dict, id_key: str, num_key: str) -> str | None:
    """A node id for one end of a packet, rebuilt from the node number if needed.

    The library fills fromId/toId by looking the node number up in its node
    database, and leaves the key set to None when it is not there yet. That is
    the normal state for the first messages after connecting - the node list
    arrives as its own config phase, and over BLE it is slow enough that real
    traffic beats it - so a plain packet.get() would render an unresolved
    sender as "None" or a placeholder. The node number is in the packet
    regardless, so reconstruct the "!hex" id the library would have produced.
    """
    node_id = packet.get(id_key)
    if node_id:
        return node_id
    num = packet.get(num_key)
    if isinstance(num, int) and num:
        return f"!{num:08x}"
    return None


def parse_incoming(packet: dict, my_id: str | None) -> dict | None:
    """Return a dict of the fields we care about for a text-message packet, or None.

    target is a (kind, key) tuple: ("channel", index) for a broadcast, or
    ("node", other_party_id) for a direct message - key is always the *other*
    side of the conversation, whether this packet was incoming or our own
    outgoing echo.
    """
    decoded = packet.get("decoded")
    if not decoded or decoded.get("portnum") != "TEXT_MESSAGE_APP":
        return None

    rx_time = packet.get("rxTime")
    when = (
        datetime.datetime.fromtimestamp(rx_time).strftime("%H:%M:%S") if rx_time else "??:??:??"
    )

    to_id = packet_node_id(packet, "toId", "to") or BROADCAST_ADDR
    from_id = packet_node_id(packet, "fromId", "from") or "?"
    if to_id == BROADCAST_ADDR:
        target = ("channel", packet.get("channel", 0))
    else:
        other = to_id if from_id == my_id else from_id
        target = ("node", other)

    return {
        "text": decoded.get("text", ""),
        # Kept for a DM too: a direct message still travels on a channel, and
        # its rules fall back to that channel's when [DM] has no match.
        "channel": packet.get("channel", 0),
        "from_id": from_id,
        "to_id": to_id,
        "target": target,
        "when": when,
        "transport": "MQTT" if packet.get("viaMqtt") else "LoRa",
        "snr": packet.get("rxSnr"),
        "rssi": packet.get("rxRssi"),
        "id": packet.get("id"),
    }


def node_label(nodes: dict, node_id: str) -> str:
    """Readable name for a node id: its short name, else long name, else the id.

    Names arrive as separate NodeInfo packets, so a node we have heard a message
    from may not have one yet - hence the fall back to the raw id rather than
    showing nothing.
    """
    user = (nodes.get(node_id) or {}).get("user") or {}
    return user.get("shortName") or user.get("longName") or node_id


def format_incoming_line(info: dict, sender: str | None = None, markup: bool = True) -> str:
    """Render a parsed incoming message as one RichLog line.

    Shaped "12:34:56 Bug2[!f2dcbabe](LoRa snr=6.5 rssi=-92): ping" - name, then
    its node id in brackets, then the transport and signal. The id is not
    repeated when no name resolved, since the name has already fallen back to it.

    The brackets are escaped: RichLog reads this as markup, where "[" opens a
    style tag, so an unescaped "[!f2dcbabe]" would be parsed as one instead of
    printed.
    """
    node_id = info["from_id"]
    if sender and sender != node_id:
        who = f"[bold]{sender}[/bold][dim]\\[{node_id}][/dim]" if markup else f"{sender}[{node_id}]"
    else:
        who = f"[bold]{node_id}[/bold]" if markup else str(node_id)
    when = f"[dim]{info['when']}[/dim]" if markup else info["when"]
    line = f"{when} {who}({info['transport']}"
    if info["snr"] is not None:
        line += f" snr={info['snr']}"
    if info["rssi"] is not None:
        line += f" rssi={info['rssi']}"
    line += f"): {info['text']}"
    return line


BOT_REPLY_PREFIX = "BOT: "


def build_reply_text(reply: str, info: dict) -> str:
    """Two lines: the rule's reply, then the message details in brackets.

        BOT: pong
        [12:34:56 from=Bug2 rx=LoRa snr=6.5 rssi=-92 dist=842m]

    Fields that cannot be determined are left out rather than sent as "--": the
    text payload is tight, and a field that is usually blank costs bytes on
    every reply while telling the recipient nothing.

    "from" carries the sender's name when one is known, matching the message
    pane, and falls back to the node id.

    The "BOT: " prefix and the bracketed detail line also keep the bot from
    answering itself: matching is exact, so a reply can never equal a keyword.
    """
    who = info.get("from_name") or info["from_id"]
    # "rx" rather than "via": every field here describes the message being
    # answered, not the reply, and rx reads as reception - matching rxSnr and
    # rxRssi, which is where snr and rssi come from.
    bits = [info["when"], f"from={who}", f"rx={info['transport']}"]
    if info["snr"] is not None:
        bits.append(f"snr={info['snr']}")
    if info["rssi"] is not None:
        bits.append(f"rssi={info['rssi']}")
    if info.get("distance_m") is not None:
        bits.append(f"dist={format_distance(info['distance_m'])}")
    return f"{BOT_REPLY_PREFIX}{reply}\n[{' '.join(bits)}]"


class ReplyEngine:
    """The rules engine, shared by the TUI and the headless server.

    Everything here works without a widget - deciding and sending replies,
    resolving the far end, pacing reconnects - so both front ends inherit it
    unchanged and a rule that works in one works in the other. Implementors
    provide `interface`, `my_id`, `here`, `tcp_host`, `history`, `_replied_ids`
    and `sent_auto_count`; the TUI has them as UI state, the server keeps its own.
    """

    # Whether log lines this class produces are Rich markup. True for the TUI,
    # which renders them in a RichLog; False for the server, whose output is a
    # plain stream where the tags would be printed literally.
    MARKUP = True

    # Seconds between reconnect attempts. Quick at first so a momentary WiFi
    # blip recovers almost immediately, then settling into a steady poll rather
    # than hammering a node that is powered off or rebooting.
    RECONNECT_DELAYS = (1, 2, 5, 10, 30)

    @classmethod
    def _reconnect_delay(cls, attempt: int) -> int:
        """Seconds to wait before reconnect attempt `attempt`, counting from 1.

        Anything past the end of the table repeats the last value, so the
        retries continue at a fixed interval instead of growing without bound.
        """
        index = min(max(attempt, 1), len(cls.RECONNECT_DELAYS)) - 1
        return cls.RECONNECT_DELAYS[index]

    def _describe_peer(self, interface, transport: str) -> str | None:
        """Address of the far end, for the status pane.

        TCP asks the socket for the resolved peer, so connecting by a hostname
        like Meshtastic.local still reports the IP actually reached. The port is
        only shown when it is not the default, to keep the narrow pane readable.
        BLE keeps its address on the inner client rather than the interface.
        """
        if transport == TRANSPORT_TCP:
            sock = getattr(interface, "socket", None)
            try:
                host, port = sock.getpeername()[:2]
            except Exception:  # noqa: BLE001
                # Socket already gone, or a stub without getpeername - fall back
                # to whatever we were asked to connect to.
                return getattr(interface, "hostname", None) or self.tcp_host
            return host if port == DEFAULT_TCP_PORT else f"{host}:{port}"
        return getattr(getattr(interface, "client", None), "address", None)

    def _distance_to(self, node_id: str | None) -> float | None:
        """Distance from this station to `node_id`, or None if either end lacks
        a position.

        Returns None for our own id too: a reply to a message typed here would
        otherwise carry a trivially true "dist=0m".
        """
        if not node_id or node_id == self.my_id or self.interface is None:
            return None
        nodes = self.interface.nodes or {}
        here = self.here or node_position(nodes.get(self.my_id, {}))
        there = node_position(nodes.get(node_id, {}))
        if here is None or there is None:
            return None
        return haversine_m(*here, *there)

    def _channel_name(self, index: int) -> str | None:
        """The channel's configured name, or None if it has none (the primary
        channel is typically unnamed - address it as [#0] in rules.txt)."""
        for ch in self.interface.localNode.channels or []:
            if ch.index == index and ch.settings:
                return ch.settings.name or None
        return None

    def _reply_sections(self, target: tuple, channel: int | None) -> list[str]:
        """rules.txt sections to search for `target`, most specific first.

        A channel message uses that channel's sections. A direct message tries
        [DM] first and then falls back to the sections of the channel it arrived
        on, so a rule can be written for private messages specifically or simply
        shared with the channel. `channel` is None when it is not known - a DM
        typed here, where the target is a node and no packet was received - and
        then only [DM] and [*] apply.

        [!exclude] applies to channel messages only; see below.
        """
        kind, key = target
        if kind == "channel":
            return self._channel_sections(key)
        if channel is None:
            return [DM_SECTION, ALL_CHANNELS]
        # [!exclude] is about broadcast traffic: it stops the bot answering
        # everyone on a busy channel. A direct message is addressed to us
        # personally, so [*] still applies to it even when the channel it
        # happened to travel on is excluded.
        sections = self._channel_sections(channel)
        if ALL_CHANNELS not in sections:
            sections.append(ALL_CHANNELS)
        return [DM_SECTION] + sections

    def _channel_sections(self, index: int) -> list[str]:
        """rules.txt sections that apply to channel `index`, most specific first.

        [*] is left out for a channel listed under [!exclude]. It is the blanket
        default, and a blanket default on a busy public channel is how a bot
        becomes a nuisance - so an excluded channel answers only from rules
        written for it by name or index.
        """
        sections = []
        name = self._channel_name(index)
        if name:
            sections.append(name)
        sections.append(f"#{index}")
        if not is_excluded(name, index):
            sections.append(ALL_CHANNELS)
        return sections

    def _known_channel_sections(self) -> set[str]:
        """Every section name that would match one of this node's channels, so
        _config_synced can warn about rules aimed at a channel that is not
        configured here (a typo'd name, or a channel on another node)."""
        known = {ALL_CHANNELS}
        for ch in self.interface.localNode.channels or []:
            if not ch.settings:
                continue
            known.add(f"#{ch.index}")
            if ch.settings.name:
                known.add(ch.settings.name)
        return known

    # How many recent packet ids to remember for the once-per-message rule.
    # Comfortably more than a mesh can redeliver in the window that matters,
    # while staying trivial in memory.
    REPLIED_ID_LIMIT = 256

    def _should_auto_reply(self, info: dict) -> bool:
        """Whether this *received* packet earns an auto-reply.

        Three gates.

        Never answer a bot reply. Every reply starts with BOT_REPLY_PREFIX, so
        this is what stops the bot answering itself into an endless exchange. It
        is deliberately an explicit check rather than relying on the fact that
        exact matching cannot match a two-line reply - that holds today, but it
        is a subtle property to leave a loop resting on.

        Never answer our own echo. Text typed here is answered directly from
        on_input_submitted, so if the radio also echoes it back, replying again
        would double up.

        And only once per packet, since the mesh redelivers the same message via
        rebroadcast or the MQTT bridge.
        """
        if info["text"].lstrip().startswith(BOT_REPLY_PREFIX):
            return False

        if info["from_id"] == self.my_id:
            return False

        packet_id = info.get("id")
        if packet_id is None:
            # Nothing to deduplicate on. Answering is the lesser failure - a
            # duplicate reply beats silently ignoring a real message.
            return True
        if packet_id in self._replied_ids:
            return False

        self._replied_ids[packet_id] = True
        while len(self._replied_ids) > self.REPLIED_ID_LIMIT:
            self._replied_ids.pop(next(iter(self._replied_ids)))
        return True

    def _maybe_auto_reply(
        self,
        interface,
        target: tuple,
        text: str,
        when: str,
        from_id: str,
        transport: str,
        snr: float | None = None,
        rssi: int | None = None,
        channel: int | None = None,
    ) -> str | None:
        """Send + record the keyword auto-reply for `text` if a rule matches it
        exactly.

        `channel` is the index the message arrived on. It only matters for a
        direct message, whose rules fall back to that channel's - see
        _reply_sections.

        Called only from on_receive, and only once the _should_auto_reply gates
        have passed. Returns the reply line to display, or None if nothing was
        sent - the caller writes it to the log itself, since on_receive is on
        meshtastic's thread and needs call_from_thread.
        """
        kind, key = target
        reply = find_reply(text, self._reply_sections(target, channel))
        if reply is None:
            return None
        info_like = {
            "when": when,
            "transport": transport,
            "snr": snr,
            "rssi": rssi,
            "from_id": from_id,
            "from_name": node_label(interface.nodes or {}, from_id),
            "distance_m": self._distance_to(from_id),
        }
        full_reply = build_reply_text(reply, info_like)
        if kind == "channel":
            interface.sendText(full_reply, channelIndex=key)
        else:
            interface.sendText(full_reply, destinationId=key)
        self.sent_auto_count += 1

        # The sent text carries literal brackets and a newline. For the TUI the
        # brackets have to be escaped, since RichLog reads the line as markup and
        # would otherwise parse "[12:34:56 ...]" as a style tag and drop it; for
        # a plain stream they must be left exactly as sent. Either way the
        # newline is folded, so one reply stays one log line.
        flat = full_reply.replace("\n", " ")
        if self.MARKUP:
            reply_line = f"[yellow]  -> auto-reply: {flat.replace('[', chr(92) + '[')}[/yellow]"
        else:
            reply_line = f"  -> auto-reply: {flat}"
        self.history.setdefault(target, []).append(reply_line)
        return reply_line


class _BoundedHistory(dict):
    """The server's stand-in for the TUI's message history.

    ReplyEngine records every reply it sends so the TUI can redraw a pane you
    switch back to. The server has no pane, but it also has no natural end - it
    is meant to stay up for weeks - so an unbounded list per target would be a
    slow leak. Each target gets a short deque instead; the recording code is
    unchanged, since deque.append works the same way.
    """

    LIMIT = 50

    def setdefault(self, key, default=None):
        if key not in self:
            super().__setitem__(key, collections.deque(maxlen=self.LIMIT))
        return self[key]


class ServerBot(ReplyEngine):
    """Headless auto-reply server: the same rules engine with no UI.

    Reply behaviour is inherited from ReplyEngine rather than reimplemented, so
    a rule that answers in the TUI answers here identically. What differs is
    everything around it - one timestamped line per event on a stream instead of
    panes, and a plain blocking wait instead of Textual's event loop, so the
    process can be detached and left running as a service.
    """

    MARKUP = False

    def __init__(
        self,
        here: tuple[float, float] | None = None,
        heartbeat: int = 600,
        out=None,
    ) -> None:
        self.here = here
        self.heartbeat = heartbeat
        # Resolved at write time rather than captured here, so daemonising after
        # construction still lands in the redirected stream.
        self._out = out
        self.interface = None
        self.transport: str | None = None
        self.peer: str | None = None
        self.tcp_host: str | None = None
        self.my_id: str | None = None
        self.history = _BoundedHistory()
        self._replied_ids: dict = {}
        self.sent_auto_count = 0
        self.received_count = 0
        self.started_at = time.monotonic()
        # As in the TUI's status bar: text messages are rare, everything else is
        # not, so this is the number that shows the link is still carrying
        # something. In a heartbeat line it is the difference between "quiet
        # mesh" and "we stopped hearing anything".
        self.packet_count = 0
        self.connected_key: str | None = None
        self.link_down = False
        self._closing = False
        self.reconnect_attempt = 0
        self.reconnect_total = 0
        self._stopped = threading.Event()

    # ---- output -----------------------------------------------------------

    def log(self, text: str) -> None:
        """One timestamped line, flushed.

        Full dates, not the bare clock the TUI shows: this output is normally a
        file read days later, where "14:07:03" alone does not say which day.
        Flushed per line for the same reason - a log being tailed is no use if
        it arrives a block at a time.
        """
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{stamp} {text}", file=self._out or sys.stdout, flush=True)

    # ---- link -------------------------------------------------------------

    def _adopt(self, interface, transport: str) -> None:
        self.interface = interface
        self.transport = transport
        self.peer = self._describe_peer(interface, transport)
        where = f" ({self.peer})" if self.peer else ""
        self.log(f"{transport.upper()} 已連線{where},等待設定同步...")

    def on_connection_lost(self, interface) -> None:
        """Start reconnecting, for the reason the TUI does the same: the
        library's reader thread ends for good on an error, so without this the
        server sits there believing it is connected and hearing nothing."""
        if self._closing or interface is not self.interface or self.link_down:
            return
        self.link_down = True
        self.reconnect_attempt = 0
        self.log("連線中斷,自動重連中...")
        threading.Thread(target=self._reconnect_loop, daemon=True).start()

    def _reconnect_loop(self) -> None:
        """Reopen the dropped link, backing off between attempts, for as long as
        the process is up. The usual causes - a node reboot, a WiFi blip, its
        single TCP slot taken by a phone - all clear on their own, and a server
        that gives up needs attention at exactly the moment it should not.

        Waits on the stop event rather than sleeping, so shutdown during a
        30-second backoff is immediate.
        """
        key = self.connected_key
        if key is None:
            return
        transport, address = _split_device_key(key)
        attempt = 0
        while not self._closing and self.link_down:
            attempt += 1
            self.reconnect_attempt = attempt
            self.reconnect_total += 1
            if self._stopped.wait(self._reconnect_delay(attempt)):
                return
            if self._closing or not self.link_down:
                return
            self.log(f"重連中(第 {attempt} 次)...")
            try:
                interface = open_interface(transport, address)
            except Exception as e:  # noqa: BLE001
                self.log(f"重連失敗: {e}")
                continue
            self.link_down = False
            self._adopt(interface, transport)
            return

    # ---- pubsub handlers --------------------------------------------------

    def on_config_synced(self, interface, topic=pub.AUTO_TOPIC) -> None:
        # Adopted here as well as in _adopt(). The library publishes
        # connection.established from its own thread while open_interface() is
        # still constructing the interface, so this can arrive before run() has
        # had the chance to assign self.interface - and _known_channel_sections
        # below reads it. The interface that published is ours by definition.
        self.interface = interface
        my_user = interface.getMyUser() or {}
        self.my_id = my_user.get("id")
        name = my_user.get("longName") or my_user.get("shortName") or "?"
        self.log(f"設定同步完成: {name} {self.my_id}")
        channels = [
            f"#{ch.index}"
            + (f" {ch.settings.name}" if ch.settings.name else "")
            for ch in (interface.localNode.channels or [])
            if ch.settings
        ]
        self.log(f"頻道: {', '.join(channels) if channels else '(無)'}")
        rules = load_rules()
        summary = ", ".join(f"[{sec}]={len(r)}" for sec, r in rules.items())
        self.log(f"規則: {summary if summary else '(無)'}")
        # The same warning the TUI gives: a section aimed at a channel this node
        # does not have will never fire, and a typo looks exactly like that.
        unknown = sorted(set(rules) - self._known_channel_sections() - {DM_SECTION})
        if unknown:
            self.log(
                "注意: 這些區段對應不到本機頻道,不會生效: "
                + ", ".join(unknown)
            )

    def on_receive(self, packet, interface) -> None:
        # Before the text filter, for the reason the TUI does the same.
        self.packet_count += 1
        info = parse_incoming(packet, self.my_id)
        if info is None:
            return
        sender = node_label(interface.nodes or {}, info["from_id"])
        kind, key = info["target"]
        self.log(f"{kind}:{key} {format_incoming_line(info, sender, markup=False)}")
        if info["from_id"] != self.my_id:
            self.received_count += 1
        if not self._should_auto_reply(info):
            return
        reply_line = self._maybe_auto_reply(
            interface,
            info["target"],
            info["text"],
            when=info["when"],
            from_id=info["from_id"],
            transport=info["transport"],
            snr=info["snr"],
            rssi=info["rssi"],
            channel=info["channel"],
        )
        if reply_line:
            self.log(reply_line)

    # ---- lifecycle --------------------------------------------------------

    def _heartbeat_line(self) -> str:
        state = "連線中斷" if self.link_down else "已連線"
        return (
            f"[心跳] {state} 執行 {format_elapsed(time.monotonic() - self.started_at)}"
            f" 封包 {self.packet_count}"
            f" 收訊 {self.received_count}"
            f" 自動回覆 {self.sent_auto_count}"
            f" 重連 {self.reconnect_total}"
        )

    # How long to give the interface to close before leaving without it. BLE
    # teardown on macOS can block indefinitely; measured against a real node,
    # SIGTERM woke the wait immediately and then close() never returned, so the
    # process needed SIGKILL. Whatever the library is waiting for, a server told
    # to stop has to stop.
    CLOSE_TIMEOUT = 5

    def stop(self, *_) -> None:
        """Unblock run(). Safe from a signal handler or another thread."""
        self._closing = True
        self._stopped.set()

    def _close_quietly(self, interface) -> None:
        try:
            interface.close()
        except Exception as exc:  # noqa: BLE001
            self.log(f"關閉介面時出錯,忽略: {exc}")

    def _shutdown(self, interface) -> None:
        """Close the interface, but not at any price.

        On a side thread with a deadline, because close() is the call that was
        observed hanging. The thread is a daemon, so a close that never finishes
        cannot keep the process alive either.
        """
        self._closing = True
        closer = threading.Thread(
            target=self._close_quietly, args=(interface,), daemon=True
        )
        closer.start()
        closer.join(self.CLOSE_TIMEOUT)
        if closer.is_alive():
            self.log(f"介面關閉逾時 ({self.CLOSE_TIMEOUT}s),不再等待")

    def run(self, transport: str, address: str) -> int:
        """Connect and serve until stopped. Returns a process exit code."""
        pub.subscribe(self.on_receive, "meshtastic.receive")
        pub.subscribe(self.on_config_synced, "meshtastic.connection.established")
        pub.subscribe(self.on_connection_lost, "meshtastic.connection.lost")

        if transport == TRANSPORT_TCP:
            self.tcp_host = address
        self.log(f"連線 {transport}:{address} ...")
        try:
            interface = open_interface(transport, address)
        except Exception as e:  # noqa: BLE001
            self.log(f"連線失敗: {e}")
            return 1
        self.connected_key = f"{transport}:{address}"
        self._adopt(interface, transport)

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self.stop)
        # The library drives everything from its own reader and publishing
        # threads, so there is nothing for this one to do but wait - on an event
        # rather than a bare sleep, so a signal ends it at once instead of after
        # the current interval. heartbeat=0 waits indefinitely, logging only
        # what actually happens.
        interval = self.heartbeat if self.heartbeat > 0 else None
        while not self._stopped.wait(interval):
            self.log(self._heartbeat_line())

        self.log("停止中...")
        self._shutdown(interface)
        self.log(f"已停止。{self._heartbeat_line()}")
        return 0


def list_devices() -> int:
    """Print every node we could connect to right now, then exit.

    resolve_server_target() already scans, but only as a step towards asking
    which one you want. This answers "what is out there" on its own - which is
    what you actually need before writing a --ble name into a launchd plist, or
    when a node has stopped appearing and you want to know whether it is
    advertising at all.

    Printed as the flags you would pass, so a line can go straight onto a
    command line.
    """
    print("掃描 BLE(約 10 秒)...", file=sys.stderr)
    try:
        devices = meshtastic.ble_interface.BLEInterface.scan()
    except Exception as e:  # noqa: BLE001
        print(f"BLE 掃描失敗: {e}", file=sys.stderr)
        return 1

    # scan() filters on Meshtastic's service UUID, so everything here is a node
    # rather than every bluetooth thing in the room.
    print(f"BLE 節點 ({len(devices)}):")
    for device in devices:
        address = getattr(device, "address", "") or ""
        print(f"  --ble {device.name}" + (f"    {address}" if address else ""))
    if not devices:
        print("  (沒有節點在廣播 - 已經連上手機的節點通常就不廣播了)")

    # findPorts prefers known USB-serial vendor ids and falls back to listing
    # everything not blacklisted, so this is "likely a node", not "any port".
    ports = meshtastic.util.findPorts(True)
    print(f"\nUSB serial ({len(ports)}):")
    for port in ports:
        print(f"  --port {port}")
    if not ports:
        print("  (沒有接上的裝置)")
    return 0


def resolve_server_target(
    tcp_host: str | None, serial_port: str | None, ble_address: str | None
) -> tuple[str, str] | None:
    """Pick the node for server mode: one named target, or a numbered choice.

    A single --host/--port/--ble is taken as the answer, which is what makes
    unattended startup possible at all. Otherwise the candidates are listed -
    named targets first, then a BLE scan - and the choice read from stdin, since
    drawing a chooser is the one thing this mode exists not to do.

    Prompts on stderr so that `bot.py --server > log` still shows them.
    """
    targets: list[tuple[str, str]] = []
    if tcp_host:
        targets.append((TRANSPORT_TCP, tcp_host))
    if serial_port:
        targets.append((TRANSPORT_SERIAL, serial_port))
    if ble_address:
        targets.append((TRANSPORT_BLE, ble_address))
    if len(targets) == 1:
        return targets[0]

    if not ble_address:
        print("掃描 BLE 裝置...", file=sys.stderr)
        try:
            for d in meshtastic.ble_interface.BLEInterface.scan():
                targets.append((TRANSPORT_BLE, d.name))
        except Exception as e:  # noqa: BLE001
            print(f"BLE 掃描失敗: {e}", file=sys.stderr)
    if not targets:
        print(
            "找不到任何裝置。用 --port / --host / --ble 指定。",
            file=sys.stderr,
        )
        return None

    for i, (transport, address) in enumerate(targets, 1):
        print(f"  {i}) {transport.upper():6} {address}", file=sys.stderr)
    while True:
        try:
            choice = input(f"選擇裝置 [1-{len(targets)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(targets):
            return targets[int(choice) - 1]
        print("請輸入清單上的編號。", file=sys.stderr)


def detached_argv(target: tuple[str, str], here, heartbeat: int) -> list[str]:
    """The command line for the background copy of ourselves.

    The device is passed explicitly because the parent has already chosen it -
    the child must never reach the interactive picker, since it has no terminal.
    --daemon is deliberately *not* forwarded: a child that daemonised again
    would spawn another child, and so on.
    """
    transport, address = target
    flag = {
        TRANSPORT_TCP: "--host",
        TRANSPORT_SERIAL: "--port",
        TRANSPORT_BLE: "--ble",
    }[transport]
    argv = [
        sys.executable,
        os.path.abspath(__file__),
        *HEADLESS_FLAGS,
        flag,
        address,
        "--heartbeat",
        str(heartbeat),
    ]
    if here:
        argv += ["--here", f"{here[0]},{here[1]}"]
    return argv


def spawn_detached(argv: list[str], log_path: Path) -> int:
    """Start `argv` in its own session, output appended to `log_path`.

    A fresh process rather than a fork. By the time this runs, importing
    meshtastic has already started its "publishing" thread - the one that hands
    every received packet to its subscribers - and fork() carries only the
    calling thread. A forked child would connect successfully and then never
    process a single message. Re-executing pays for the imports twice and gets a
    process whose threads are all actually there.

    start_new_session detaches it from the controlling terminal, so closing the
    shell does not take the server with it.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log, open(os.devnull, "rb") as devnull:
        child = subprocess.Popen(
            argv, stdin=devnull, stdout=log, stderr=log, start_new_session=True
        )
    print(f"背景執行中 pid={child.pid}, log: {log_path}", file=sys.stderr)
    print(f"停止: kill {child.pid}", file=sys.stderr)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Headless Meshtastic auto-reply server: answers messages "
        "from rules.txt with no UI, one log line per event. Give it one of "
        "--port / --host / --ble to start straight away, or none to pick from "
        "a list. Stop it with SIGTERM.",
    )
    parser.add_argument(
        "--host",
        metavar="HOST[:PORT]",
        help="this node over TCP, e.g. Meshtastic.local or 192.168.0.247. "
        f"Port defaults to {DEFAULT_TCP_PORT}.",
    )
    parser.add_argument(
        "--here",
        metavar="LAT,LON",
        type=parse_latlon,
        help="this station's position, used for the dist= field in replies. "
        "Only needed when the connected node has no GPS fix. Stays local - it "
        "is never sent to the device or the mesh.",
    )
    parser.add_argument(
        "--port",
        metavar="PATH",
        help="this node over USB serial, e.g. /dev/cu.usbmodem2101. The only "
        "transport that still works once the node's WiFi is off and its "
        "Bluetooth is disabled, which is the normal state on MUI/TFT boards.",
    )
    parser.add_argument(
        "--ble",
        metavar="NAME",
        help="this BLE node, by name, e.g. Meshtastic_1a2b. Connected to "
        "without waiting for a scan, which is what --daemon needs to start "
        "unattended.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list the nodes you could connect to right now - BLE names and USB "
        "serial ports - then exit without connecting to any of them.",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="relaunch in the background once the device is chosen, writing "
        "to --log. Prints the pid to stop it with. The device is picked first, "
        "so the background copy never needs a terminal of its own.",
    )
    parser.add_argument(
        "--log",
        metavar="PATH",
        default="meshtastic-bot.log",
        help="where --daemon writes. Appended to, never truncated. "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--heartbeat",
        metavar="SECS",
        type=int,
        default=600,
        help="how often to log a still-alive line with the counters. 0 turns "
        "it off, logging only what actually happens. (default: %(default)s)",
    )
    args = parser.parse_args()

    if args.list:
        sys.exit(list_devices())

    target = resolve_server_target(args.host, args.port, args.ble)
    if target is None:
        sys.exit(1)
    transport, address = target
    if args.daemon:
        sys.exit(
            spawn_detached(
                detached_argv(target, args.here, args.heartbeat), Path(args.log)
            )
        )
    sys.exit(ServerBot(here=args.here, heartbeat=args.heartbeat).run(transport, address))


if __name__ == "__main__":
    main()
