"""
Rebuild Standard MIDI files from BandLab instrument-track pattern data.

BandLab's pattern JSON is not publicly documented; the exporter therefore
looks for the common note shapes defensively (pitch/note/midiNote, start/
startPosition/time, end/endPosition/duration, velocity) and records what it
understood in the manifest. The raw pattern JSON is always saved next to the
.mid so nothing is lost if the schema is different from what we expect.
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

from .models import Project, Track
from .util import write_json

PPQ = 960


def _vlq(n: int) -> bytes:
    out = bytearray([n & 0x7F])
    n >>= 7
    while n:
        out.insert(0, (n & 0x7F) | 0x80)
        n >>= 7
    return bytes(out)


def _meta(kind: int, data: bytes) -> bytes:
    return b"\xff" + bytes([kind]) + _vlq(len(data)) + data


def _pick(d: dict, *names: str, default=None):
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default


def extract_notes(track: Track, bpm: float) -> tuple[list[dict], list[str]]:
    """Return (notes, warnings). Note = {start_beats, dur_beats, pitch, vel}."""
    notes: list[dict] = []
    warnings: list[str] = []
    for pat in track.patterns or []:
        if not isinstance(pat, dict):
            continue
        p_start = float(_pick(pat, "startPosition", "start", "position", default=0.0) or 0.0)
        p_unit_seconds = True  # region-like objects are in seconds in BandLab
        note_lists = []
        for key in ("notes", "events", "midiNotes"):
            v = pat.get(key)
            if isinstance(v, list):
                note_lists.append(v)
        if not note_lists:
            # Maybe the pattern references a stored score; keep going.
            warnings.append(f"pattern {pat.get('id')} has no inline notes")
            continue
        for lst in note_lists:
            for n in lst:
                if not isinstance(n, dict):
                    continue
                pitch = _pick(n, "pitch", "midiNote", "note", "noteNumber", "key")
                if pitch is None:
                    continue
                st = _pick(n, "startPosition", "start", "time", "position", default=0.0)
                dur = _pick(n, "duration", "length")
                en = _pick(n, "endPosition", "end")
                if dur is None and en is not None:
                    dur = float(en) - float(st)
                if dur is None:
                    dur = 0.25
                vel = _pick(n, "velocity", "vel", default=100)
                vel = float(vel)
                if vel <= 1.0:
                    vel = vel * 127.0
                # Heuristic for units: BandLab regions are seconds; pattern note
                # times are most likely beats. Values that look like beats keep
                # beats; decide by the pattern's own unit hint when present.
                unit = str(_pick(pat, "unit", "timeUnit", default="beats")).lower()
                if unit.startswith("sec"):
                    st_b = float(st) * bpm / 60.0
                    dur_b = float(dur) * bpm / 60.0
                else:
                    st_b = float(st)
                    dur_b = float(dur)
                p_off_b = p_start * bpm / 60.0 if p_unit_seconds else p_start
                notes.append(
                    {
                        "start_beats": p_off_b + st_b,
                        "dur_beats": max(dur_b, 1.0 / 64),
                        "pitch": int(round(float(pitch))),
                        "vel": int(max(1, min(127, round(vel)))),
                    }
                )
    return notes, warnings


def write_midi(path: Path, notes: list[dict], bpm: float, name: str, time_sig=(4, 4)) -> None:
    events: list[tuple[int, int, bytes]] = []  # (tick, order, bytes)
    for n in notes:
        on = int(round(n["start_beats"] * PPQ))
        off = int(round((n["start_beats"] + n["dur_beats"]) * PPQ))
        p = max(0, min(127, n["pitch"]))
        events.append((on, 1, bytes([0x90, p, n["vel"]])))
        events.append((max(off, on + 1), 0, bytes([0x80, p, 0])))
    events.sort(key=lambda e: (e[0], e[1]))
    track = bytearray()
    track += _vlq(0) + _meta(0x03, name.encode("utf-8", "replace"))
    track += _vlq(0) + _meta(0x51, struct.pack(">I", int(60_000_000 / bpm))[1:])
    den = {1: 0, 2: 1, 4: 2, 8: 3, 16: 4}.get(time_sig[1], 2)
    track += _vlq(0) + _meta(0x58, bytes([time_sig[0], den, 24, 8]))
    last = 0
    for tick, _, data in events:
        track += _vlq(tick - last) + data
        last = tick
    track += _vlq(0) + _meta(0x2F, b"")
    out = b"MThd" + struct.pack(">IHHH", 6, 0, 1, PPQ)
    out += b"MTrk" + struct.pack(">I", len(track)) + bytes(track)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out)


def track_to_midi(project: Project, track: Track, out: Path) -> dict[str, Any]:
    notes, warnings = extract_notes(track, project.bpm)
    write_json(out.with_suffix(".patterns.json"), track.patterns)
    write_midi(out, notes, project.bpm, track.name, project.time_sig)
    return {"notes": len(notes), "warnings": warnings, "trackId": track.id,
            "instrumentPreset": track.instrument_preset, "soundbank": track.soundbank}
