#!/usr/bin/env python3
"""Meshtastic BLE monitor - three-pane interactive TUI.

Layout (all visible at once, k9s/ranger style):
    +-----------+-----------+---------------------------+
    | Devices   | Channels  |  Messages for the         |
    | (BLE)     | & Nodes   |  selected channel/node    |
    |           |           |  + send box               |
    +-----------+-----------+---------------------------+

Flow:
  1. Left pane - scans for Meshtastic BLE peripherals, pick one to connect.
  2. Middle pane - once connected, lists this node's channels AND all known
     mesh nodes. Pick a channel to broadcast on it, or a node to send it a
     direct message.
  3. Right pane - live message log for whichever target is selected (each
     target keeps its own scrollback), plus an input box to send text.
     Keyword auto-reply (see rules.txt) fires for every incoming message on
     every target, in the background, the whole time the app is running.

Usage:
    python3 bot.py

Auto-reply rules live in rules.txt next to this script, one per line as
    keyword=reply text
Blank lines and lines starting with # are ignored. The file is re-read on
every incoming message, so edits take effect immediately - no restart needed.

The Meshtastic phone/desktop app must be disconnected from the device first -
BLE only allows one connected client at a time.
"""

import datetime
from pathlib import Path

from pubsub import pub
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Label, ListItem, ListView, RichLog

import meshtastic
import meshtastic.ble_interface

RULES_FILE = Path(__file__).parent / "rules.txt"

DEFAULT_RULES = """\
# keyword=reply text (one per line, case-insensitive substring match on keyword)
ping=pong
help=指令: ping
"""

BROADCAST_ADDR = "^all"


def load_rules() -> dict[str, str]:
    """Re-read rules.txt every call so edits take effect without restarting the bot."""
    if not RULES_FILE.exists():
        RULES_FILE.write_text(DEFAULT_RULES, encoding="utf-8")

    rules: dict[str, str] = {}
    for raw_line in RULES_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keyword, _, reply = line.partition("=")
        keyword = keyword.strip()
        if keyword:
            rules[keyword] = reply.strip()
    return rules


def find_reply(text: str) -> str | None:
    lowered = text.lower()
    for keyword, reply in load_rules().items():
        if keyword.lower() in lowered:
            return reply
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

    to_id = packet.get("toId", BROADCAST_ADDR)
    from_id = packet.get("fromId", "?")
    if to_id == BROADCAST_ADDR:
        target = ("channel", packet.get("channel", 0))
    else:
        other = to_id if from_id == my_id else from_id
        target = ("node", other)

    return {
        "text": decoded.get("text", ""),
        "from_id": from_id,
        "to_id": to_id,
        "target": target,
        "when": when,
        "transport": "MQTT" if packet.get("viaMqtt") else "LoRa",
        "snr": packet.get("rxSnr"),
        "rssi": packet.get("rxRssi"),
    }


def format_incoming_line(info: dict) -> str:
    """Render a parsed incoming message as one RichLog line."""
    line = f"[dim]{info['when']}[/dim] [bold]{info['from_id']}[/bold] ({info['transport']}"
    if info["snr"] is not None:
        line += f" snr={info['snr']}"
    if info["rssi"] is not None:
        line += f" rssi={info['rssi']}"
    line += f"): {info['text']}"
    return line


def build_reply_text(reply: str, info: dict) -> str:
    """Append the collected message info to a keyword reply, compact (LoRa payload limit)."""
    bits = [info["when"], f"via={info['transport']}"]
    if info["snr"] is not None:
        bits.append(f"snr={info['snr']}")
    if info["rssi"] is not None:
        bits.append(f"rssi={info['rssi']}")
    return f"{reply} | {' '.join(bits)} from={info['from_id']}"


