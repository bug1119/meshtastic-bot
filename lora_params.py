"""Derive the LoRa parameters a node is actually using.

A node reports its *stored* LoRa config, not the settings it ends up running.
With `use_preset` set - the normal state - `bandwidth` stays 0 and
`override_frequency` stays 0.0, because the firmware derives both at runtime
from region + preset + frequency slot and never sends the result back. So a
panel that prints those fields verbatim shows "0 kHz" and no frequency at all.

The tables below mirror the firmware so those values can be reconstructed:

  * preset -> bandwidth/SF/CR: `modemPresetToParams()` in src/mesh/MeshRadio.h
  * region -> band edges, wide-LoRa flag: the `RDEF(...)` table in
    src/mesh/RadioInterface.cpp
  * region profile -> channel spacing/padding: the `PROFILE_*` constants in the
    same file
  * the frequency formula: `applyModemConfig()`, also in RadioInterface.cpp

Mirroring firmware logic means it can drift when upstream changes a band plan,
so two things are deliberate. Everything is keyed by protobuf enum *name*, never
by number - the installed meshtastic package and the firmware disagree about
numbering already (the firmware has presets this package has never heard of), so
numeric keys would silently misalign. And an unknown region or preset returns
None rather than a guess, which the caller shows as "unknown" instead of
inventing a plausible-looking frequency.

Verified against two real nodes and the firmware's own documentation:

  * a GAT562 on TW/MEDIUM_FAST/slot 1 has override_frequency pinned to 920.125;
    this computes 920.125 for those inputs
  * its stored bandwidth 250 and spread_factor 9 match MEDIUM_FAST's table entry
  * EU_866's four channels compute as 865.7/866.3/866.9/867.5 MHz, matching the
    band plan spelled out in the RadioInterface.cpp comments
"""

from meshtastic.protobuf import config_pb2

_LORA = config_pb2.Config.LoRaConfig

# preset name -> (bandwidth kHz normal, bandwidth kHz wide-LoRa, SF, CR).
# Presets absent from older meshtastic packages are listed anyway; keying by name
# means an entry nobody asks for simply goes unused.
PRESET_PARAMS = {
    "SHORT_TURBO": (500.0, 1625.0, 7, 5),
    "SHORT_FAST": (250.0, 812.5, 7, 5),
    "SHORT_SLOW": (250.0, 812.5, 8, 5),
    "MEDIUM_FAST": (250.0, 812.5, 9, 5),
    "MEDIUM_SLOW": (250.0, 812.5, 10, 5),
    "MEDIUM_TURBO": (500.0, 1625.0, 9, 5),
    "LONG_TURBO": (500.0, 1625.0, 11, 8),
    "LONG_MODERATE": (125.0, 406.25, 11, 8),
    "LONG_SLOW": (125.0, 406.25, 12, 8),
    "LONG_FAST": (250.0, 812.5, 11, 5),
    "LITE_FAST": (125.0, 125.0, 9, 5),
    "LITE_SLOW": (125.0, 125.0, 10, 5),
    "NARROW_FAST": (62.5, 62.5, 7, 6),
    "NARROW_SLOW": (62.5, 62.5, 8, 6),
    "TINY_FAST": (15.6, 15.6, 7, 5),
    "TINY_SLOW": (15.6, 15.6, 8, 6),
}

# The firmware's default branch is LONG_FAST, so an unrecognised preset lands
# there on the device too.
_DEFAULT_PRESET = "LONG_FAST"

# Channel geometry per region profile: (spacing MHz, padding MHz).
_PROFILE_STD = (0.0, 0.0)
_PROFILE_EU868 = (0.0, 0.0)
_PROFILE_LITE = (0.4, 0.0375)
_PROFILE_NARROW = (0.0, 0.0104)
_PROFILE_HAM_20KHZ = (0.0, 0.0022)
_PROFILE_HAM_100KHZ = (0.0, 0.01875)

