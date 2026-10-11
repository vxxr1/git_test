"""
Objective audio checks.

* stem_vs_dry():  does a BandLab "Processed Stem" actually contain the effect
                  chain and the fader?  (never assumed - measured)
* compare_mix():  BandLab mixdown vs an FL Studio render - alignment offset,
                  integrated loudness, true-peak/clipping, RMS per band,
                  stereo width, residual after level matching, and a verdict.

Everything is computed with numpy on decoded PCM; ffmpeg does decoding and
EBU R128 loudness so M4A, WAV and FLAC are all accepted.
"""
from __future__ import annotations

import json
import logging
import math
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .clips import FFMPEG, probe

log = logging.getLogger("bl2fl.analysis")


def decode(path: Path, sr: int = 44100, mono: bool = False, max_seconds: Optional[float] = None) -> np.ndarray:
    """Decode to float32 numpy array shaped (n, channels)."""
    ch = 1 if mono else 2
    cmd = [FFMPEG, "-hide_banner", "-v", "error", "-i", str(path)]
    if max_seconds:
        cmd += ["-t", f"{max_seconds}"]
    cmd += ["-f", "f32le", "-acodec", "pcm_f32le", "-ac", str(ch), "-ar", str(sr), "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    arr = np.frombuffer(raw, dtype=np.float32)
    return arr.reshape(-1, ch)


def loudness(path: Path) -> dict[str, float]:
    """Integrated loudness / LRA / true peak via ffmpeg ebur128."""
    cmd = [FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-filter_complex", "ebur128=peak=true", "-f", "null", "-"]
    txt = subprocess.run(cmd, capture_output=True, text=True).stderr
    out: dict[str, float] = {}
    tail = txt[txt.rfind("Summary:"):] if "Summary:" in txt else txt
    for key, rx in (("integrated_lufs", r"I:\s+(-?[\d.]+) LUFS"), ("lra_lu", r"LRA:\s+(-?[\d.]+) LU"),
                    ("true_peak_dbtp", r"Peak:\s+(-?[\d.]+) dBFS")):
        m = re.search(rx, tail)
        if m:
            out[key] = float(m.group(1))
    return out


def rms_db(x: np.ndarray) -> float:
    if x.size == 0:
        return -120.0
    r = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return 20 * math.log10(r) if r > 1e-9 else -120.0


def best_offset(a: np.ndarray, b: np.ndarray, sr: int, max_shift_s: float = 2.0) -> tuple[int, float]:
    """Lag (samples) that best aligns b to a, via FFT cross-correlation on mono mixes."""
    n = min(len(a), len(b), sr * 120)
    x = a[:n].mean(axis=1) if a.ndim > 1 else a[:n]
    y = b[:n].mean(axis=1) if b.ndim > 1 else b[:n]
    x = x - x.mean(); y = y - y.mean()
    if np.max(np.abs(x)) < 1e-6 or np.max(np.abs(y)) < 1e-6:
        return 0, 0.0
    size = 1 << int(math.ceil(math.log2(2 * n)))
    X = np.fft.rfft(x, size); Y = np.fft.rfft(y, size)
    corr = np.fft.irfft(X * np.conj(Y), size)
    max_shift = int(max_shift_s * sr)
    lags = np.concatenate([np.arange(0, max_shift + 1), np.arange(-max_shift, 0)])
    vals = np.concatenate([corr[: max_shift + 1], corr[-max_shift:]])
    k = int(np.argmax(vals))
    denom = math.sqrt(float(np.dot(x, x)) * float(np.dot(y, y))) or 1.0
    return int(lags[k]), float(vals[k] / denom)


def align(a: np.ndarray, b: np.ndarray, lag: int) -> tuple[np.ndarray, np.ndarray]:
    if lag > 0:
        b = np.concatenate([np.zeros((lag, b.shape[1]), dtype=b.dtype), b])
    elif lag < 0:
        b = b[-lag:]
    n = min(len(a), len(b))
    return a[:n], b[:n]


def band_rms(x: np.ndarray, sr: int) -> dict[str, float]:
    mono = x.mean(axis=1) if x.ndim > 1 else x
    spec = np.abs(np.fft.rfft(mono * np.hanning(len(mono)))) ** 2
    freqs = np.fft.rfftfreq(len(mono), 1 / sr)
    bands = {"sub_20_60": (20, 60), "bass_60_250": (60, 250), "lowmid_250_1k": (250, 1000),
             "mid_1k_4k": (1000, 4000), "high_4k_10k": (4000, 10000), "air_10k_20k": (10000, 20000)}
    total = spec.sum() or 1.0
    return {k: 10 * math.log10(max(spec[(freqs >= lo) & (freqs < hi)].sum() / total, 1e-12)) for k, (lo, hi) in bands.items()}


def stereo_width(x: np.ndarray) -> float:
    if x.ndim < 2 or x.shape[1] < 2:
        return 0.0
    mid = (x[:, 0] + x[:, 1]) / 2
    side = (x[:, 0] - x[:, 1]) / 2
    m, s = rms_db(mid), rms_db(side)
    return s - m


def clipping(x: np.ndarray, thresh: float = 0.999) -> dict[str, Any]:
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    n = int(np.sum(np.abs(x) >= thresh))
    return {"peak": peak, "samples_at_ceiling": n, "clipped": n > 10}


def stem_vs_dry(wet_path: Path, dry_sources: list[tuple[Path, float, float, float]], track_volume: float,
                sr: int = 44100) -> dict[str, Any]:
    """Compare a BandLab per-track render with the dry regions laid on a timeline.

    dry_sources: [(sample_path, start_s, sample_offset_s, duration_s), ...]
    Returns whether the stem contains processing (spectral/residual difference)
    and the gain relation to the dry signal (fader baked in or not).
    """
    wet = decode(wet_path, sr)
    total = len(wet)
    dry = np.zeros_like(wet)
    for path, start, off, dur in dry_sources:
        seg = decode(path, sr)
        s0 = int(round(start * sr)); o0 = int(round(off * sr)); n = int(round(dur * sr))
        seg = seg[o0:o0 + n]
        end = min(total, s0 + len(seg))
        if end > s0:
            dry[s0:end] += seg[: end - s0]
    lag, corr = best_offset(wet, dry, sr, 0.5)
    a, b = align(wet, dry, lag)
    pb = float(np.dot(b.ravel(), b.ravel())) or 1e-12
    g = float(np.dot(a.ravel(), b.ravel())) / pb               # least-squares gain wet ~= g*dry
    resid = a - g * b
    resid_db = rms_db(resid) - rms_db(a) if a.size else 0.0
    bands_w, bands_d = band_rms(a, sr), band_rms(b, sr)
    spectral_diff = max(abs(bands_w[k] - bands_d[k]) for k in bands_w)
    processed = resid_db > -20.0 or spectral_diff > 2.0 or corr < 0.9
    gain_db = 20 * math.log10(abs(g)) if abs(g) > 1e-6 else -120.0
    fader_db = 20 * math.log10(track_volume) if track_volume > 0 else -120.0
    return {
        "lag_samples": lag, "lag_ms": lag * 1000.0 / sr, "correlation": round(corr, 4),
        "fit_gain_db": round(gain_db, 2), "track_fader_db": round(fader_db, 2),
        "fader_in_stem": abs(gain_db - fader_db) < 1.0 and abs(fader_db) > 0.5,
        "residual_db_rel": round(resid_db, 2), "max_band_diff_db": round(spectral_diff, 2),
        "stem_rms_db": round(rms_db(a), 2), "dry_rms_db": round(rms_db(b), 2),
        "effects_present": bool(processed),
        "verdict": ("stem is PROCESSED (effects baked in)" if processed else "stem matches DRY audio (no effects)")
                   + ("; fader level included" if abs(gain_db - fader_db) < 1.0 else "; level differs from fader -> see fit_gain_db"),
    }


def compare_mix(reference: Path, candidate: Path, sr: int = 44100) -> dict[str, Any]:
    ref = decode(reference, sr)
    cand = decode(candidate, sr)
    lag, corr = best_offset(ref, cand, sr, 5.0)
    a, b = align(ref, cand, lag)
    la, lb = loudness(reference), loudness(candidate)
    gain = 10 ** ((la.get("integrated_lufs", -14) - lb.get("integrated_lufs", -14)) / 20)
    resid = a - b * gain
    out = {
        "reference": str(reference), "candidate": str(candidate),
        "duration_ref_s": round(len(ref) / sr, 3), "duration_cand_s": round(len(cand) / sr, 3),
        "alignment_offset_ms": round(lag * 1000.0 / sr, 2), "alignment_correlation": round(corr, 4),
        "loudness_ref": la, "loudness_cand": lb,
        "loudness_diff_lu": round(la.get("integrated_lufs", 0) - lb.get("integrated_lufs", 0), 2),
        "level_matched_residual_db": round(rms_db(resid) - rms_db(a), 2),
        "band_diff_db": {k: round(band_rms(b, sr)[k] - band_rms(a, sr)[k], 2) for k in band_rms(a, sr)},
        "stereo_width_ref_db": round(stereo_width(a), 2), "stereo_width_cand_db": round(stereo_width(b), 2),
        "clipping_ref": clipping(a), "clipping_cand": clipping(b),
    }
    issues = []
    if abs(out["alignment_offset_ms"]) > 5:
        issues.append(f"timing offset {out['alignment_offset_ms']} ms")
    if abs(out["duration_ref_s"] - out["duration_cand_s"]) > 1.0:
        issues.append("length differs by more than 1 s (missing audio or extra tail)")
    if abs(out["loudness_diff_lu"]) > 1.0:
        issues.append(f"loudness differs by {out['loudness_diff_lu']} LU")
    if out["level_matched_residual_db"] > -12:
        issues.append("content differs noticeably after level match (effects/balance)")
    big = {k: v for k, v in out["band_diff_db"].items() if abs(v) > 2.0}
    if big:
        issues.append(f"tonal balance differs: {big}")
    if abs(out["stereo_width_ref_db"] - out["stereo_width_cand_db"]) > 3:
        issues.append("stereo width differs")
    if out["clipping_cand"]["clipped"] and not out["clipping_ref"]["clipped"]:
        issues.append("FL render clips")
    out["issues"] = issues
    out["verdict"] = "close match" if not issues else ("minor differences" if len(issues) <= 2 and out["level_matched_residual_db"] < -8 else "audible differences")
    return out