class MeshtasticTUI(App):
    """Three-pane Meshtastic BLE monitor: devices | channels & nodes | messages."""

    CSS = """
    #main-row { height: 1fr; }
    #devices-pane, #targets-pane { width: 28; border: solid $accent; }
    #messages-pane { border: solid $accent; }
    #status-pane { height: 8; border: solid $warning; }
    ListView { height: 1fr; }
    RichLog { height: 1fr; }
    .pane-title { text-style: bold; background: $accent 20%; padding: 0 1; }
    #status-pane .pane-title { background: $warning 30%; }
    """

    BINDINGS = [Binding("r", "rescan", "Rescan devices"), Binding("q", "quit", "Quit")]

    def __init__(self) -> None:
        super().__init__()
        self.interface = None
        self.my_id: str | None = None
        self.target: tuple[str, str | int] | None = None  # ("channel", idx) or ("node", id)
        self.history: dict[tuple, list[str]] = {}

    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal(id="main-row"):
                with Vertical(id="devices-pane"):
                    yield Label("裝置 (BLE)", classes="pane-title")
                    yield ListView(id="device-list")
                with Vertical(id="targets-pane"):
                    yield Label("頻道 / Node", classes="pane-title")
                    yield ListView(id="target-list")
                with Vertical(id="messages-pane"):
                    yield Label("訊息", classes="pane-title", id="messages-title")
                    yield RichLog(id="log", wrap=True, highlight=True, markup=True)
                    yield Input(
                        placeholder="選一個頻道/node 才能送訊息...", id="send-box", disabled=True
                    )
            with Vertical(id="status-pane"):
                yield Label("狀態", classes="pane-title")
                yield RichLog(id="status-log", wrap=True, highlight=True, markup=True)

    def on_mount(self) -> None:
        pub.subscribe(self.on_receive, "meshtastic.receive")
        pub.subscribe(self.on_config_synced, "meshtastic.connection.established")
        self.action_rescan()

    def on_unmount(self) -> None:
        pub.unsubscribe(self.on_receive, "meshtastic.receive")
        pub.unsubscribe(self.on_config_synced, "meshtastic.connection.established")
        if self.interface:
            self.interface.close()

    # ---- pane 1: devices -------------------------------------------------

    def action_rescan(self) -> None:
        self.query_one("#device-list", ListView).clear()
        self.scan_devices()

    @work(thread=True)
    def scan_devices(self) -> None:
        try:
            devices = meshtastic.ble_interface.BLEInterface.scan()
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._log_system, f"[red]掃描失敗: {e}[/red]")
            return
        self.call_from_thread(self._populate_devices, devices)

    def _populate_devices(self, devices: list) -> None:
        listview = self.query_one("#device-list", ListView)
        for d in devices:
            listview.append(ListItem(Label(d.name), name=d.name))
        if not devices:
            self._log_system("沒找到裝置,按 R 重新掃描")

    def _on_device_selected(self, device_name: str) -> None:
        self._log_system(f"連線到 {device_name}...")
        self.connect_device(device_name)

    @work(thread=True)
    def connect_device(self, device_name: str) -> None:
        try:
            interface = meshtastic.ble_interface.BLEInterface(address=device_name)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._log_system, f"[red]連線失敗: {e}[/red]")
            return
        self.call_from_thread(self._connected, interface)

    def _connected(self, interface) -> None:
        # This fires as soon as the raw BLE link is up. Channels/nodes/myUser are
        # not populated yet at this point - that's a separate, slightly later
        # config-sync phase signalled by "meshtastic.connection.established"
        # (see on_config_synced below). Don't populate the targets pane here.
        self.interface = interface
        self._log_system("[green]BLE 已連線[/green],等待設定同步...")

    def on_config_synced(self, interface, topic=pub.AUTO_TOPIC) -> None:
        # Fires on meshtastic's own pubsub thread, not Textual's - hop back.
        self.call_from_thread(self._config_synced, interface)

    def _config_synced(self, interface) -> None:
        my_user = interface.getMyUser() or {}
        self.my_id = my_user.get("id")
        self._log_system(f"[green]設定同步完成[/green] (my id: {self.my_id})")
        self._populate_targets()

    # ---- pane 2: channels & nodes -----------------------------------------

    def _populate_targets(self) -> None:
        listview = self.query_one("#target-list", ListView)
        listview.clear()

        for ch in self.interface.localNode.channels or []:
            if not ch.settings:
                continue
            name = ch.settings.name or f"(unnamed #{ch.index})"
            listview.append(
                ListItem(Label(f"# {name}"), name=f"channel:{ch.index}")
            )

        for node_id, node in (self.interface.nodes or {}).items():
            if node_id == self.my_id:
                continue
            user = node.get("user", {})
            label = user.get("shortName") or user.get("longName") or node_id
            listview.append(ListItem(Label(f"@ {label} [dim]{node_id}[/dim]"), name=f"node:{node_id}"))

        self._log_system(f"載入 {len(listview.children)} 個頻道/node")

    def _on_target_selected(self, target_key: str) -> None:
        kind, _, key = target_key.partition(":")
        if kind == "channel":
            key = int(key)
        self.target = (kind, key)
        self.query_one("#messages-title", Label).update(f"訊息 - {kind}:{key}")
        self.query_one("#send-box", Input).disabled = False
        self._redraw_log()
        self._log_system(f"切換監控目標: {kind}:{key}")

    def _redraw_log(self) -> None:
        log = self.query_one("#log", RichLog)
        log.clear()
        for line in self.history.get(self.target, []):
            log.write(line)

    # ---- pane 3: messages ---------------------------------------------------

    def _log_system(self, line: str) -> None:
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.query_one("#status-log", RichLog).write(f"[dim]{now}[/dim] {line}")

    def on_receive(self, packet, interface) -> None:
        info = parse_incoming(packet, self.my_id)
        if info is None:
            return
        line = format_incoming_line(info)
        self.history.setdefault(info["target"], []).append(line)

        def update_ui():
            if info["target"] == self.target:
                self.query_one("#log", RichLog).write(line)
            kind, key = info["target"]
            self._log_system(f"收到訊息 {kind}:{key} from={info['from_id']}")

        self.call_from_thread(update_ui)

        reply = find_reply(info["text"])
        if reply is None:
            return
        full_reply = build_reply_text(reply, info)
        kind, key = info["target"]
        if kind == "channel":
            interface.sendText(full_reply, channelIndex=key)
        else:
            interface.sendText(full_reply, destinationId=key)

        reply_line = f"[yellow]  -> auto-reply: {full_reply}[/yellow]"
        self.history.setdefault(info["target"], []).append(reply_line)

        def update_ui2():
            if info["target"] == self.target:
                self.query_one("#log", RichLog).write(reply_line)

        self.call_from_thread(update_ui2)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or self.target is None:
            return
        event.input.value = ""
        now = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[dim]{now}[/dim] [bold cyan]me[/bold cyan]: {text}"
        self.history.setdefault(self.target, []).append(line)
        self.query_one("#log", RichLog).write(line)

        kind, key = self.target
        if kind == "channel":
            self.interface.sendText(text, channelIndex=key)
        else:
            self.interface.sendText(text, destinationId=key)

    # ---- shared list dispatch -------------------------------------------

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "device-list":
            self._on_device_selected(event.item.name)
        elif event.list_view.id == "target-list":
            self._on_target_selected(event.item.name)


if __name__ == "__main__":
    MeshtasticTUI().run()