# region name -> (freq start MHz, freq end MHz, wide LoRa, profile, override slot).
# override slot is the region's forced 1-based slot, 0 when it has none.
REGIONS = {
    "US": (902.0, 928.0, False, _PROFILE_STD, 0),
    "EU_433": (433.0, 434.0, False, _PROFILE_STD, 0),
    "EU_868": (869.4, 869.65, False, _PROFILE_EU868, 0),
    "EU_866": (865.6, 867.6, False, _PROFILE_LITE, 0),
    "EU_N_868": (869.4, 869.65, False, _PROFILE_NARROW, 1),
    "CN": (470.0, 510.0, False, _PROFILE_STD, 0),
    "JP": (920.5, 923.5, False, _PROFILE_STD, 0),
    "ANZ": (915.0, 928.0, False, _PROFILE_STD, 0),
    "ANZ_433": (433.05, 434.79, False, _PROFILE_STD, 0),
    "RU": (868.7, 869.2, False, _PROFILE_STD, 0),
    "KR": (920.0, 923.0, False, _PROFILE_STD, 0),
    "TW": (920.0, 925.0, False, _PROFILE_STD, 0),
    "IN": (865.0, 867.0, False, _PROFILE_STD, 0),
    "NZ_865": (864.0, 868.0, False, _PROFILE_STD, 0),
    "TH": (920.0, 925.0, False, _PROFILE_STD, 0),
    "UA_433": (433.0, 434.7, False, _PROFILE_STD, 0),
    "MY_433": (433.0, 435.0, False, _PROFILE_STD, 0),
    "MY_919": (919.0, 924.0, False, _PROFILE_STD, 0),
    "SG_923": (917.0, 925.0, False, _PROFILE_STD, 0),
    "PH_433": (433.0, 434.7, False, _PROFILE_STD, 0),
    "PH_868": (868.0, 869.4, False, _PROFILE_STD, 0),
    "PH_915": (915.0, 918.0, False, _PROFILE_STD, 0),
    "KZ_433": (433.075, 434.775, False, _PROFILE_STD, 0),
    "KZ_863": (863.0, 868.0, False, _PROFILE_STD, 0),
    "NP_865": (865.0, 868.0, False, _PROFILE_STD, 0),
    "BR_902": (902.0, 907.5, False, _PROFILE_STD, 0),
    "ITU1_2M": (144.0, 146.0, False, _PROFILE_HAM_20KHZ, 26),
    "ITU2_2M": (144.0, 148.0, False, _PROFILE_HAM_20KHZ, 51),
    "ITU3_2M": (144.0, 148.0, False, _PROFILE_HAM_20KHZ, 33),
    "ITU2_125CM": (220.0, 225.0, False, _PROFILE_HAM_100KHZ, 37),
    "ITU1_70CM": (430.0, 440.0, False, _PROFILE_HAM_100KHZ, 37),
    "ITU2_70CM": (420.0, 450.0, False, _PROFILE_HAM_100KHZ, 137),
    "ITU3_70CM": (430.0, 450.0, False, _PROFILE_HAM_100KHZ, 37),
    "LORA_24": (2400.0, 2483.5, True, _PROFILE_STD, 0),
    "UNSET": (902.0, 928.0, False, _PROFILE_STD, 0),
}


def preset_name(preset) -> str | None:
    """Protobuf enum name for a modem preset value, or None if unrecognised."""
    try:
        return _LORA.ModemPreset.Name(preset)
    except ValueError:
        return None


def region_name(region) -> str | None:
    """Protobuf enum name for a region code value, or None if unrecognised."""
    try:
        return _LORA.RegionCode.Name(region)
    except ValueError:
        return None


def bandwidth_khz(lora) -> float | None:
    """Bandwidth the node is really using, in kHz, or None if undeterminable.

    With a custom (non-preset) config the stored value is authoritative; with a
    preset it comes from the preset table, widened for the 2.4 GHz band.
    """
    if not lora.use_preset:
        return float(lora.bandwidth) or None

    name = preset_name(lora.modem_preset)
    params = PRESET_PARAMS.get(name) or PRESET_PARAMS.get(_DEFAULT_PRESET)
    if params is None:
        return None
    narrow, wide, _sf, _cr = params

    region = REGIONS.get(region_name(lora.region))
    wide_lora = region[2] if region else False
    return wide if wide_lora else narrow


def frequency_mhz(lora) -> float | None:
    """Centre frequency the node is really using, in MHz, or None.

    None means it cannot be derived rather than that there is no frequency: the
    region is unknown, or the slot is left to the firmware, which picks it from a
    hash of the primary channel's name. That hash is deliberately not reproduced
    here - it depends on channel state this does not have, and guessing it would
    print a confidently wrong number.
    """
    if lora.override_frequency:
        return float(lora.override_frequency)

    region = REGIONS.get(region_name(lora.region))
    if region is None:
        return None
    freq_start, _freq_end, _wide, (spacing, padding), override_slot = region

    bw = bandwidth_khz(lora)
    if not bw:
        return None

    slot = lora.channel_num or override_slot
    if not slot:
        # Firmware falls back to hash(channel name) % slot count.
        return None

    slot_width = spacing + (padding * 2) + (bw / 1000)
    freq = freq_start + (bw / 2000) + padding + ((slot - 1) * slot_width)
    return freq + lora.frequency_offset


def slot_count(lora) -> int | None:
    """How many frequency slots the region/bandwidth combination provides."""
    region = REGIONS.get(region_name(lora.region))
    bw = bandwidth_khz(lora)
    if region is None or not bw:
        return None
    freq_start, freq_end, _wide, (spacing, padding), _override = region
    slot_width = spacing + (padding * 2) + (bw / 1000)
    if slot_width <= 0:
        return None
    return round((freq_end - freq_start + spacing) / slot_width)
