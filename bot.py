#!/usr/bin/env python3
"""Meshtastic BLE channel monitor - interactive TUI (k9s-style).

Flow:
  1. Device screen - scans for Meshtastic BLE peripherals, pick one to connect.
  2. Channel screen - lists the connected node's channels, pick one to watch.
  3. Channel view - live scrolling message log (time/sender/transport/SNR/RSSI)
     plus an input box to send text on that channel. Keyword auto-reply (see
     rules.txt) keeps firing in the background the whole time you're in the
     channel view, alongside anything you send by hand.

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
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, RichLog

import meshtastic
import meshtastic.ble_interface

RULES_FILE = Path(__file__).parent / "rules.txt"

DEFAULT_RULES = """\
# keyword=reply text (one per line, case-insensitive substring match on keyword)
ping=pong
help=指令: ping
"""


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


def parse_incoming(packet: dict) -> dict | None:
    """Return a dict of the fields we care about for a text-message packet, or None."""
    decoded = packet.get("decoded")
    if not decoded or decoded.get("portnum") != "TEXT_MESSAGE_APP":
        return None

    rx_time = packet.get("rxTime")
    when = (
        datetime.datetime.fromtimestamp(rx_time).strftime("%H:%M:%S") if rx_time else "??:??:??"
    )

    return {
        "text": decoded.get("text", ""),
        "from_id": packet.get("fromId", "?"),
        "channel": packet.get("channel", 0),
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


class DeviceScreen(Screen):
    """Step 1: scan for Meshtastic BLE peripherals and pick one."""

    BINDINGS = [Binding("r", "rescan", "Rescan"), Binding("q", "app.quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("按 R 掃描附近的 Meshtastic 裝置 (BLE)", id="status")
        yield ListView(id="device-list")
        yield Footer()

    def on_mount(self) -> None:
        self.action_rescan()

    def action_rescan(self) -> None:
        self.query_one("#status", Label).update("掃描中... (約 10 秒)")
        self.query_one("#device-list", ListView).clear()
        self.scan_devices()

    @work(thread=True)
    def scan_devices(self) -> None:
        try:
            devices = meshtastic.ble_interface.BLEInterface.scan()
        except Exception as e:  # noqa: BLE001 - surface any scan failure to the UI
            self.app.call_from_thread(self._show_scan_error, str(e))
            return
        self.app.call_from_thread(self._populate, devices)

    def _show_scan_error(self, message: str) -> None:
        self.query_one("#status", Label).update(f"掃描失敗: {message}")

    def _populate(self, devices: list) -> None:
        listview = self.query_one("#device-list", ListView)
        if not devices:
            self.query_one("#status", Label).update("沒找到裝置,按 R 重新掃描")
            return
        for d in devices:
            listview.append(ListItem(Label(f"{d.name}  [dim]{d.address}[/dim]"), name=d.name))
        self.query_one("#status", Label).update(f"找到 {len(devices)} 個裝置,選一個連線")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        device_name = event.item.name
        self.query_one("#status", Label).update(f"連線到 {device_name}...")
        self.connect_device(device_name)

    @work(thread=True)
    def connect_device(self, device_name: str) -> None:
        try:
            interface = meshtastic.ble_interface.BLEInterface(address=device_name)
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self._show_scan_error, f"連線失敗: {e}")
            return
        self.app.call_from_thread(self._connected, interface)

    def _connected(self, interface) -> None:
        self.app.interface = interface
        self.app.push_screen(ChannelSelectScreen())


class ChannelSelectScreen(Screen):
    """Step 2: pick which channel to watch."""

    BINDINGS = [Binding("q", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("選擇要監控的頻道", id="status")
        yield ListView(id="channel-list")
        yield Footer()

    def on_mount(self) -> None:
        listview = self.query_one("#channel-list", ListView)
        channels = self.app.interface.localNode.channels or []
        found = False
        for ch in channels:
            if not ch.settings:
                continue
            name = ch.settings.name or f"(unnamed #{ch.index})"
            listview.append(ListItem(Label(f"{ch.index}: {name}"), name=str(ch.index)))
            found = True
        if not found:
            self.query_one("#status", Label).update("這台裝置沒有設定任何頻道")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.app.channel_index = int(event.item.name)
        self.app.push_screen(ChannelViewScreen())


class ChannelViewScreen(Screen):
    """Step 3: live message log + send box for the selected channel."""

    BINDINGS = [Binding("q", "app.pop_screen", "Back to channels")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield RichLog(id="log", wrap=True, highlight=True, markup=True)
            yield Input(placeholder="輸入訊息按 Enter 送出...", id="send-box")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#log", RichLog).write(
            f"[bold green]監控頻道 index={self.app.channel_index}[/bold green] "
            "- 關鍵字自動回覆仍在背景運作"
        )
        pub.subscribe(self.on_receive, "meshtastic.receive")
        self.query_one("#send-box", Input).focus()

    def on_unmount(self) -> None:
        pub.unsubscribe(self.on_receive, "meshtastic.receive")

    def on_receive(self, packet, interface) -> None:
        info = parse_incoming(packet)
        if info is None:
            return
        if info["channel"] != self.app.channel_index:
            return

        self.app.call_from_thread(self._append_line, format_incoming_line(info))

        reply = find_reply(info["text"])
        if reply is None:
            return
        full_reply = build_reply_text(reply, info)
        interface.sendText(full_reply, channelIndex=info["channel"])
        self.app.call_from_thread(
            self._append_line, f"[yellow]  -> auto-reply: {full_reply}[/yellow]"
        )

    def _append_line(self, line: str) -> None:
        self.query_one("#log", RichLog).write(line)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.query_one("#log", RichLog).write(f"[dim]{now}[/dim] [bold cyan]me[/bold cyan]: {text}")
        self.app.interface.sendText(text, channelIndex=self.app.channel_index)


class MeshtasticTUI(App):
    """k9s-style Meshtastic channel monitor."""

    CSS = """
    ListView { height: 1fr; }
    RichLog { height: 1fr; border: solid $accent; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.interface = None
        self.channel_index: int | None = None

    def on_mount(self) -> None:
        self.push_screen(DeviceScreen())

    def on_unmount(self) -> None:
        if self.interface:
            self.interface.close()


if __name__ == "__main__":
    MeshtasticTUI().run()
