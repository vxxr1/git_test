"""
Render each BandLab region to a tick-aligned WAV clip with ffmpeg.

Why pre-render instead of trimming inside FL Studio: BandLab regions carry
sample offsets, loop lengths, playback-rate and pitch changes, region gain and
fades. Baking those into one file per region (plus the exact amount of
leading silence needed to land on an FL tick boundary) keeps timing sample-
accurate and independent of FL's own stretch/offset semantics.

Signal order (verified from BandLab behaviour notes): region gain -> track
fader -> effects. The track fader is therefore baked into the dry clips too,
so FL's mixer fader can sit at 0 dB and the recreated effect chain sees the
same level BandLab's chain saw.
"""
from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .models import Project, Region, Track
from .util import safe_name, short_id

log = logging.getLogger("bl2fl.clips")

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


def probe(path: Path) -> dict:
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout
        info = json.loads(out)
        st = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
        return {
            "duration": float(info.get("format", {}).get("duration") or st.get("duration") or 0.0),
            "sample_rate": int(st.get("sample_rate") or 44100),
            "channels": int(st.get("channels") or 2),
            "codec": st.get("codec_name"),
            "bit_depth": st.get("bits_per_sample") or st.get("bits_per_raw_sample"),
        }
    except (subprocess.CalledProcessError, ValueError, StopIteration) as e:
        log.warning("ffprobe failed for %s: %s", path, e)
        return {"duration": 0.0, "sample_rate": 44100, "channels": 2}


def has_filter(name: str) -> bool:
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        return f" {name} " in out
    except OSError:
        return False


_HAS_RUBBERBAND: Optional[bool] = None


def rubberband_available() -> bool:
    global _HAS_RUBBERBAND
    if _HAS_RUBBERBAND is None:
        _HAS_RUBBERBAND = has_filter("rubberband")
    return _HAS_RUBBERBAND


def clip_plan(project: Project, track: Track, region: Region, ppq: int) -> dict:
    """Decide where the clip lands and how much pre-roll silence it needs."""
    ticks_exact = region.start * project.bpm / 60.0 * ppq
    tick = int(math.floor(ticks_exact + 1e-6))
    tick_time = tick * 60.0 / (project.bpm * ppq)
    pad = max(0.0, region.start - tick_time)
    return {"tick": tick, "pad_seconds": pad, "duration": region.duration}


def render_region(
    project: Project,
    track: Track,
    region: Region,
    source: Path,
    out: Path,
    ppq: int,
    bake_track_volume: bool = True,
    sample_rate: Optional[int] = None,
) -> dict:
    """Write the region's audio to ``out`` and return the placement plan."""
    plan = clip_plan(project, track, region, ppq)
    src = probe(source)
    sr = sample_rate or src["sample_rate"] or 44100
    gain = float(region.gain or 1.0) * (float(track.volume) if bake_track_volume else 1.0)
    rate = float(region.playback_rate or 1.0)
    pitch = float(region.pitch_shift or 0.0)
    dur = region.duration
    if dur <= 0:
        raise ValueError(f"region {region.id} has non-positive duration")

    # Source span: the region shows `dur` seconds of timeline; at playback rate r
    # that consumes dur*r seconds of the sample (after the sample offset).
    src_needed = dur * rate
    loop = float(region.loop_length or 0.0)
    filters: list[str] = []
    inputs = [FFMPEG, "-hide_banner", "-v", "error", "-y"]
    if loop > 0 and loop < src_needed and src["duration"] > 0:
        # Looped region: repeat the loop section enough times, then trim.
        reps = int(math.ceil(src_needed / loop)) + 1
        inputs += ["-stream_loop", str(reps - 1), "-ss", f"{region.sample_offset:.6f}", "-t", f"{loop:.6f}", "-i", str(source)]
        filters.append(f"atrim=0:{src_needed:.6f}")
    else:
        inputs += ["-ss", f"{region.sample_offset:.6f}", "-t", f"{src_needed:.6f}", "-i", str(source)]
    notes: list[str] = []
    if abs(rate - 1.0) > 1e-6 or abs(pitch) > 1e-6:
        if region.pitch_time_correction and rubberband_available():
            # Independent time/pitch: tempo = rate, pitch in semitones.
            filters.append(f"rubberband=tempo={rate:.6f}:pitch={2 ** (pitch / 12.0):.6f}:pitchq=quality")
            notes.append(f"rubberband tempo {rate:g} pitch {pitch:+g} st")
        else:
            # Varispeed: resample so both pitch and speed change together.
            factor = rate * (2 ** (pitch / 12.0)) if not region.pitch_time_correction else rate
            filters.append(f"asetrate={sr * factor:.3f},aresample={sr}")
            if region.pitch_time_correction and abs(pitch) > 1e-6:
                filters.append(f"rubberband=pitch={2 ** (pitch / 12.0):.6f}") if rubberband_available() else None
            notes.append(f"varispeed factor {factor:g}")
    filters = [f for f in filters if f]
    if abs(gain - 1.0) > 1e-6:
        filters.append(f"volume={gain:.6f}")
    if region.fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={region.fade_in:.6f}")
    if region.fade_out > 0:
        filters.append(f"afade=t=out:st={max(0.0, dur - region.fade_out):.6f}:d={region.fade_out:.6f}")
    filters.append(f"atrim=0:{dur:.6f}")
    pad_samples = int(round(plan["pad_seconds"] * sr))
    if pad_samples > 0:
        filters.append(f"adelay=delays={pad_samples}S:all=1")
    filters.append(f"aformat=sample_rates={sr}")
    cmd = inputs + ["-af", ",".join(filters), "-ar", str(sr), "-c:a", "pcm_s24le", str(out)]
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, check=True, capture_output=True)
    plan.update({"gain_applied": gain, "notes": notes, "source": str(source), "sample_rate": sr,
                 "pad_samples": pad_samples, "ffmpeg": " ".join(cmd)})
    return plan


def pad_whole_file(source: Path, out: Path, pad_seconds: float, gain: float = 1.0) -> None:
    """Used for full-length stems that already start at 0 (pad is normally 0)."""
    src = probe(source)
    sr = src["sample_rate"] or 44100
    filters = []
    if abs(gain - 1.0) > 1e-6:
        filters.append(f"volume={gain:.6f}")
    if pad_seconds > 0:
        filters.append(f"adelay=delays={int(round(pad_seconds * sr))}S:all=1")
    cmd = [FFMPEG, "-hide_banner", "-v", "error", "-y", "-i", str(source)]
    if filters:
        cmd += ["-af", ",".join(filters)]
    cmd += ["-c:a", "pcm_s24le" if source.suffix.lower() != ".wav" or filters else "copy", str(out)]
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, check=True, capture_output=True)


def clip_filename(index: int, track: Track, region_no: int, region: Region) -> str:
    return f"{index:02d}_{safe_name(track.name, 40)}_r{region_no:02d}_{short_id(region.id, 6)}.wav"
