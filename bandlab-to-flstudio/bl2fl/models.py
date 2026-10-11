"""
Normalised view of a BandLab revision.

The raw revision JSON is always kept on disk untouched; these dataclasses only
make the parts the migration needs explicit and give every value a unit.

Units (verified against live BandLab data, see docs/RESEARCH.md):
  * region.start / end / sample_offset / loop_length ... seconds
  * track.volume / region.gain / master_volume ......... linear gain, 0..2 (1 = unity)
  * track.pan ........................................... -1 (L) .. +1 (R)
  * automation point positions .......................... beats (bpm-dependent)
  * effect params ....................................... whatever BandLab stores; see
                                                          presets.py for conversions
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Effect:
    slug: str
    bypass: bool = False
    params: dict[str, Any] = field(default_factory=dict)
    automation: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Region:
    id: str
    name: str
    sample_id: Optional[str]
    start: float
    end: float
    sample_offset: float = 0.0
    loop_length: float = 0.0
    playback_rate: float = 1.0
    pitch_shift: float = 0.0
    pitch_time_correction: bool = True
    gain: float = 1.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class AuxSend:
    id: str
    level: float = 0.0


@dataclass
class Track:
    id: str
    name: str
    type: str
    order: int
    volume: float = 1.0
    pan: float = 0.0
    is_muted: bool = False
    is_solo: bool = False
    color_name: Optional[str] = None
    fx_mix: float = 1.0
    effects: list[Effect] = field(default_factory=list)
    preset_name: Optional[str] = None
    preset_id: Optional[str] = None
    preset_link: Optional[str] = None
    aux_sends: list[AuxSend] = field(default_factory=list)
    automation: dict[str, list[dict]] = field(default_factory=dict)
    regions: list[Region] = field(default_factory=list)
    auto_pitch: Optional[dict[str, Any]] = None
    patterns: Optional[list[dict]] = None
    instrument_preset: Optional[str] = None
    soundbank: Optional[str] = None
    sampler_kit: Optional[Any] = None
    can_edit: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ helpers
    @property
    def is_audio(self) -> bool:
        return self.type in ("voice", "audio", "guitar", "guitars", "bass", "mic", "looper") or bool(
            self.regions and not self.patterns
        )

    @property
    def is_midi(self) -> bool:
        return bool(self.patterns) or self.type in (
            "piano", "synth-pads", "synth-bass", "synth-keys", "synth-leads", "drum-machine",
            "creators-kit", "sampler", "midi_instrument",
        )

    @property
    def end_time(self) -> float:
        return max((r.end for r in self.regions), default=0.0)

    @property
    def role(self) -> str:
        return classify_role(self)

    @property
    def has_vocal_processing(self) -> bool:
        return self.auto_pitch is not None or any(
            e.slug for e in self.effects if not e.bypass
        )


@dataclass
class Sample:
    id: str
    name: Optional[str] = None
    status: Optional[str] = None
    duration: Optional[float] = None
    file: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Project:
    song_id: str
    song_name: str
    song_slug: Optional[str]
    revision_id: str
    revision_created: Optional[str]
    bpm: float
    time_sig: tuple[int, int]
    key: Optional[str]
    master_volume: float
    mastering: dict[str, Any]
    mixdown: dict[str, Any]
    tracks: list[Track]
    aux_channels: list[dict[str, Any]]
    samples: dict[str, Sample]
    description: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        d = float((self.mixdown or {}).get("duration") or 0.0)
        return max(d, max((t.end_time for t in self.tracks), default=0.0))

    def seconds_to_beats(self, seconds: float) -> float:
        return seconds * self.bpm / 60.0

    def beats_to_seconds(self, beats: float) -> float:
        return beats * 60.0 / self.bpm


# ----------------------------------------------------------------- parsing
def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_revision(rev: dict[str, Any], song: Optional[dict[str, Any]] = None) -> Project:
    song = song or rev.get("song") or {}
    metro = rev.get("metronome") or {}
    sig = metro.get("signature") or {}
    tracks: list[Track] = []
    for i, t in enumerate(rev.get("tracks") or []):
        effects = [
            Effect(
                slug=str(e.get("slug") or ""),
                bypass=bool(e.get("bypass")),
                params=dict(e.get("params") or {}),
                automation=dict(e.get("automation") or {}),
                raw=e,
            )
            for e in (t.get("effects") or [])
            if isinstance(e, dict)
        ]
        regions = [
            Region(
                id=str(r.get("id") or f"region{j}"),
                name=str(r.get("name") or ""),
                sample_id=r.get("sampleId"),
                start=_f(r.get("startPosition")),
                end=_f(r.get("endPosition")),
                sample_offset=_f(r.get("sampleOffset")),
                loop_length=_f(r.get("loopLength")),
                playback_rate=_f(r.get("playbackRate"), 1.0) or 1.0,
                pitch_shift=_f(r.get("pitchShift")),
                pitch_time_correction=bool(r.get("pitchTimeCorrection", True)),
                gain=_f(r.get("gain"), 1.0),
                fade_in=_f(r.get("fadeIn")),
                fade_out=_f(r.get("fadeOut")),
                raw=r,
            )
            for j, r in enumerate(t.get("regions") or [])
            if isinstance(r, dict)
        ]
        regions.sort(key=lambda r: r.start)
        ed = t.get("effectsData") or {}
        tracks.append(
            Track(
                id=str(t.get("id") or f"track{i}"),
                name=str(t.get("name") or f"Track {i + 1}"),
                type=str(t.get("type") or "voice"),
                order=int(t.get("order") if t.get("order") is not None else i),
                volume=_f(t.get("volume"), 1.0),
                pan=_f(t.get("pan")),
                is_muted=bool(t.get("isMuted")),
                is_solo=bool(t.get("isSolo")),
                color_name=t.get("colorName"),
                fx_mix=_f(t.get("fxMix"), 1.0),
                effects=effects,
                preset_name=(ed.get("displayName") if isinstance(ed, dict) else None),
                preset_id=(ed.get("originalPresetId") if isinstance(ed, dict) else None),
                preset_link=(ed.get("link") if isinstance(ed, dict) else None),
                aux_sends=[
                    AuxSend(id=str(s.get("id")), level=_f(s.get("sendLevel", s.get("SendLevel"))))
                    for s in (t.get("auxSends") or [])
                    if isinstance(s, dict)
                ],
                automation=dict(t.get("automation") or {}),
                regions=regions,
                auto_pitch=t.get("autoPitch") if isinstance(t.get("autoPitch"), dict) else None,
                patterns=t.get("patterns") if isinstance(t.get("patterns"), list) else None,
                instrument_preset=t.get("preset"),
                soundbank=t.get("soundbank"),
                sampler_kit=t.get("samplerKit"),
                can_edit=bool(t.get("canEdit", True)),
                raw=t,
            )
        )
    tracks.sort(key=lambda t: t.order)
    samples: dict[str, Sample] = {}
    for s in rev.get("samples") or []:
        if isinstance(s, dict) and s.get("id"):
            samples[s["id"]] = Sample(
                id=s["id"],
                name=s.get("name"),
                status=s.get("status"),
                duration=_f(s.get("duration"), 0.0) or None,
                file=s.get("file") or s.get("audioUrl"),
                raw=s,
            )
    return Project(
        song_id=str(song.get("id") or rev.get("songId") or ""),
        song_name=str(song.get("name") or rev.get("name") or "Untitled"),
        song_slug=song.get("slug"),
        revision_id=str(rev.get("id") or ""),
        revision_created=rev.get("createdOn"),
        bpm=_f(metro.get("bpm"), 120.0) or 120.0,
        time_sig=(int(sig.get("notesCount") or 4), int(sig.get("noteValue") or 4)),
        key=rev.get("key"),
        master_volume=_f(rev.get("volume"), 1.0),
        mastering=dict(rev.get("mastering") or {}),
        mixdown=dict(rev.get("mixdown") or {}),
        tracks=tracks,
        aux_channels=list(rev.get("auxChannels") or []),
        samples=samples,
        description=rev.get("description"),
        raw=rev,
    )


# ------------------------------------------------------------- classification
_ROLE_RULES: list[tuple[str, re.Pattern]] = [
    ("adlib", re.compile(r"\b(ad[\s-]?libs?|adlib|hype|shouts?)\b", re.I)),
    ("harmony", re.compile(r"\b(harm(ony|onies)?|harms?)\b", re.I)),
    ("double", re.compile(r"\b(dbl|doubles?|dub|stack|layer)\b", re.I)),
    ("backing_vocal", re.compile(r"\b(bgv|bvs?|backing|background|bg\s*vox|back\s*vox|chorus\s*vox)\b", re.I)),
    ("lead_vocal", re.compile(r"\b(lead|main|verse|hook|chorus|vox|vocal|voc|vocals|rap|sing|melody|mic|take|voice|hook)\b", re.I)),
    ("drums", re.compile(r"\b(beat|drum|drums|808|kick|snare|hat|hi-?hat|perc|percussion|clap|trap|loop|instrumental|inst)\b", re.I)),
    ("bass", re.compile(r"\b(bass|sub)\b", re.I)),
    ("instrument", re.compile(r"\b(piano|keys|synth|guitar|gtr|pad|lead\s*synth|strings|organ|melody|sample|chords|flute|horn|brass|violin)\b", re.I)),
    ("fx", re.compile(r"\b(fx|sfx|effect|riser|impact|sweep|transition|noise|ambience|atmos)\b", re.I)),
]

VOCAL_ROLES = ("lead_vocal", "backing_vocal", "harmony", "double", "adlib")

ROLE_LABELS = {
    "lead_vocal": "Lead Vocal",
    "backing_vocal": "Backing Vocal",
    "harmony": "Harmony",
    "double": "Vocal Double",
    "adlib": "Ad-lib",
    "drums": "Beat / Drums",
    "bass": "Bass",
    "instrument": "Instrument",
    "fx": "Sound FX",
    "other": "Other",
}

# FL Studio colours (RGB) kept consistent across every project.
ROLE_COLORS = {
    "lead_vocal": (0xE5, 0x39, 0x35),
    "backing_vocal": (0xF4, 0x8F, 0xB1),
    "harmony": (0xCE, 0x93, 0xD8),
    "double": (0xFF, 0x8A, 0x65),
    "adlib": (0xFF, 0xB7, 0x4D),
    "drums": (0x42, 0xA5, 0xF5),
    "bass": (0x5C, 0x6B, 0xC0),
    "instrument": (0x66, 0xBB, 0x6A),
    "fx": (0x26, 0xC6, 0xDA),
    "other": (0x9E, 0x9E, 0x9E),
    "wet_reference": (0x78, 0x90, 0x9C),
}


def classify_role(track: Track) -> str:
    name = track.name or ""
    if track.is_midi and not track.regions:
        if track.type in ("drum-machine", "creators-kit", "sampler"):
            return "drums"
        if track.type == "synth-bass":
            return "bass"
        return "instrument"
    for role, rx in _ROLE_RULES:
        if rx.search(name):
            return role
    # Fall back on the kind of processing on the track.
    slugs = {e.slug for e in track.effects}
    if track.auto_pitch or slugs & {"deEsser", "autoPitch", "pitchShifter"}:
        return "lead_vocal"
    if track.type in ("voice", "mic"):
        # A plain recorded take with vocal-style chain is most likely a vocal.
        if slugs & {"compressor", "la2aCompr", "compr1176", "expGate", "backCompr"}:
            return "lead_vocal"
        return "other"
    if track.type in ("guitar", "guitars", "bass"):
        return "bass" if track.type == "bass" else "instrument"
    return "other"
