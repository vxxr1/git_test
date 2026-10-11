"""
Phase 3: capture every vocal/track effect chain and map it to FL Studio.

Input  : the normalised Project (every track's effects[] with exact params,
         AutoPitch block, fader, pan, sends, automation).
Output : * per-song  "Vocal Preset Documentation/<track>.md|json"
         * per-song  "Preset Screenshots/<track>.png"  (rendered parameter cards)
         * global    "Shared FL Studio Vocal Presets/presets.json|md"
           with one entry per distinct chain and the songs/tracks that use it
         * an FLChain spec per track consumed by flp_writer.py

BandLab effect parameters are in the units BandLab stores (dB, seconds, Hz,
0..1 mixes, or BandLab's own 0..10 knobs). Each mapping below says which
unit is assumed and flags anything that could not be converted faithfully.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .models import Effect, Project, Track, VOCAL_ROLES
from .util import lin_to_db, safe_name, write_json

# ----------------------------------------------------------------------------
# BandLab pedal catalogue: slug -> UI name, category, parameter units.
# Names marked (?) are best-effort; BandLab assembles the real names inside
# its audio engine at runtime, so they are not readable from the web bundle.
# ----------------------------------------------------------------------------
BANDLAB_EFFECTS: dict[str, dict[str, Any]] = {
    "expGate":           {"name": "Noise Gate", "cat": "dynamics", "units": {"threshold": "dB", "attack": "s", "release": "s"}},
    "compressor":        {"name": "Compressor", "cat": "dynamics", "units": {"threshold": "dB", "ratio": ":1", "attack": "s", "release": "s", "knee": "dB", "makeupGain": "x"}},
    "backCompr":         {"name": "Vocal Compressor (?)", "cat": "dynamics", "units": {"threshold": "BandLab knob", "ratio": ":1", "attack": "s", "release": "s", "makeupGain": "dB"}},
    "la2aCompr":         {"name": "Vintage Compressor (LA-2A style)", "cat": "dynamics", "units": {"threshold": "peak reduction 0-40", "makeupGain": "dB", "mode": "compress/limit"}},
    "compr1176":         {"name": "FET Compressor (1176 style)", "cat": "dynamics", "units": {"gain": "input 0-20", "makeup": "output dB", "attack": "1-7 (fast..slow)", "release": "1-7", "ratio": ":1"}},
    "multibandComp2":    {"name": "Multiband Compressor", "cat": "dynamics", "units": {}},
    "compander":         {"name": "Compander", "cat": "dynamics", "units": {}},
    "deEsser":           {"name": "De-Esser", "cat": "dynamics", "units": {"frequency": "Hz", "threshold": "dB"}},
    "antiSqueal":        {"name": "Anti Squeal / Feedback suppressor", "cat": "dynamics", "units": {"amount": "0-1"}},
    "cleanLimiter":      {"name": "Limiter", "cat": "dynamics", "units": {}},
    "masteringLimiter":  {"name": "Mastering Limiter", "cat": "dynamics", "units": {}},
    "rossCompr":         {"name": "Ross Compressor (pedal)", "cat": "dynamics", "units": {}},
    "threeBandEq":       {"name": "3-Band EQ", "cat": "eq", "units": {"lowGain": "dB", "midGain": "dB", "highGain": "dB", "midFreq": "Hz", "lowFreq": "Hz", "highFreq": "Hz", "midQ": "Q"}},
    "bossGE7":           {"name": "Graphic EQ (7-band, GE-7 style)", "cat": "eq", "units": {"gainAt100Hz": "dB", "gainAt200Hz": "dB", "gainAt400Hz": "dB", "gainAt800Hz": "dB", "gainAt1600Hz": "dB", "gainAt3200Hz": "dB", "gainAt6400Hz": "dB", "level": "dB"}},
    "visualEq":          {"name": "Visual EQ", "cat": "eq", "units": {}},
    "filter":            {"name": "Filter", "cat": "eq", "units": {"frequency": "Hz", "gain": "dB", "q": "Q", "filterType": "type"}},
    "autoFilter":        {"name": "Auto Filter", "cat": "modulation", "units": {}},
    "simpleStudioReverb": {"name": "Studio Reverb", "cat": "reverb", "units": {"mix": "0-10", "size": "0-10", "color": "0-10"}},
    "reverbHybrid":      {"name": "Hybrid Reverb", "cat": "reverb", "units": {"dryWetMix": "0-1", "roomSize": "0-10", "spread": "0-10", "dampening": "Hz", "irType": "IR"}},
    "univerb":           {"name": "Convolution Reverb (Univerb)", "cat": "reverb", "units": {"dryWetMix": "0-1", "irType": "IR"}},
    "springReverb2":     {"name": "Spring Reverb", "cat": "reverb", "units": {"intensity": "0-100", "mode": "mode"}},
    "oneKnobRoom":       {"name": "Room (one knob)", "cat": "reverb", "units": {"intensity": "0-100"}},
    "ddelay":            {"name": "Digital Delay", "cat": "delay", "units": {"time": "s", "regen": "%", "wet": "%", "dry": "%", "lpf": "Hz", "hpf": "Hz", "level": "dB"}},
    "ddelayPlus":        {"name": "Digital Delay+", "cat": "delay", "units": {"delayTimeMsec": "s", "feedback": "0-1", "wet": "0-1", "dry": "0-1", "lpf": "Hz", "hpf": "Hz", "spread": "0-1", "level": "dB"}},
    "ddelayPlus2":       {"name": "Digital Delay+ 2", "cat": "delay", "units": {"delayTimeMsec": "s", "feedback": "0-1", "wet": "0-1", "dry": "0-1", "lpf": "Hz", "hpf": "Hz", "spread": "0-1", "level": "dB"}},
    "beatSyncDelay2":    {"name": "Beat Sync Delay", "cat": "delay", "units": {"delayTime": "s", "feedback": "0-1", "dryWetMix": "0-1", "cutoff": "Hz"}},
    "pingpongDelay":     {"name": "Ping-Pong Delay", "cat": "delay", "units": {"delayTimeSec": "s", "feedback": "0-1", "dryWetMix": "0-1", "cutoff": "Hz", "spread": "0-1"}},
    "delay":             {"name": "Delay", "cat": "delay", "units": {"delayTime": "s", "feedback": "0-1", "dryWetMix": "0-1", "cutoff": "Hz"}},
    "reverser":          {"name": "Reverser", "cat": "delay", "units": {}},
    "overdrive":         {"name": "Overdrive", "cat": "distortion", "units": {"amount": "0-10", "tone": "Hz", "outputGain": "0-10"}},
    "tubeScreamer":      {"name": "Tube Screamer", "cat": "distortion", "units": {"drive": "0-10", "tone": "0-10", "level": "0-10"}},
    "tubeScreamer2":     {"name": "Tube Screamer 2", "cat": "distortion", "units": {}},
    "bossBD2":           {"name": "Blues Driver (BD-2 style)", "cat": "distortion", "units": {}},
    "lofi":              {"name": "Lo-Fi", "cat": "distortion", "units": {"dryWetMix": "0-1", "downsample": "x", "bitDepth": "bits"}},
    "tapeSimulator":     {"name": "Tape Simulator", "cat": "saturation", "units": {}},
    "exciter":           {"name": "Exciter", "cat": "saturation", "units": {}},
    "enhancer":          {"name": "Enhancer", "cat": "saturation", "units": {"dryWetMix": "0-1", "mode": "mode"}},
    "gain": {"name": "Gain", "cat": "utility", "units": {"gain": "dB"}},
    "gain2": {"name": "Gain", "cat": "utility", "units": {"gain": "dB"}},
    "stereoSpreader":    {"name": "Stereo Spreader", "cat": "stereo", "units": {}},
    "autoPan":           {"name": "Auto Pan", "cat": "modulation", "units": {}},
    "chorus":            {"name": "Chorus", "cat": "modulation", "units": {"rate": "Hz", "depth": "0-1"}},
    "bossCE5":           {"name": "Chorus Ensemble (CE-5 style)", "cat": "modulation", "units": {}},
    "dimensionD":        {"name": "Dimension D (chorus)", "cat": "modulation", "units": {"preset": "button"}},
    "phaser":            {"name": "Phaser", "cat": "modulation", "units": {"rate": "Hz"}},
    "ehSmallStone":      {"name": "Small Stone Phaser", "cat": "modulation", "units": {}},
    "tremolo":           {"name": "Tremolo", "cat": "modulation", "units": {"rate": "Hz", "depth": "0-1"}},
    "bossVB2":           {"name": "Vibrato (VB-2 style)", "cat": "modulation", "units": {}},
    "autoWah":           {"name": "Auto Wah", "cat": "modulation", "units": {}},
    "pitchShifter":      {"name": "Pitch Shifter", "cat": "pitch", "units": {"shift": "semitones", "mix": "0-1"}},
    "guitarAmp":         {"name": "Guitar Amp", "cat": "amp", "units": {}},
    "genericAmp":        {"name": "Amp", "cat": "amp", "units": {}},
    "guitarCab":         {"name": "Guitar Cabinet", "cat": "amp", "units": {}},
    "cabinet":           {"name": "Cabinet", "cat": "amp", "units": {}},
}

# FL Studio stock plugins we target (all ship with every FL edition that can
# open third-party projects; Fruity Parametric EQ 2 & Fruity Limiter need
# Producer Edition or higher, NewTone/Pitcher are included from Producer).
FL = {
    "eq":        "Fruity Parametric EQ 2",
    "comp":      "Fruity Compressor",
    "limiter":   "Fruity Limiter",
    "gate":      "Fruity Limiter",       # its noise-gate section
    "reverb":    "Fruity Reeverb 2",
    "delay":     "Fruity Delay 3",
    "pitch":     "Pitcher",
    "dist":      "Fruity Fast Dist",
    "blood":     "Fruity Blood Overdrive",
    "balance":   "Fruity Balance",
    "stereo":    "Fruity Stereo Enhancer",
    "chorus":    "Fruity Chorus",
    "phaser":    "Fruity Phaser",
    "flanger":   "Fruity Flanger",
    "filter":    "Fruity Filter",
    "softclip":  "Fruity Soft Clipper",
    "waveshaper": "Fruity WaveShaper",
    "deesser":   "Fruity Parametric EQ 2",  # dynamic band is not available; see notes
    "multiband": "Maximus",
    "lofi":      "Fruity Squeeze",
    "send":      "Fruity Send",
}


@dataclass
class FLStep:
    """One mixer slot in the recreated chain."""

    plugin: str
    settings: dict[str, Any] = field(default_factory=dict)   # human/UI units
    note: str = ""
    native_state: Optional[dict[str, Any]] = None             # fed to flp_writer encoders
    source_slug: str = ""
    bypass: bool = False
    exact: bool = True


@dataclass
class FLChain:
    track_name: str
    role: str
    steps: list[FLStep]
    fader_db: float
    pan: float
    bandlab_volume: float
    preset_name: Optional[str]
    preset_id: Optional[str]
    chain_hash: str
    warnings: list[str] = field(default_factory=list)
    aux_reverb_send: float = 0.0
    auto_pitch: Optional[dict[str, Any]] = None


# ----------------------------------------------------------- conversions
def _g(p: dict, k: str, default: float = 0.0) -> float:
    try:
        return float(p.get(k, default))
    except (TypeError, ValueError):
        return default


def _knob10_to_pct(v: float) -> float:
    return max(0.0, min(100.0, v * 10.0))


def _ms(seconds: float) -> float:
    return seconds * 1000.0


def map_effect(e: Effect, bpm: float) -> list[FLStep]:
    """Translate one BandLab effect into one or more FL stock plugin steps."""
    p = e.params or {}
    s = e.slug
    out: list[FLStep] = []
    bypass = bool(e.bypass)

    if s == "expGate":
        thr = _g(p, "threshold", -40)
        out.append(FLStep(FL["gate"], {
            "section": "Noise gate", "gate_threshold_dB": thr,
            "gate_release_ms": _ms(_g(p, "release", 0.2)),
            "attack_ms_note": f"BandLab attack {_ms(_g(p,'attack',0.005)):.1f} ms (Fruity Limiter gate has no attack control)",
            "comp_section": "off (threshold 0 dB, ratio 1:1)", "limiter_ceiling_dB": 0,
        }, note="Noise gate section of Fruity Limiter; gain reduction depth fixed by plugin.",
            native_state={"kind": "limiter_gate", "threshold_db": thr, "release_s": _g(p, "release", 0.2)},
            source_slug=s, bypass=bypass, exact=False))

    elif s == "compressor":
        thr, ratio = _g(p, "threshold", -20), _g(p, "ratio", 4)
        att, rel, knee = _g(p, "attack", 0.01), _g(p, "release", 0.1), _g(p, "knee", 6)
        makeup = _g(p, "makeupGain", 1.0)
        out.append(FLStep(FL["comp"], {
            "threshold_dB": thr, "ratio": f"{ratio:g}:1", "attack_ms": _ms(att), "release_ms": _ms(rel),
            "gain_dB": lin_to_db(makeup) if makeup > 0 else 0.0,
            "type": "Hard" if knee < 3 else ("Medium" if knee < 10 else "Soft"),
            "knee_note": f"BandLab knee {knee:g} dB -> nearest Fruity Compressor type",
        }, native_state={"kind": "compressor", "threshold_db": thr, "ratio": ratio, "attack_s": att,
                         "release_s": rel, "gain_db": lin_to_db(makeup) if makeup > 0 else 0.0, "knee_db": knee},
            source_slug=s, bypass=bypass, exact=True))

    elif s == "backCompr":
        # BandLab's vocal compressor exposes threshold on a 0..20 knob. Observed
        # projects: threshold 2.5..18.5, makeupGain 0..8.6 dB. We assume the knob
        # is "amount of compression" with 20 = most. Flagged inexact.
        amt = _g(p, "threshold", 10)
        thr_db = -1.0 * (6.0 + amt * 1.5)            # 20 -> -36 dB, 0 -> -6 dB
        ratio = _g(p, "ratio", 4)
        out.append(FLStep(FL["comp"], {
            "threshold_dB": round(thr_db, 1), "ratio": f"{ratio:g}:1",
            "attack_ms": _ms(_g(p, "attack", 0.02)), "release_ms": _ms(_g(p, "release", 0.4)),
            "gain_dB": _g(p, "makeupGain", 0), "type": "Medium",
            "assumption": f"BandLab threshold knob {amt:g}/20 mapped to {thr_db:.1f} dB; adjust by ear",
        }, native_state={"kind": "compressor", "threshold_db": thr_db, "ratio": ratio,
                         "attack_s": _g(p, "attack", 0.02), "release_s": _g(p, "release", 0.4),
                         "gain_db": _g(p, "makeupGain", 0), "knee_db": 6},
            source_slug=s, bypass=bypass, exact=False))

    elif s == "la2aCompr":
        pr = _g(p, "threshold", 20)                   # peak reduction 1..40
        mode = str(p.get("mode", "compress"))
        ratio = 3.0 if mode == "compress" else 12.0
        thr_db = -1.0 * (pr * 0.9)                    # 40 -> -36 dB
        out.append(FLStep(FL["comp"], {
            "threshold_dB": round(thr_db, 1), "ratio": f"{ratio:g}:1 ({mode} mode)",
            "attack_ms": 10, "release_ms": 500, "gain_dB": _g(p, "makeupGain", 0), "type": "Soft",
            "assumption": "LA-2A program-dependent optical behaviour approximated with slow soft-knee settings",
        }, native_state={"kind": "compressor", "threshold_db": thr_db, "ratio": ratio, "attack_s": 0.01,
                         "release_s": 0.5, "gain_db": _g(p, "makeupGain", 0), "knee_db": 15},
            source_slug=s, bypass=bypass, exact=False))

    elif s == "compr1176":
        ratio = float(str(p.get("ratio", "4")).replace(":1", "") or 4)
        inp = _g(p, "gain", 10)                       # input 1.5..18.6
        att = {1: 0.8, 2: 0.6, 3: 0.4, 4: 0.3, 5: 0.2, 6: 0.1, 7: 0.02}
        rel = {1: 1.1, 2: 0.9, 3: 0.7, 4: 0.5, 5: 0.3, 6: 0.15, 7: 0.05}
        a_ms = att.get(int(round(_g(p, "attack", 4))), 0.3)
        r_s = rel.get(int(round(_g(p, "release", 4))), 0.5)
        thr_db = -1.0 * (inp * 1.8)                   # more input -> deeper into compression
        out.append(FLStep(FL["comp"], {
            "threshold_dB": round(thr_db, 1), "ratio": f"{ratio:g}:1", "attack_ms": a_ms,
            "release_ms": r_s * 1000, "gain_dB": _g(p, "makeup", 0), "type": "Hard",
            "assumption": f"1176 input knob {inp:g} mapped to threshold {thr_db:.1f} dB",
        }, native_state={"kind": "compressor", "threshold_db": thr_db, "ratio": ratio, "attack_s": a_ms / 1000,
                         "release_s": r_s, "gain_db": _g(p, "makeup", 0), "knee_db": 0},
            source_slug=s, bypass=bypass, exact=False))

    elif s == "deEsser":
        f, thr = _g(p, "frequency", 7000), _g(p, "threshold", -20)
        cut = max(-12.0, min(0.0, thr / 3.0))         # -20 dB thr -> about -6.7 dB static cut
        out.append(FLStep(FL["eq"], {
            "band": f"peak @ {f:.0f} Hz, {cut:.1f} dB, Q~2",
            "assumption": "FL has no stock de-esser; a static high-shelf/peak cut at the de-ess "
                          "frequency is written. For a dynamic de-esser use Fruity Multiband "
                          "Compressor high band or a third-party de-esser.",
        }, native_state={"kind": "eq", "bands": [{"type": "peak", "freq": f, "gain_db": cut, "q": 2.0}]},
            source_slug=s, bypass=bypass, exact=False))

    elif s == "threeBandEq":
        bands = [
            {"type": "low_shelf", "freq": _g(p, "lowFreq", 220), "gain_db": _g(p, "lowGain"), "q": 0.7},
            {"type": "peak", "freq": _g(p, "midFreq", 2000), "gain_db": _g(p, "midGain"), "q": _g(p, "midQ", 1.2)},
            {"type": "high_shelf", "freq": _g(p, "highFreq", 7000), "gain_db": _g(p, "highGain"), "q": 0.7},
        ]
        out.append(FLStep(FL["eq"], {
            "low_shelf": f"{bands[0]['freq']:.0f} Hz {bands[0]['gain_db']:+.1f} dB",
            "mid_peak": f"{bands[1]['freq']:.0f} Hz {bands[1]['gain_db']:+.1f} dB Q {bands[1]['q']:.2f}",
            "high_shelf": f"{bands[2]['freq']:.0f} Hz {bands[2]['gain_db']:+.1f} dB",
            "note": "BandLab 3-band EQ gains beyond +-18 dB are clipped to Fruity PEQ2's range",
        }, native_state={"kind": "eq", "bands": bands}, source_slug=s, bypass=bypass, exact=True))

    elif s == "bossGE7":
        freqs = [100, 200, 400, 800, 1600, 3200, 6400]
        bands = [{"type": "peak", "freq": f, "gain_db": _g(p, f"gainAt{f}Hz"), "q": 1.4} for f in freqs]
        level = _g(p, "level", 0)
        out.append(FLStep(FL["eq"], {
            **{f"{f}Hz": f"{_g(p, f'gainAt{f}Hz'):+.1f} dB" for f in freqs},
            "output_level_dB": level,
            "note": "7 graphic bands -> 7 peak bands (Q 1.4, one octave). Output level on the EQ's main level.",
        }, native_state={"kind": "eq", "bands": bands, "main_level_db": level}, source_slug=s, bypass=bypass, exact=True))

    elif s == "filter":
        ft = str(p.get("filterType", "peaking"))
        t = {"peaking": "peak", "highshelf": "high_shelf", "lowshelf": "low_shelf", "bandpass": "band_pass",
             "lowpass": "low_pass", "highpass": "high_pass", "notch": "notch"}.get(ft, "peak")
        out.append(FLStep(FL["eq"], {
            "band": f"{ft} @ {_g(p,'frequency',1000):.0f} Hz {_g(p,'gain'):+.1f} dB Q {_g(p,'q',1):.2f}",
        }, native_state={"kind": "eq", "bands": [{"type": t, "freq": _g(p, "frequency", 1000),
                                                 "gain_db": _g(p, "gain"), "q": max(0.1, _g(p, "q", 1))}]},
            source_slug=s, bypass=bypass, exact=True))

    elif s == "visualEq":
        bands = []
        for i in range(8):
            if str(p.get(f"filter{i}Active", "false")).lower() != "true":
                continue
            bw = _g(p, f"filter{i}Bandwidth", 0.5)
            bands.append({"type": {"peaking": "peak", "highshelf": "high_shelf", "lowshelf": "low_shelf",
                                   "lowpass": "low_pass", "highpass": "high_pass"}.get(str(p.get(f"filter{i}Type", "peaking")), "peak"),
                          "freq": _g(p, f"filter{i}Freq", 1000), "gain_db": _g(p, f"filter{i}Gain"),
                          "q": 1.0 / max(0.05, bw)})
        out.append(FLStep(FL["eq"], {"bands": [f"{b['type']} {b['freq']:.0f} Hz {b['gain_db']:+.1f} dB" for b in bands]},
                          native_state={"kind": "eq", "bands": bands}, source_slug=s, bypass=bypass, exact=True))

    elif s == "simpleStudioReverb":
        mix, size, color = _g(p, "mix", 3), _g(p, "size", 3), _g(p, "color", 5)
        wet_pct = _knob10_to_pct(mix)
        out.append(FLStep(FL["reverb"], {
            "wet_pct": round(wet_pct, 1), "dry_pct": 100, "room_size_pct": round(_knob10_to_pct(size), 1),
            "decay_s": round(0.4 + size * 0.5, 2), "high_cut_Hz": round(2000 + color * 1400),
            "assumption": "BandLab 0-10 knobs mapped linearly: mix->wet %, size->room size & decay, color->high cut",
        }, native_state={"kind": "reverb", "wet": wet_pct / 100, "size": size / 10, "decay_s": 0.4 + size * 0.5,
                         "highcut_hz": 2000 + color * 1400}, source_slug=s, bypass=bypass, exact=False))

    elif s == "reverbHybrid":
        mix, size, spread = _g(p, "dryWetMix", 0.3), _g(p, "roomSize", 3), _g(p, "spread", 5)
        damp = _g(p, "dampening", 8000)
        ir = str(p.get("irType", "basic"))
        decay = {"roomShort": 0.8, "chamberShort": 1.2, "longPlateShort": 2.2, "basic": 1.5}.get(ir, 1.5) * (0.6 + size / 10)
        out.append(FLStep(FL["reverb"], {
            "wet_pct": round(mix * 100, 1), "dry_pct": 100, "room_size_pct": round(size * 10, 1),
            "decay_s": round(decay, 2), "high_damping_Hz": round(damp), "stereo_separation_pct": round(spread * 10),
            "ir_type": ir, "assumption": "IR character approximated by decay length",
        }, native_state={"kind": "reverb", "wet": mix, "size": size / 10, "decay_s": decay, "highcut_hz": damp,
                         "stereo": spread / 10}, source_slug=s, bypass=bypass, exact=False))

    elif s == "univerb":
        mix = _g(p, "dryWetMix", 0.3)
        out.append(FLStep(FL["reverb"], {"wet_pct": round(mix * 100, 1), "ir_type": p.get("irType"),
                                         "assumption": "convolution IR replaced by algorithmic reverb; use Fruity Convolver with a matching IR for closer results"},
                          native_state={"kind": "reverb", "wet": mix, "size": 0.5, "decay_s": 1.8, "highcut_hz": 8000},
                          source_slug=s, bypass=bypass, exact=False))

    elif s in ("springReverb2", "oneKnobRoom"):
        inten = _g(p, "intensity", 40) / 100.0
        out.append(FLStep(FL["reverb"], {"wet_pct": round(inten * 60, 1), "decay_s": 1.6 if s == "springReverb2" else 0.7,
                                         "assumption": "intensity -> wet level"},
                          native_state={"kind": "reverb", "wet": inten * 0.6, "size": 0.4, "decay_s": 1.6 if s == "springReverb2" else 0.7,
                                        "highcut_hz": 6000}, source_slug=s, bypass=bypass, exact=False))

    elif s in ("ddelay", "ddelayPlus", "ddelayPlus2", "beatSyncDelay2", "pingpongDelay", "delay"):
        beat_map = {"wholeNote": 4, "halfNote": 2, "quarterNote": 1, "eighthNote": 0.5, "sixteenthNote": 0.25,
                    "quarterNoteTriplet": 2 / 3, "eighthNoteTriplet": 1 / 3, "sixteenthNoteTriplet": 1 / 6,
                    "dottedEighthNote": 0.75, "dottedQuarterNote": 1.5}
        sel = str(p.get("delayUnitSelection", ""))
        beats = None
        if s == "ddelay":
            t_s = _g(p, "time", 0.3); fb = _g(p, "regen", 20) / 100; wet = _g(p, "wet", 20) / 100; dry = _g(p, "dry", 100) / 100
        elif s in ("ddelayPlus", "ddelayPlus2"):
            t_s = _g(p, "delayTimeMsec", 0.3); fb = _g(p, "feedback", 0.2); wet = _g(p, "wet", 0.2); dry = _g(p, "dry", 1)
            if sel == "delayTimeBeats": beats = beat_map.get(str(p.get("delayTimeBeats")), None)
        elif s == "beatSyncDelay2":
            t_s = _g(p, "delayTime", 0.3); fb = _g(p, "feedback", 0.2); wet = _g(p, "dryWetMix", 0.2); dry = 1.0
            if sel == "syncDelay": beats = beat_map.get(str(p.get("syncDelay")), None)
        elif s == "pingpongDelay":
            t_s = _g(p, "delayTimeSec", 0.3); fb = _g(p, "feedback", 0.2); wet = _g(p, "dryWetMix", 0.2); dry = 1.0
            if sel == "delayTimeBeats": beats = beat_map.get(str(p.get("delayTimeBeats")), None)
        else:
            t_s = _g(p, "delayTime", 0.3); fb = _g(p, "feedback", 0.2); wet = _g(p, "dryWetMix", 0.2); dry = 1.0
        if beats is not None:
            t_s = beats * 60.0 / bpm
        lpf = _g(p, "lpf", _g(p, "cutoff", 16000)) or 16000
        hpf = _g(p, "hpf", 0)
        out.append(FLStep(FL["delay"], {
            "time_ms": round(t_s * 1000, 1), "tempo_sync_beats": beats, "feedback_pct": round(fb * 100, 1),
            "wet_pct": round(wet * 100, 1), "dry_pct": round(dry * 100, 1), "feedback_lowpass_Hz": round(lpf),
            "feedback_highpass_Hz": round(hpf), "ping_pong": s == "pingpongDelay",
            "stereo_offset_note": "BandLab 'spread' approximated with Fruity Delay 3 stereo offset" if "spread" in p else "",
        }, native_state={"kind": "delay", "time_s": t_s, "beats": beats, "feedback": fb, "wet": wet, "dry": dry,
                         "lpf": lpf, "hpf": hpf, "pingpong": s == "pingpongDelay", "spread": _g(p, "spread", 0)},
            source_slug=s, bypass=bypass, exact=beats is not None or True))

    elif s in ("overdrive", "tubeScreamer", "tubeScreamer2", "bossBD2"):
        drive = _g(p, "amount", _g(p, "drive", 2)) / 10.0
        out.append(FLStep(FL["blood"], {"drive_pct": round(drive * 100), "tone_Hz": p.get("tone"),
                                        "output_level": p.get("outputGain", p.get("level")),
                                        "assumption": "pedal model replaced by Fruity Blood Overdrive; drive scaled 0-10 -> 0-100%"},
                          native_state={"kind": "blood", "drive": drive, "tone": _g(p, "tone", 8000)},
                          source_slug=s, bypass=bypass, exact=False))

    elif s == "lofi":
        out.append(FLStep(FL["lofi"], {"bit_depth": p.get("bitDepth"), "downsample": p.get("downsample"),
                                       "mix_pct": round(_g(p, "dryWetMix", 0.5) * 100)},
                          native_state=None, source_slug=s, bypass=bypass, exact=False,
                          note="Fruity Squeeze: set bits/downsample by hand (state format not generated)"))

    elif s in ("tapeSimulator", "exciter", "enhancer"):
        out.append(FLStep(FL["softclip"] if s == "tapeSimulator" else FL["stereo"] if s == "enhancer" else FL["eq"],
                          {"original": p, "assumption": "character effect; nearest stock plugin loaded with gentle default"},
                          native_state={"kind": "softclip", "threshold": 0.9} if s == "tapeSimulator" else None,
                          source_slug=s, bypass=bypass, exact=False))

    elif s in ("gain", "gain2"):
        g = _g(p, "gain", 0)
        out.append(FLStep(FL["balance"], {"volume_dB": g},
                          native_state={"kind": "balance", "gain_db": g, "pan": 0}, source_slug=s, bypass=bypass, exact=True))

    elif s == "stereoSpreader":
        out.append(FLStep(FL["stereo"], {"stereo_separation": p, "assumption": "frequency-split widening approximated"},
                          native_state={"kind": "stereo", "separation": -0.3}, source_slug=s, bypass=bypass, exact=False))

    elif s in ("chorus", "bossCE5", "dimensionD"):
        out.append(FLStep(FL["chorus"], {"rate_Hz": p.get("rate", p.get("speed")), "depth": p.get("depth"), "original": p},
                          native_state=None, source_slug=s, bypass=bypass, exact=False,
                          note="Fruity Chorus loaded with defaults; set rate/depth from the values listed"))

    elif s in ("phaser", "ehSmallStone"):
        out.append(FLStep(FL["phaser"], {"rate_Hz": p.get("rate", p.get("speed")), "original": p}, native_state=None,
                          source_slug=s, bypass=bypass, exact=False, note="set rate by hand"))

    elif s in ("tremolo", "autoPan", "bossVB2", "autoWah", "autoFilter", "reverser"):
        out.append(FLStep("Fruity Flanger" if s == "bossVB2" else "Fruity Love Philter" if s in ("autoWah", "autoFilter") else "Fruity Balance",
                          {"original": p, "assumption": "no 1:1 stock equivalent; see notes"}, native_state=None,
                          source_slug=s, bypass=bypass, exact=False,
                          note="Needs manual recreation (automation clip on volume/pan or Love Philter envelope)."))

    elif s == "pitchShifter":
        out.append(FLStep(FL["pitch"], {"shift_semitones": p.get("shift"), "mix_pct": round(_g(p, "mix", 1) * 100)},
                          native_state=None, source_slug=s, bypass=bypass, exact=False,
                          note="Use Pitcher (fine tune / octave) or Fruity Pitch Shifter; set shift by hand"))

    elif s == "multibandComp2":
        out.append(FLStep(FL["multiband"], {"original": p}, native_state=None, source_slug=s, bypass=bypass, exact=False,
                          note="Maximus loaded with defaults; copy band thresholds/ratios from the values listed"))

    elif s in ("cleanLimiter", "masteringLimiter"):
        out.append(FLStep(FL["limiter"], {"ceiling_dB": p.get("threshold", -0.3), "original": p},
                          native_state={"kind": "limiter", "ceiling_db": _g(p, "threshold", -0.3)}, source_slug=s, bypass=bypass, exact=False))

    elif s in ("guitarAmp", "genericAmp", "guitarCab", "cabinet"):
        out.append(FLStep("Fruity Blood Overdrive + Fruity Convolver", {"original": p},
                          native_state=None, source_slug=s, bypass=bypass, exact=False,
                          note="Amp/cab models have no stock equivalent; the wet stem preserves the sound."))

    else:
        out.append(FLStep("(no stock equivalent)", {"original": p}, native_state=None, source_slug=s, bypass=bypass, exact=False,
                          note=f"Unknown BandLab effect '{s}'; parameters recorded, wet stem preserves the sound."))
    return out


def map_auto_pitch(ap: dict[str, Any], song_key: Optional[str]) -> FLStep:
    """BandLab AutoPitch -> FL Pitcher (key/scale/speed)."""
    scale = str(ap.get("scale", "scale_chromatic"))
    tonic = str(ap.get("tonic", "tonic_undefined"))
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    tonic_idx = None
    for i, n in enumerate(names):
        if tonic.lower().replace("tonic_", "").replace("sharp", "#") in (n.lower(), n.lower().replace("#", "s")):
            tonic_idx = i
    if tonic_idx is None and isinstance(ap.get("target"), int):
        tonic_idx = int(ap["target"]) % 12
    intervals = {
        "scale_chromatic": list(range(12)), "scale_major": [0, 2, 4, 5, 7, 9, 11],
        "scale_minor": [0, 2, 3, 5, 7, 8, 10], "scale_major_pentatonic": [0, 2, 4, 7, 9],
        "scale_minor_pentatonic": [0, 3, 5, 7, 10],
    }.get(scale, list(range(12)))
    if scale == "scale_custom" and isinstance(ap.get("targetNotes"), list):
        intervals = [int(x) % 12 for x in ap["targetNotes"] if isinstance(x, (int, float))]
        tonic_idx = 0
    keys_on = [False] * 12
    for iv in intervals:
        keys_on[((tonic_idx or 0) + iv) % 12] = True
    amount = ap.get("intensity", ap.get("amount", ap.get("mix", 1.0)))
    resp = ap.get("responseTime", ap.get("speed"))
    settings = {
        "key": names[tonic_idx] if tonic_idx is not None else f"(song key {song_key})",
        "scale": scale.replace("scale_", ""), "notes_enabled": [names[i] for i in range(12) if keys_on[i]],
        "amount_or_intensity": amount, "response_time": resp, "algorithm": ap.get("algorithm"),
        "preset_slug": ap.get("slug"), "version": ap.get("version"), "all_bandlab_fields": ap,
        "assumption": "Pitcher 'Speed' set from BandLab response time (fast response = high speed). Pitcher has no formant/algorithm choices.",
    }
    native = {"kind": "pitcher", "keys_on": keys_on,
              "speed": _pitch_speed(resp), "amount": float(amount) if isinstance(amount, (int, float)) else 1.0}
    return FLStep(FL["pitch"], settings, note="AutoPitch -> Pitcher", native_state=native, source_slug="autoPitch", exact=False)


def _pitch_speed(resp: Any) -> float:
    try:
        r = float(resp)
    except (TypeError, ValueError):
        return 0.9
    # BandLab response time is seconds-ish (0 = hardest). Pitcher speed 1 = instant.
    return max(0.1, min(1.0, 1.0 - min(r, 1.0) * 0.8))


# ----------------------------------------------------------- chain builder
def chain_hash(track: Track) -> str:
    payload = [
        [e.slug, bool(e.bypass), {k: (round(v, 3) if isinstance(v, (int, float)) else v) for k, v in sorted((e.params or {}).items())}]
        for e in track.effects
    ]
    if track.auto_pitch:
        payload.append(["autoPitch", {k: v for k, v in sorted(track.auto_pitch.items()) if k not in ("id",)}])
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:12]


def build_chain(project: Project, track: Track) -> FLChain:
    steps: list[FLStep] = []
    warnings: list[str] = []
    if track.auto_pitch:
        steps.append(map_auto_pitch(track.auto_pitch, project.key))
    for e in track.effects:
        if not e.slug:
            continue
        for st in map_effect(e, project.bpm):
            steps.append(st)
            if not st.exact:
                warnings.append(f"{e.slug}: approximated ({st.plugin}) {st.note}".strip())
        if e.automation:
            warnings.append(f"{e.slug}: BandLab automates {sorted(e.automation.keys())}; recreate as FL automation clips")
    steps = _merge_eq_steps(steps)
    if track.fx_mix not in (None, 1.0):
        warnings.append(f"track fxMix={track.fx_mix:g} (chain wet/dry); FL applies effects 100% wet per slot - set slot mix knobs to {track.fx_mix*100:.0f}% if needed")
    send = 0.0
    for s in track.aux_sends:
        if s.level:
            send = max(send, s.level)
    if send:
        warnings.append(f"aux send to shared reverb bus level {send:g}; a send to the Reverb Bus insert is written")
    if track.automation:
        for lane, pts in track.automation.items():
            if isinstance(pts, list) and pts:
                warnings.append(f"{lane} automation with {len(pts)} points (positions in beats) -> see automation.json")
    return FLChain(
        track_name=track.name, role=track.role, steps=steps,
        fader_db=lin_to_db(track.volume), pan=track.pan, bandlab_volume=track.volume,
        preset_name=track.preset_name, preset_id=track.preset_id, chain_hash=chain_hash(track),
        warnings=warnings, aux_reverb_send=send, auto_pitch=track.auto_pitch,
    )


def _merge_eq_steps(steps: list[FLStep]) -> list[FLStep]:
    """Adjacent EQ conversions share one Fruity Parametric EQ 2 while it has free bands."""
    out: list[FLStep] = []
    for st in steps:
        prev = out[-1] if out else None
        if (prev is not None and st.plugin == FL["eq"] and prev.plugin == FL["eq"] and st.native_state and prev.native_state
                and prev.native_state.get("kind") == "eq" and st.native_state.get("kind") == "eq"
                and prev.bypass == st.bypass
                and len(prev.native_state.get("bands", [])) + len(st.native_state.get("bands", [])) <= 7):
            merged_bands = list(prev.native_state["bands"]) + list(st.native_state["bands"])
            prev.native_state = {"kind": "eq", "bands": merged_bands,
                                 "main_level_db": float(prev.native_state.get("main_level_db", 0)) + float(st.native_state.get("main_level_db", 0))}
            prev.settings = {**prev.settings, **{f"{st.source_slug}:{k}": v for k, v in st.settings.items()}}
            prev.note = (prev.note + " | " if prev.note else "") + f"merged with {st.source_slug}" + (f": {st.note}" if st.note else "")
            prev.source_slug = f"{prev.source_slug}+{st.source_slug}"
            prev.exact = prev.exact and st.exact
            continue
        out.append(st)
    return out


# ----------------------------------------------------------- documentation
def chain_to_dict(c: FLChain) -> dict[str, Any]:
    return {
        "track": c.track_name, "role": c.role, "bandlab_preset": {"name": c.preset_name, "id": c.preset_id},
        "chain_hash": c.chain_hash, "fader_dB": round(c.fader_db, 2), "bandlab_volume_linear": c.bandlab_volume,
        "pan": c.pan, "aux_reverb_send": c.aux_reverb_send,
        "steps": [
            {"order": i + 1, "bandlab_effect": s.source_slug,
             "bandlab_name": BANDLAB_EFFECTS.get(s.source_slug, {}).get("name", s.source_slug if s.source_slug != "autoPitch" else "AutoPitch"),
             "fl_plugin": s.plugin, "bypassed": s.bypass, "settings": s.settings, "exact": s.exact, "note": s.note}
            for i, s in enumerate(c.steps)
        ],
        "warnings": c.warnings,
    }


def write_track_docs(project: Project, track: Track, chain: FLChain, doc_dir: Path, shot_dir: Path, index: int) -> dict[str, Any]:
    base = f"{index:02d}_{safe_name(track.name, 50)}"
    data = chain_to_dict(chain)
    data["bandlab_raw"] = {
        "effects": [{"slug": e.slug, "bypass": e.bypass, "params": e.params, "automation": e.automation} for e in track.effects],
        "autoPitch": track.auto_pitch, "effectsData": {"displayName": track.preset_name, "originalPresetId": track.preset_id, "link": track.preset_link},
        "volume": track.volume, "pan": track.pan, "fxMix": track.fx_mix,
        "auxSends": [{"id": s.id, "sendLevel": s.level} for s in track.aux_sends], "automation": track.automation,
    }
    write_json(doc_dir / f"{base}.json", data)
    lanes = {k: v for k, v in (track.automation or {}).items() if isinstance(v, list) and v}
    fx_lanes = {e.slug: e.automation for e in track.effects if e.automation}
    if lanes or fx_lanes:
        write_json(doc_dir / f"{base}.automation.json", {
            "note": "positions are BEATS (bpm %g); value is linear gain (volume) or -1..1 (pan)" % project.bpm,
            "bpm": project.bpm, "track": lanes, "effects": fx_lanes,
            "seconds": {k: [{"seconds": project.beats_to_seconds(float(pt.get("position", 0))), "value": pt.get("value")}
                            for pt in v if isinstance(pt, dict)] for k, v in lanes.items()}})
    md = [f"# {track.name}  ({project.song_name})", "",
          f"* Role: **{track.role}**", f"* BandLab preset: **{track.preset_name or 'none'}** (id {track.preset_id or '-'})",
          f"* Fader: {track.volume:g} linear = {chain.fader_db:+.2f} dB; Pan: {track.pan:+.2f}; FX mix: {track.fx_mix:g}",
          f"* Aux reverb send: {chain.aux_reverb_send:g}", ""]
    if track.auto_pitch:
        md += ["## AutoPitch", "```json", json.dumps(track.auto_pitch, indent=2), "```", ""]
    md += ["## Effect chain (BandLab order)", "", "| # | BandLab effect | bypass | parameters |", "|---|---|---|---|"]
    for i, e in enumerate(track.effects, 1):
        nm = BANDLAB_EFFECTS.get(e.slug, {}).get("name", e.slug)
        md.append(f"| {i} | {nm} (`{e.slug}`) | {'yes' if e.bypass else 'no'} | `{json.dumps(e.params, sort_keys=True)}` |")
    md += ["", "## FL Studio recreation", "", "| # | FL plugin | settings | exact? | note |", "|---|---|---|---|---|"]
    for i, s in enumerate(chain.steps, 1):
        md.append(f"| {i} | {s.plugin} | `{json.dumps(s.settings, default=str)}` | {'yes' if s.exact else 'approx'} | {s.note} |")
    if chain.warnings:
        md += ["", "## Warnings / manual follow-up", ""] + [f"* {w}" for w in chain.warnings]
    (doc_dir / f"{base}.md").write_text("\n".join(md) + "\n", "utf-8")
    try:
        from . import render_cards
        render_cards.render_chain_card(project, track, chain, shot_dir / f"{base}.png")
    except Exception as e:  # pragma: no cover - matplotlib optional
        data["screenshot_error"] = str(e)
    return data


def write_shared_presets(all_chains: list[tuple[str, FLChain]], shared_dir: Path) -> dict[str, Any]:
    """Group identical chains across songs; emits presets.json + README.md."""
    groups: dict[str, dict[str, Any]] = {}
    for song, c in all_chains:
        if not c.steps:
            continue
        g = groups.setdefault(c.chain_hash, {"chain_hash": c.chain_hash, "bandlab_preset_names": set(), "used_by": [],
                                              "steps": chain_to_dict(c)["steps"], "warnings": c.warnings})
        if c.preset_name:
            g["bandlab_preset_names"].add(c.preset_name)
        g["used_by"].append({"song": song, "track": c.track_name, "role": c.role})
    out = []
    for g in groups.values():
        g["bandlab_preset_names"] = sorted(g["bandlab_preset_names"])
        g["name"] = (g["bandlab_preset_names"][0] if g["bandlab_preset_names"] else f"Chain {g['chain_hash']}")
        g["fst_file"] = f"{safe_name(g['name'])} [{g['chain_hash']}].fst"
        out.append(g)
    out.sort(key=lambda g: -len(g["used_by"]))
    write_json(shared_dir / "presets.json", out)
    md = ["# Shared vocal/effect presets", "",
          "One entry per distinct BandLab effect chain (identical parameters => same entry).",
          "Each entry lists every song/track that uses it, the FL Studio recreation, and what needs manual adjustment.",
          "The .fst next to this file is an FL Studio mixer-track state holding the recreated chain; drop it on a mixer insert.", ""]
    for g in out:
        md += [f"## {g['name']}  (`{g['chain_hash']}`)", "",
               f"Used by {len(g['used_by'])} track(s): " + ", ".join(f"{u['song']} / {u['track']}" for u in g["used_by"]), "",
               "| # | BandLab | FL plugin | settings | exact |", "|---|---|---|---|---|"]
        for s in g["steps"]:
            md.append(f"| {s['order']} | {s['bandlab_name']} | {s['fl_plugin']} | `{json.dumps(s['settings'], default=str)[:300]}` | {'yes' if s['exact'] else 'approx'} |")
        if g["warnings"]:
            md += ["", *[f"* {w}" for w in g["warnings"]]]
        md.append("")
    (shared_dir / "README.md").write_text("\n".join(md), "utf-8")
    return {"distinct_chains": len(out), "groups": out}
