#!/usr/bin/env python3
"""Meshtastic channel auto-responder.

Connects to a Meshtastic node over BLE, watches incoming text messages on the
EDGE_ATS channel only, logs each one (time, sender, transport LoRa/MQTT,
SNR/RSSI, text), and replies with a fixed message when the text matches a
keyword rule.

Usage:
    python3 bot.py

Edit KEYWORD_RULES below to add/change triggers. Matching is case-insensitive
substring match against the message body.

The Meshtastic phone/desktop app must be disconnected from the device first -
BLE only allows one connected client at a time.
"""

import datetime
import sys
import time

from pubsub import pub

import meshtastic
import meshtastic.ble_interface

# BLE name of the GAT562 node. Using the name rather than the address because
# macOS assigns a different synthetic UUID per app for the same physical
# device (privacy feature) - the real MAC address (visible in system
# Bluetooth settings) does not match what BLEInterface's scan sees.
DEVICE_ADDRESS = "Bug2_1ca6"

# Only watch/reply on this channel (by name, not index - resolved after connecting).
CHANNEL_NAME = "EDGE_ATS"

# keyword (lowercase substring match) -> reply text
KEYWORD_RULES = {
    "ping": "pong",
    "help": "指令: ping",
}

# Resolved to the real channel index once connected (see main()).
channel_index = None


def find_reply(text: str) -> str | None:
    lowered = text.lower()
    for keyword, reply in KEYWORD_RULES.items():
        if keyword in lowered:
            return reply
    return None


def on_receive(packet, interface):
    decoded = packet.get("decoded")
    if not decoded or decoded.get("portnum") != "TEXT_MESSAGE_APP":
        return

    channel = packet.get("channel", 0)
    if channel != channel_index:
        return  # not the channel we're watching

    text = decoded.get("text", "")
    from_id = packet.get("fromId", "?")

    rx_time = packet.get("rxTime")
    when = (
        datetime.datetime.fromtimestamp(rx_time).strftime("%Y-%m-%d %H:%M:%S")
        if rx_time
        else "?"
    )
    transport = "MQTT" if packet.get("viaMqtt") else "LoRa"
    snr = packet.get("rxSnr")
    rssi = packet.get("rxRssi")

    print(
        f"[recv] {when} ch={channel} from={from_id} via={transport} "
        f"snr={snr} rssi={rssi}: {text!r}"
    )

    reply = find_reply(text)
    if reply is None:
        return

    # Append the collected info to the reply itself, not just the local log -
    # kept compact since LoRa payloads are size-limited.
    info_bits = [when, f"via={transport}"]
    if snr is not None:
        info_bits.append(f"snr={snr}")
    if rssi is not None:
        info_bits.append(f"rssi={rssi}")
    full_reply = f"{reply} | {' '.join(info_bits)} from={from_id}"

    print(f"[send] ch={channel}: {full_reply!r}")
    interface.sendText(full_reply, channelIndex=channel)


def on_connection(interface, topic=pub.AUTO_TOPIC):
    print(f"[connected] node info: {interface.myInfo}")


def main():
    global channel_index

    pub.subscribe(on_receive, "meshtastic.receive")
    pub.subscribe(on_connection, "meshtastic.connection.established")

    print(f"Connecting to {DEVICE_ADDRESS} over BLE...")
    interface = meshtastic.ble_interface.BLEInterface(address=DEVICE_ADDRESS)

    ch = interface.localNode.getChannelByName(CHANNEL_NAME)
    if ch is None:
        names = [c.settings.name for c in (interface.localNode.channels or []) if c.settings]
        print(f"ERROR: no channel named {CHANNEL_NAME!r} on this node. Found: {names}")
        interface.close()
        sys.exit(1)
    channel_index = ch.index
    print(f"Watching channel {CHANNEL_NAME!r} (index {channel_index})")

    print("Listening. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        interface.close()


if __name__ == "__main__":
    main()
