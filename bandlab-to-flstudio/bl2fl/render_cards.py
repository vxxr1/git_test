"""
Render a PNG "parameter card" for a track's effect chain.

BandLab's UI cannot be screenshotted from a headless pipeline, so each card
shows exactly the values BandLab stores for the chain (preset name, every
effect in order with all parameters, AutoPitch, fader/pan/sends) next to the
FL Studio recreation. The card is generated, not a UI screenshot, and says so.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

from .models import Project, Track
from .presets import BANDLAB_EFFECTS, FLChain


def render_chain_card(project: Project, track: Track, chain: FLChain, out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    lines: list[tuple[str, str]] = []
    lines.append(("title", f"{project.song_name}  -  {track.name}"))
    lines.append(("sub", f"role: {track.role}   BandLab preset: {track.preset_name or 'none'}   "
                         f"fader {chain.fader_db:+.1f} dB   pan {track.pan:+.2f}   reverb send {chain.aux_reverb_send:g}"))
    if track.auto_pitch:
        ap = {k: v for k, v in track.auto_pitch.items() if k not in ("id",)}
        lines.append(("h", "AutoPitch"))
        for w in textwrap.wrap(json.dumps(ap, default=str), 110):
            lines.append(("p", w))
    lines.append(("h", "BandLab effect chain (signal order)"))
    if not track.effects:
        lines.append(("p", "(no effects)"))
    for i, e in enumerate(track.effects, 1):
        nm = BANDLAB_EFFECTS.get(e.slug, {}).get("name", e.slug)
        head = f"{i}. {nm}  [{e.slug}]" + ("   BYPASSED" if e.bypass else "")
        lines.append(("e", head))
        params = ", ".join(f"{k}={v:g}" if isinstance(v, (int, float)) else f"{k}={v}" for k, v in sorted(e.params.items()))
        for w in textwrap.wrap(params or "(no parameters)", 110):
            lines.append(("p", "      " + w))
    lines.append(("h", "FL Studio recreation"))
    for i, s in enumerate(chain.steps, 1):
        lines.append(("e", f"{i}. {s.plugin}" + ("" if s.exact else "   (approximation)")))
        settings = ", ".join(f"{k}={v}" for k, v in s.settings.items() if k not in ("original", "all_bandlab_fields"))
        for w in textwrap.wrap(settings, 110):
            lines.append(("p", "      " + w))
    if chain.warnings:
        lines.append(("h", "Notes"))
        for wmsg in chain.warnings:
            for w in textwrap.wrap("- " + wmsg, 110):
                lines.append(("p", w))
    lines.append(("foot", "Generated parameter card (values read from BandLab project data; not a UI screenshot)"))

    height = 0.32 * len(lines) + 0.8
    fig = plt.figure(figsize=(12, max(3.0, height)), dpi=120)
    fig.patch.set_facecolor("#1e1f24")
    y = 1.0 - 0.3 / height
    for kind, txt in lines:
        style = {"title": dict(size=14, weight="bold", color="#ffffff"),
                 "sub": dict(size=9, color="#b0b7c3"),
                 "h": dict(size=11, weight="bold", color="#f4b942"),
                 "e": dict(size=10, weight="bold", color="#8fd3ff"),
                 "p": dict(size=9, color="#e6e6e6", family="monospace"),
                 "foot": dict(size=8, color="#8a8f98", style="italic")}[kind]
        fig.text(0.03, y, txt, va="top", ha="left", **style)
        y -= 0.3 / height if kind != "h" else 0.36 / height
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=fig.get_facecolor())
    plt.close(fig)
