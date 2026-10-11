"""
Write FL Studio project files (.flp) from scratch.

The FLP container is a flat list of events: a 1-byte id followed by a fixed
size payload (ids < 192) or a varint length + bytes (ids >= 192). The event
sequence written here mirrors an FL Studio 24.2 project byte-for-byte in its
structure (see flp_template.py for the extracted boilerplate):

    header events -> audio-clip channels -> arrangement (playlist + markers +
    500 playlist tracks) -> mixer (127 inserts + one parameter table) -> tail

Everything the migration needs is supported: audio clips at arbitrary tick
positions, per-clip channel volume/pan/colour, playlist track names/colours/
mute state, mixer insert names/colours/volume/pan/routing/sends, and effect
slots that load stock plugins with either a known-good default state, a
constructed state, or no state at all (FL then uses the plugin's defaults).

Verification of the generated files inside FL Studio has to happen on a
Windows machine (see windows/verify_fl.py); this module validates its own
output by round-tripping it through PyFLP.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Optional

from .flp_template import TEMPLATE as T

MAX_TRACKS = 500
INSERT_COUNT = int(T["INSERT_COUNT"])          # 127 (master + 126)
SLOTS = 10
PPQ_DEFAULT = 960                               # finest FL timebase; 1 tick = 0.52 ms @120 BPM
VOL_UNITY = 12800                               # mixer fader 0 dB / channel volume 100 %
PAN_CENTER_CH = 6400                            # channel pan (0..12800)


# ----------------------------------------------------------------- events
def enc(eid: int, payload: bytes) -> bytes:
    if eid < 64:
        assert len(payload) == 1, (eid, payload)
        return bytes([eid]) + payload
    if eid < 128:
        assert len(payload) == 2, (eid, payload)
        return bytes([eid]) + payload
    if eid < 192:
        assert len(payload) == 4, (eid, payload)
        return bytes([eid]) + payload
    n = len(payload)
    out = bytes([eid])
    while n >= 128:
        out += bytes([(n & 127) | 128])
        n >>= 7
    return out + bytes([n]) + payload


def u8(v: int) -> bytes: return struct.pack("<B", v & 0xFF)
def u16(v: int) -> bytes: return struct.pack("<H", v & 0xFFFF)
def i16(v: int) -> bytes: return struct.pack("<h", v)
def u32(v: int) -> bytes: return struct.pack("<I", v & 0xFFFFFFFF)
def i32(v: int) -> bytes: return struct.pack("<i", v)
def f32(v: float) -> bytes: return struct.pack("<f", v)
def text(s: str) -> bytes: return s.encode("utf-16le") + b"\x00\x00"
def color_bytes(rgb: tuple[int, int, int]) -> bytes:
    r, g, b = rgb
    return bytes([r & 0xFF, g & 0xFF, b & 0xFF, 0])


def _tmpl_events(key: str) -> list[tuple[int, bytes]]:
    return [(int(i), bytes.fromhex(h)) for i, h in T[key]]


# ------------------------------------------------------------------ spec
@dataclass
class SlotSpec:
    plugin: str                       # FL internal name, e.g. "Fruity Parametric EQ 2"
    state: Optional[bytes] = None     # raw plugin state (PluginID.Data); None = plugin defaults
    mix: int = VOL_UNITY              # slot wet/dry (0..12800)
    enabled: bool = True
    name: Optional[str] = None        # optional user name shown on the slot


@dataclass
class InsertSpec:
    index: int                        # 1..126 (0 is master)
    name: str
    color: tuple[int, int, int] = (0x48, 0x51, 0x56)
    volume: int = VOL_UNITY           # 0..16000
    pan: int = 0                      # -6400..6400
    slots: list[SlotSpec] = field(default_factory=list)
    route_to_master: bool = True
    sends: dict[int, int] = field(default_factory=dict)   # target insert -> level (0..12800)
    enabled: bool = True


@dataclass
class ClipChannelSpec:
    name: str
    sample_path: str                  # as FL should store it (absolute Windows path or bare file name)
    insert: int                       # mixer insert index
    color: tuple[int, int, int] = (0x9E, 0x9E, 0x9E)
    volume: int = 10000               # FL channel volume default (10000 = "78%" which is unity for audio clips)
    pan: int = PAN_CENTER_CH


@dataclass
class ClipSpec:
    channel: int                      # ClipChannelSpec index (channel iid)
    track: int                        # playlist track number, 1-based
    position_ticks: int
    length_ticks: int
    muted: bool = False
    group: int = 0


@dataclass
class TrackSpec:
    number: int                       # 1-based playlist track
    name: str
    color: tuple[int, int, int] = (0x56, 0x51, 0x48)
    enabled: bool = True
    height: float = 1.0


@dataclass
class MarkerSpec:
    position_ticks: int
    name: str


@dataclass
class ProjectSpec:
    title: str
    bpm: float
    time_sig: tuple[int, int] = (4, 4)
    ppq: int = PPQ_DEFAULT
    comments: str = ""
    genre: str = ""
    artists: str = ""
    channels: list[ClipChannelSpec] = field(default_factory=list)
    clips: list[ClipSpec] = field(default_factory=list)
    tracks: list[TrackSpec] = field(default_factory=list)
    markers: list[MarkerSpec] = field(default_factory=list)
    inserts: list[InsertSpec] = field(default_factory=list)
    master_slots: list[SlotSpec] = field(default_factory=list)
    master_volume: int = VOL_UNITY
    arrangement_name: str = "Arrangement"

    def seconds_to_ticks(self, s: float) -> float:
        return s * self.bpm / 60.0 * self.ppq


# -------------------------------------------------------------- building
def _header_events(spec: ProjectSpec) -> bytes:
    out = bytearray()
    for eid, payload in _tmpl_events("HEADER_EVENTS"):
        if eid == 156:                       # Tempo (bpm * 1000)
            payload = u32(int(round(spec.bpm * 1000)))
        elif eid == 17:
            payload = u8(spec.time_sig[0])
        elif eid == 18:
            payload = u8(spec.time_sig[1])
        elif eid == 67:                      # currently selected pattern
            payload = u16(1)
        out += enc(eid, payload)
        if eid == 10:                        # ShowInfo: project texts follow it in FL's order
            out += enc(194, text(spec.title))
            out += enc(206, text(spec.genre))
            out += enc(207, text(spec.artists))
            out += enc(202, text(""))        # DataPath
            out += enc(195, text(spec.comments))
            out += enc(237, struct.pack("<dd", 45940.0, 0.0))   # Timestamp (created, time spent)
    return bytes(out)


def _channel_events(idx: int, ch: ClipChannelSpec) -> bytes:
    out = bytearray()
    for eid, payload in _tmpl_events("CHANNEL_BLOCK"):
        if eid == 64:
            payload = u16(idx)
        elif eid == 21:                      # Type: 4 = Instrument slot used by audio clips
            payload = u8(4)
        elif eid == 201:
            payload = text("")
        elif eid == 203:
            payload = text(ch.name)
        elif eid == 128:
            payload = color_bytes(ch.color)
        elif eid == 22:                      # RoutedTo (mixer insert)
            payload = struct.pack("<b", ch.insert)
        elif eid == 219:                     # Levels: pan i32, volume u32, pitch i32, ...
            payload = i32(ch.pan) + u32(ch.volume) + payload[8:]
        elif eid == 196:
            payload = text(ch.sample_path)
        elif eid == 143:                     # SamplerFlags: keep as in template (resample off etc.)
            payload = payload
        out += enc(eid, payload)
    return bytes(out)


def _playlist_item(spec: ProjectSpec, clip: ClipSpec) -> bytes:
    tail = bytes.fromhex(T["PLAYLIST_ITEM_TAIL"])
    flags = 0x0040 | (0x2000 if clip.muted else 0)   # 0x2000 = muted (FL "item disabled")
    return (
        u32(clip.position_ticks)
        + u16(20480)
        + u16(clip.channel)
        + u32(max(1, clip.length_ticks))
        + u16(MAX_TRACKS - clip.track)
        + u16(clip.group)
        + bytes([120, 0])
        + u16(flags)
        + bytes([64, 100, 128, 128])
        + f32(-1.0) + f32(-1.0)                     # start/end offsets: -1 = whole sample
        + tail
    )


def _track_events(spec: ProjectSpec) -> bytes:
    named = {t.number: t for t in spec.tracks}
    base = bytearray.fromhex(T["TRACK_DATA_EMPTY"])
    out = bytearray()
    for n in range(1, MAX_TRACKS + 1):
        data = bytearray(base)
        data[0:4] = u32(n)
        t = named.get(n)
        if t:
            data[4:8] = color_bytes(t.color)
            data[12] = 1 if t.enabled else 0
            data[13:17] = f32(max(0.2, t.height))
        out += enc(238, bytes(data))
        if t:
            out += enc(239, text(t.name))
        out += enc(43, b"\x00")
    return bytes(out)


def _arrangement_events(spec: ProjectSpec) -> bytes:
    out = bytearray()
    out += enc(99, u16(0))
    out += enc(241, text(spec.arrangement_name))
    out += enc(36, b"\x00")
    items = b"".join(_playlist_item(spec, c) for c in sorted(spec.clips, key=lambda c: (c.position_ticks, c.track)))
    out += enc(233, items)
    for m in sorted(spec.markers, key=lambda m: m.position_ticks):
        out += enc(148, u32(m.position_ticks))
        out += enc(205, text(m.name))
    out += _track_events(spec)
    return bytes(out)


def _slot_events(slot_idx: int, slot: Optional[SlotSpec]) -> bytes:
    out = bytearray()
    if slot is not None:
        out += enc(201, text(slot.plugin))
        for eid, payload in _tmpl_events("PLUGIN_SLOT_EVENTS"):
            out += enc(eid, payload)
            if eid == 212 and slot.name:
                out += enc(203, text(slot.name))
        if slot.state is not None:
            out += enc(213, slot.state)
    out += enc(98, u16(slot_idx))
    return bytes(out)


def _insert_block(ins: Optional[InsertSpec], is_master: bool, master_slots: list[SlotSpec]) -> bytes:
    out = bytearray()
    out += enc(42, b"\x00")                        # insert separator (precedes every insert's Flags)
    flags = 0x0C if is_master else 0x4C          # EnableEffects|Enabled (|DockMiddle)
    if ins is not None and not ins.enabled:
        flags &= ~0x08
    out += enc(236, u32(0) + u32(flags) + u32(0))
    if ins is not None:
        out += enc(204, text(ins.name))
        out += enc(128, color_bytes(ins.color))
    slots = master_slots if is_master else (ins.slots if ins else [])
    for s in range(SLOTS):
        out += _slot_events(s, slots[s] if s < len(slots) else None)
    routing = bytearray(INSERT_COUNT)
    if not is_master and ins is not None:
        if ins.route_to_master:
            routing[0] = 1
        for target in ins.sends:
            if 0 <= target < INSERT_COUNT:
                routing[target] = 1
    elif not is_master:
        routing[0] = 1
    out += enc(235, bytes(routing))
    out += enc(165, u32(3))
    out += enc(166, u32(1))
    out += enc(154, i32(-1))                       # Input: none
    out += enc(147, i32(0 if is_master else -1))   # Output: master -> soundcard out 0
    return bytes(out)


def _params_event(spec: ProjectSpec, by_index: dict[int, InsertSpec]) -> bytes:
    def item(ins_idx: int, slot: int, pid: int, msg: int, u1: int = 31, flag: int = 0x2000) -> bytes:
        return struct.pack("<4sBBHi", b"\x00" * 4, pid, u1, flag | ((ins_idx & 0x7F) << 6) | (slot & 0x3F), msg)

    out = bytearray()
    out += item(0, 0, 0, VOL_UNITY, u1=0, flag=0x4000)   # global record as FL writes it (main volume)
    for i in range(INSERT_COUNT):
        ins = by_index.get(i)
        slots = spec.master_slots if i == 0 else (ins.slots if ins else [])
        for s in range(SLOTS):
            sl = slots[s] if s < len(slots) else None
            out += item(i, s, 0, 1 if (sl is None or sl.enabled) else 0)
            out += item(i, s, 1, sl.mix if sl else VOL_UNITY)
        vol = spec.master_volume if i == 0 else (ins.volume if ins else VOL_UNITY)
        pan = ins.pan if (ins and i) else 0
        out += item(i, 0, 192, max(0, min(16000, vol)))
        out += item(i, 0, 193, max(-6400, min(6400, pan)))
        out += item(i, 0, 194, 0)
        for pid, msg in ((208, 0), (209, 0), (210, 0), (216, 5777), (217, 33145), (218, 55825),
                         (224, 17500), (225, 17500), (226, 17500)):
            out += item(i, 0, pid, msg)
        if ins:
            for target, level in sorted(ins.sends.items()):
                out += item(i, 0, 64 + target, max(0, min(VOL_UNITY, level)))
        if i != INSERT_COUNT - 1:
            out += item(i, 0, 164, 0)
        out += item(i, 0, 190, 0)
    return bytes(out)


def _mixer_events(spec: ProjectSpec) -> bytes:
    by_index = {ins.index: ins for ins in spec.inserts}
    out = bytearray()
    for eid, payload in _tmpl_events("PRE_MIXER_EVENTS"):
        if eid == 42:
            continue                                 # written at the start of each insert block
        out += enc(eid, payload)
    for i in range(INSERT_COUNT):
        out += _insert_block(by_index.get(i) if i else None, i == 0, spec.master_slots)
    out += enc(225, _params_event(spec, by_index))
    for eid, payload in _tmpl_events("TAIL_EVENTS"):
        out += enc(eid, payload)
    return bytes(out)


def build_flp(spec: ProjectSpec) -> bytes:
    if spec.ppq not in (24, 48, 72, 96, 120, 144, 168, 192, 384, 768, 960):
        raise ValueError(f"invalid PPQ {spec.ppq}")
    for ins in spec.inserts:
        if not 1 <= ins.index < INSERT_COUNT:
            raise ValueError(f"insert index out of range: {ins.index}")
        if len(ins.slots) > SLOTS:
            raise ValueError(f"insert {ins.name} has more than {SLOTS} effects; split the chain")
    body = bytearray()
    body += _header_events(spec)
    for idx, ch in enumerate(spec.channels):
        body += _channel_events(idx, ch)
    body += _arrangement_events(spec)
    body += _mixer_events(spec)
    header = b"FLhd" + u32(6) + u16(0) + u16(len(spec.channels)) + u16(spec.ppq)
    return header + b"FLdt" + u32(len(body)) + bytes(body)


# ---------------------------------------------------- mixer-state (.fst)
def build_insert_fst(name: str, slots: list[SlotSpec]) -> bytes:
    """An FL Studio mixer-track state file: drop it onto any insert to load the chain."""
    body = bytearray()
    body += enc(199, T["FL_VERSION"].encode("ascii") + b"\x00")
    body += enc(159, u32(3228))
    body += enc(28, b"\x01")
    body += enc(37, b"\x01")
    body += enc(204, text(name))
    body += enc(236, u32(0) + u32(0x4C) + u32(0))
    for s in range(SLOTS):
        body += _slot_events(s, slots[s] if s < len(slots) else None)
    params = bytearray()
    for s in range(SLOTS):
        sl = slots[s] if s < len(slots) else None
        params += struct.pack("<4sBBHi", b"\x00" * 4, 0, 31, 0x2000 | s, 1 if (sl is None or sl.enabled) else 0)
        params += struct.pack("<4sBBHi", b"\x00" * 4, 1, 31, 0x2000 | s, sl.mix if sl else VOL_UNITY)
    for pid, msg in ((192, VOL_UNITY), (193, 0), (194, 0), (208, 0), (209, 0), (210, 0), (216, 5777),
                     (217, 33145), (218, 55825), (224, 17500), (225, 17500), (226, 17500)):
        params += struct.pack("<4sBBHi", b"\x00" * 4, pid, 31, 0x2000, msg)
    body += enc(225, bytes(params))
    return b"FLhd" + u32(6) + u16(64) + u16(1) + u16(96) + b"FLdt" + u32(len(body)) + bytes(body)


# ------------------------------------------------------- plugin states
def _default_state(plugin: str) -> Optional[bytes]:
    st = T.get("PLUGIN_STATES", {}).get(plugin)
    if st:
        return bytes.fromhex(st)
    if plugin == "Fruity Reeverb 2":
        return bytes.fromhex(T["REEVERB2_STATE"])
    if plugin == "Fruity Limiter":
        return bytes.fromhex(T["LIMITER_STATE"])
    return None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _peq2_freq(hz: float) -> int:
    # Fruity Parametric EQ 2 stores frequency as 65536 * log1000(f / 20)  (20 Hz .. 20 kHz)
    hz = _clamp(hz, 20.0, 20000.0)
    return int(round(65536 * math.log(hz / 20.0) / math.log(1000.0)))


def _peq2_width(q: float, band_type: int) -> int:
    if band_type in (5, 7):                       # shelves: Q = (1 - w) * 1.2
        w = 1.0 - _clamp(q, 0.05, 1.2) / 1.2
    elif band_type in (1, 3):                     # low/high pass: Q = 2 ** (w' * 5)
        wp = _clamp(math.log2(max(q, 1e-3)) / 5.0, -1.0, 1.0)
        w = (1.0 - wp) / 2.0
    else:                                         # peak/notch/bandpass: Q = 1 / ((w + 0.01) * 3)
        w = 1.0 / (3.0 * max(q, 0.05)) - 0.01
    return int(round(_clamp(w, 0.0, 1.0) * 65536))


PEQ2_TYPES = {"off": 0, "low_pass": 1, "band_pass": 2, "high_pass": 3, "notch": 4,
              "low_shelf": 5, "peak": 6, "high_shelf": 7}


def encode_state(native: dict, constructed_ok: bool = True) -> tuple[Optional[bytes], bool]:
    """Return (state_bytes, is_template_backed). Constructed states are built from
    reverse-engineered layouts and must be verified in FL Studio; when
    constructed_ok is False only template-backed states are returned."""
    kind = native.get("kind")
    if kind == "balance":
        gain_db = float(native.get("gain_db", 0.0))
        vol = int(round(256 * 10 ** (gain_db / 20.0)))
        pan = int(round(float(native.get("pan", 0.0)) * 128))
        return struct.pack("<ii", _clamp_i(pan, -128, 128), _clamp_i(vol, 0, 320)), True
    if kind == "softclip":
        base = bytearray(_default_state("Fruity Soft Clipper") or b"")
        if len(base) >= 8:
            thr = int(round(_clamp(float(native.get("threshold", 0.9)), 0.1, 1.0) * 32767))
            base[0:4] = i32(thr)
            return bytes(base), True
        return None, False
    if kind == "stereo":
        base = _default_state("Fruity Stereo Enhancer")
        return (base, True) if base else (None, False)
    if kind == "blood":
        base = bytearray(_default_state("Fruity Blood Overdrive") or b"")
        if len(base) >= 36:
            drive = _clamp(float(native.get("drive", 0.3)), 0.0, 1.0)
            base[12:16] = i32(int(round(drive * 65536)))          # preamp
            return bytes(base), True
        return None, False
    if kind == "reverb":
        base = bytearray(_default_state("Fruity Reeverb 2") or b"")
        if len(base) < 64:
            return None, False
        wet = _clamp(float(native.get("wet", 0.3)), 0.0, 1.25)
        size = _clamp(float(native.get("size", 0.5)), 0.0, 1.0)
        decay_s = _clamp(float(native.get("decay_s", 1.5)), 0.1, 20.0)
        highcut = _clamp(float(native.get("highcut_hz", 8000)), 200.0, 20000.0)
        stereo = _clamp(float(native.get("stereo", 0.5)), 0.0, 1.0)
        fields = list(struct.unpack("<16i", base[:64]))
        fields[4] = int(round(size * 100))                         # room size 0..100
        fields[6] = int(round(_clamp(decay_s / 20.0, 0, 1) * 200))  # decay 0..200
        fields[2] = int(round(_clamp(math.log(highcut / 200.0) / math.log(100.0), 0, 1) * 221))  # high cut 0..221 (log)
        fields[10] = int(round((stereo - 0.5) * 2 * 100))          # stereo separation -100..100
        fields[11] = 128                                            # dry 100 %
        fields[13] = int(round(wet * 128))                          # wet
        base[:64] = struct.pack("<16i", *fields)
        return bytes(base), True
    if kind == "limiter_gate":
        base = bytearray(_default_state("Fruity Limiter") or b"")
        if len(base) < 148:
            return None, False
        fields = list(struct.unpack("<37i", base[:148]))
        thr_db = _clamp(float(native.get("threshold_db", -40.0)), -60.0, 0.0)
        fields[17] = int(round((1.0 + thr_db / 60.0) * 1000))      # noise threshold 0..1000 (-60..0 dB)
        fields[18] = int(round(_clamp(float(native.get("release_s", 0.2)) / 2.0, 0, 1) * 10000))
        fields[16] = 1000                                           # gate gain (full)
        fields[9] = 1000                                            # comp threshold at max = off
        fields[11] = 0                                              # comp ratio 1:1
        base[:148] = struct.pack("<37i", *fields)
        return bytes(base), True
    if not constructed_ok:
        return None, False
    if kind == "compressor":
        thr = int(round(_clamp(float(native.get("threshold_db", -20)), -60, 0) * 10))
        ratio = int(round(_clamp(float(native.get("ratio", 4)), 1, 30) * 10))
        gain = int(round(_clamp(float(native.get("gain_db", 0)), 0, 30) * 10))
        attack = int(round(_clamp(float(native.get("attack_s", 0.01)) * 10000, 0, 4000)))
        release = int(round(_clamp(float(native.get("release_s", 0.1)) * 1000, 0, 4000)))
        knee = float(native.get("knee_db", 6))
        ctype = 0 if knee < 3 else (1 if knee < 10 else 3)
        return struct.pack("<Iiiiiii", 0, thr, ratio, gain, attack, release, ctype), False
    if kind == "eq":
        bands = list(native.get("bands", []))[:7]
        gains, freqs, widths, types = [0] * 7, [_peq2_freq(1000)] * 7, [_peq2_width(1.0, 6)] * 7, [0] * 7
        for i, b in enumerate(bands):
            t = PEQ2_TYPES.get(str(b.get("type", "peak")), 6)
            types[i] = t
            gains[i] = int(round(_clamp(float(b.get("gain_db", 0)), -18, 18) * 100))
            freqs[i] = _peq2_freq(float(b.get("freq", 1000)))
            widths[i] = _peq2_width(float(b.get("q", 1.0)), t)
        main = int(round(_clamp(float(native.get("main_level_db", 0)), -18, 18) * 100))
        return struct.pack("<I7i7I7I7I7ii", 1, *gains, *freqs, *widths, *types, *([0] * 7), main), False
    if kind == "delay":
        time_s = float(native.get("time_s", 0.3))
        beats = native.get("beats")
        fb = _clamp(float(native.get("feedback", 0.2)), 0, 0.95)
        wet = _clamp(float(native.get("wet", 0.2)), 0, 1)
        dry = _clamp(float(native.get("dry", 1.0)), 0, 1)
        lpf = _clamp(float(native.get("lpf", 16000)), 100, 20000)
        pingpong = bool(native.get("pingpong"))
        delay_time = int(round(_clamp(beats * 96, 1, 768))) if beats else int(round(_clamp(time_s * 1000 / 5.0, 1, 768)))
        fields = [1, int(wet * 6000), 1 if pingpong else 0, 1 if beats else 0, 0, delay_time, 0, 0,
                  int(_clamp(float(native.get("spread", 0.0)), 0, 1) * 6000), 0, 0, 0, 0, 0, 0,
                  int(fb * 6000), 0, int(_clamp(math.log(lpf / 100.0) / math.log(200.0), 0, 1) * 6000), 0, 0, 0, 0, 44100, 16,
                  int(wet * 6000), 1500, int(dry * 6000)]
        return struct.pack("<%di" % len(fields), *fields), False
    return None, False


def _clamp_i(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))


# ----------------------------------------------------------- verification
def verify_with_pyflp(flp_bytes: bytes) -> dict:
    """Round-trip through PyFLP (independent parser) and summarise what it sees."""
    import tempfile
    import pyflp
    from pyflp._events import EventEnum, _EventEnumMeta

    original = _EventEnumMeta.__call__

    def compat(cls, value, *args, **kw):
        return cls._missing_(value) if cls is EventEnum else original(cls, value, *args, **kw)

    _EventEnumMeta.__call__ = compat
    try:
        with tempfile.NamedTemporaryFile(suffix=".flp", delete=False) as fh:
            fh.write(flp_bytes)
            tmp = fh.name
        p = pyflp.parse(tmp)
        channels = list(p.channels)
        arr = next(iter(p.arrangements))
        clips = [i for t in arr.tracks for i in t]
        inserts = [ins for ins in p.mixer if ins.name]
        return {
            "version": str(p.version), "tempo": p.tempo, "ppq": p.ppq, "title": p.title,
            "channels": len(channels), "clips": len(clips),
            "clip_end_ticks": max((c.position + c.length for c in clips), default=0),
            "named_inserts": [(i.iid, i.name, [s.internal_name for s in i if s.internal_name]) for i in inserts],
            "sample_paths": [str(getattr(c, "sample_path", "")) for c in channels][:5],
        }
    finally:
        _EventEnumMeta.__call__ = original
