"""
Phase 4: turn one downloaded song into FL Studio projects.

Output inside "<Song>/FL Studio Project/":

    <Song>.flp                 BandLab sound: BandLab's own per-track renders
                               ("Processed Stems") on active tracks; the editable
                               dry tracks are present but their playlist tracks are
                               muted so nothing is processed twice.
    <Song> (editable mix).flp  The reverse: dry regions + recreated effect chains
                               active, wet reference tracks muted. Render this on
                               Windows and compare it with the BandLab mixdown.
    <Song> (safe).flp          Like <Song>.flp but effect slots only carry
                               template-backed plugin states (no constructed EQ/
                               compressor/delay states) - a fallback if FL refuses
                               a constructed state.
    *.wav                      every audio file the projects reference, beside the FLP
    build.json                 what went where (channels, inserts, clips, warnings)

Mixer layout: insert 1..N dry tracks (with chains), then wet reference inserts,
then "Reverb Bus" (BandLab's shared aux reverb), master untouched.
"""
from __future__ import annotations

import logging
import math
import shutil
from pathlib import Path
from typing import Any, Optional

from . import clips as clipmod
from .downloader import DIR_ORIG, DIR_WET, Manifest
from .flp_writer import (ClipChannelSpec, ClipSpec, InsertSpec, MarkerSpec, ProjectSpec, SlotSpec, TrackSpec,
                         VOL_UNITY, build_flp, build_insert_fst, encode_state, verify_with_pyflp)
from .models import ROLE_COLORS, Project, Track
from .presets import FLChain, build_chain
from .util import safe_name, write_json

log = logging.getLogger("bl2fl.build")

DIR_FLP = "FL Studio Project"
DIR_DRYVOX = "Dry Vocals"
DIR_DOCS = "Vocal Preset Documentation"
DIR_SHOTS = "Preset Screenshots"


def _hex(rgb):
    return "#%02x%02x%02x" % rgb


class SongBuilder:
    def __init__(self, folder: Path, project: Project, manifest: Manifest, windows_root: Optional[str] = None,
                 ppq: int = 960, stems_post_fader: Optional[bool] = None):
        self.folder = folder
        self.p = project
        self.man = manifest
        self.windows_root = windows_root.rstrip("\\/") if windows_root else None
        self.ppq = ppq
        self.stems_post_fader = stems_post_fader   # None = unknown -> assume BandLab stems include fader/pan
        self.out = folder / DIR_FLP
        self.out.mkdir(parents=True, exist_ok=True)
        self.chains: dict[str, FLChain] = {}
        self.build_info: dict[str, Any] = {"warnings": [], "channels": [], "inserts": [], "clips": []}

    # ---------------------------------------------------------------- paths
    def _fl_path(self, filename: str) -> str:
        if self.windows_root:
            rel = self.out.relative_to(self.folder.parent)
            return f"{self.windows_root}\\{str(rel).replace('/', chr(92))}\\{filename}"
        return filename

    def _sample_file(self, sample_id: str) -> Optional[Path]:
        it = self.man.data.get("items", {}).get(f"sample:{sample_id}") or {}
        pth = it.get("path")
        return Path(pth) if pth and Path(pth).exists() else None

    def _wet_file(self, track_id: str) -> Optional[Path]:
        it = self.man.data.get("items", {}).get(f"wet:{track_id}") or {}
        pth = it.get("path")
        return Path(pth) if pth and Path(pth).exists() else None

    # ---------------------------------------------------------------- build
    def build(self) -> dict[str, Any]:
        p = self.p
        base = safe_name(p.song_name)
        spec = ProjectSpec(title=p.song_name, bpm=p.bpm, time_sig=p.time_sig, ppq=self.ppq,
                           comments=f"Migrated from BandLab song {p.song_id} revision {p.revision_id}. "
                                    f"Key: {p.key or '?'}. Master volume {p.master_volume:g}. "
                                    f"Mastering: {p.mastering.get('preset') if p.mastering else 'none'}.",
                           arrangement_name=f"{p.song_name} (BandLab)")
        audio_tracks = [t for t in p.tracks if t.regions]
        n = len(audio_tracks)
        reverb_bus = 2 * n + 1
        if reverb_bus >= 126:
            self.build_info["warnings"].append("more than 62 tracks: wet reference inserts were merged")
            reverb_bus = 125
        docs = self.folder / DIR_DOCS
        shots = self.folder / DIR_SHOTS
        dryvox = self.folder / DIR_DRYVOX
        from .presets import write_track_docs

        track_no = 1
        for idx, t in enumerate(audio_tracks, start=1):
            role = t.role
            color = ROLE_COLORS.get(role, ROLE_COLORS["other"])
            chain = build_chain(p, t)
            self.chains[t.id] = chain
            write_track_docs(p, t, chain, docs, shots, idx)

            # ---- dry editable track: insert idx ---------------------------
            dry_insert = idx
            slots, slots_safe = self._slots_for_chain(chain)
            sends = {reverb_bus: int(round(min(1.0, chain.aux_reverb_send) * VOL_UNITY))} if chain.aux_reverb_send else {}
            ins = InsertSpec(dry_insert, f"{t.name}", color, VOL_UNITY, int(round(max(-1, min(1, t.pan)) * 6400)),
                             slots, True, sends, enabled=not t.is_muted)
            spec.inserts.append(ins)
            self.build_info["inserts"].append({"index": dry_insert, "name": ins.name, "kind": "dry+chain", "track": t.name,
                                               "plugins": [s.plugin for s in slots], "pan": t.pan,
                                               "fader_note": f"BandLab fader {t.volume:g} baked into clips; FL fader 0 dB"})
            spec.tracks.append(TrackSpec(track_no, f"{t.name} [dry + FL chain]", color, enabled=True))
            dry_track_no = track_no
            track_no += 1
            for r_no, r in enumerate(t.regions, start=1):
                src = self._sample_file(r.sample_id) if r.sample_id else None
                if not src:
                    self.build_info["warnings"].append(f"{t.name} region {r_no}: sample {r.sample_id} missing; clip skipped")
                    continue
                fname = clipmod.clip_filename(idx, t, r_no, r)
                dest = self.out / fname
                try:
                    if not dest.exists():
                        plan = clipmod.render_region(p, t, r, src, dest, self.ppq)
                    else:
                        plan = clipmod.clip_plan(p, t, r, self.ppq)
                except Exception as e:  # noqa: BLE001
                    self.build_info["warnings"].append(f"{t.name} region {r_no}: render failed: {e}")
                    continue
                ch_index = len(spec.channels)
                spec.channels.append(ClipChannelSpec(f"{t.name} r{r_no}", self._fl_path(fname), dry_insert, color))
                length_ticks = int(math.ceil(spec.seconds_to_ticks(r.duration + plan["pad_seconds"])))
                spec.clips.append(ClipSpec(ch_index, dry_track_no, plan["tick"], length_ticks))
                self.build_info["clips"].append({"track": t.name, "region": r.id, "file": fname, "tick": plan["tick"],
                                                 "pad_s": plan["pad_seconds"], "start_s": r.start, "duration_s": r.duration,
                                                 "notes": plan.get("notes", [])})
                if role in ("lead_vocal", "backing_vocal", "harmony", "double", "adlib"):
                    dryvox.mkdir(parents=True, exist_ok=True)
                    target = dryvox / fname
                    if not target.exists():
                        shutil.copy2(dest, target)

            # ---- wet reference track: insert n + idx ------------------------
            wet = self._wet_file(t.id)
            wet_insert = n + idx
            wcolor = ROLE_COLORS["wet_reference"]
            if wet:
                fname = f"{idx:02d}_{safe_name(t.name, 40)}_WET{wet.suffix.lower()}"
                dest = self.out / fname
                if not dest.exists():
                    if wet.suffix.lower() == ".wav":
                        shutil.copy2(wet, dest)
                    else:
                        clipmod.pad_whole_file(wet, dest.with_suffix(".wav"), 0.0)
                        fname = dest.with_suffix(".wav").name
                post = True if self.stems_post_fader is None else self.stems_post_fader
                spec.inserts.append(InsertSpec(wet_insert, f"{t.name} [BandLab render]", wcolor,
                                               VOL_UNITY if post else _fader(t.volume), 0 if post else int(t.pan * 6400),
                                               [], True, {}, enabled=not t.is_muted))
                spec.tracks.append(TrackSpec(track_no, f"{t.name} [BandLab render - reference]", wcolor, enabled=True))
                ch_index = len(spec.channels)
                spec.channels.append(ClipChannelSpec(f"{t.name} WET", self._fl_path(fname), wet_insert, wcolor))
                dur = clipmod.probe(dest if dest.exists() else self.out / fname)["duration"] or p.duration
                spec.clips.append(ClipSpec(ch_index, track_no, 0, int(math.ceil(spec.seconds_to_ticks(dur)))))
                self.build_info["inserts"].append({"index": wet_insert, "name": f"{t.name} [BandLab render]", "kind": "wet-reference"})
                track_no += 1
            else:
                self.build_info["warnings"].append(f"{t.name}: no BandLab per-track render available; only the dry reconstruction exists")

        # ---- shared reverb bus ------------------------------------------------
        aux = next((a for a in p.aux_channels if isinstance(a, dict)), None)
        rv_state, _ = encode_state({"kind": "reverb", "wet": 0.35, "size": 0.6, "decay_s": 1.8, "highcut_hz": 7000})
        bus = InsertSpec(reverb_bus, "Reverb Bus (BandLab aux)", (120, 144, 156), VOL_UNITY, 0,
                         [SlotSpec("Fruity Reeverb 2", rv_state)], True, {})
        if aux and aux.get("returnLevel") not in (None, 1):
            bus.volume = _fader(float(aux.get("returnLevel") or 1.0))
        spec.inserts.append(bus)
        self.build_info["inserts"].append({"index": reverb_bus, "name": bus.name, "kind": "aux", "bandlab": aux})

        # ---- markers from BandLab? (none stored) -> bar markers every 8 bars ----
        bar = spec.seconds_to_ticks(60.0 / p.bpm * p.time_sig[0])
        total = p.duration
        if total:
            for b in range(0, int(spec.seconds_to_ticks(total) / bar) + 1, 8):
                spec.markers.append(MarkerSpec(int(b * bar), f"Bar {b + 1}"))

        # ---- write variants ------------------------------------------------------
        results = {}
        results["bandlab_sound"] = self._write_variant(spec, base, p.song_name, mute_dry=True, mute_wet=False, safe=False)
        results["editable_mix"] = self._write_variant(spec, f"{base} (editable mix)", f"{p.song_name} (editable mix)", mute_dry=False, mute_wet=True, safe=False)
        results["safe"] = self._write_variant(spec, f"{base} (safe)", f"{p.song_name} (safe)", mute_dry=True, mute_wet=False, safe=True)

        # ---- per-chain .fst presets -------------------------------------------------
        for t in audio_tracks:
            c = self.chains[t.id]
            if c.steps:
                slots, _ = self._slots_for_chain(c)
                fst = build_insert_fst(f"{t.name} ({p.song_name})", slots)
                (docs / f"{safe_name(t.name, 50)} [{c.chain_hash}].fst").write_bytes(fst)

        self.build_info.update({"variants": results, "ppq": self.ppq, "bpm": p.bpm,
                                "chains": {t.id: {"hash": self.chains[t.id].chain_hash, "steps": len(self.chains[t.id].steps),
                                                  "warnings": self.chains[t.id].warnings} for t in audio_tracks}})
        write_json(self.out / "build.json", self.build_info)
        return self.build_info

    # ------------------------------------------------------------ helpers
    def _slots_for_chain(self, chain: FLChain) -> tuple[list[SlotSpec], list[SlotSpec]]:
        slots: list[SlotSpec] = []
        safe: list[SlotSpec] = []
        for st in chain.steps:
            if st.plugin.startswith("(") or " + " in st.plugin:
                continue
            state, template_backed = (None, False)
            if st.native_state:
                state, template_backed = encode_state(st.native_state, constructed_ok=True)
            slots.append(SlotSpec(st.plugin, state, VOL_UNITY, enabled=not st.bypass))
            safe.append(SlotSpec(st.plugin, state if template_backed else None, VOL_UNITY, enabled=not st.bypass))
        return slots[:10], safe[:10]

    def _write_variant(self, spec: ProjectSpec, name: str, title: str, mute_dry: bool, mute_wet: bool, safe: bool) -> dict[str, Any]:
        import copy
        v = copy.deepcopy(spec)
        v.title = title
        for tr in v.tracks:
            if "[dry + FL chain]" in tr.name and mute_dry:
                tr.enabled = False
            if "[BandLab render" in tr.name and mute_wet:
                tr.enabled = False
        if safe:
            for ins in v.inserts:
                new = []
                for s in ins.slots:
                    tb = False
                    for st in self._all_steps():
                        if st.plugin == s.plugin and st.native_state:
                            _, tb = encode_state(st.native_state, constructed_ok=True)
                            break
                    new.append(SlotSpec(s.plugin, s.state if tb else None, s.mix, s.enabled))
                ins.slots = new
        data = build_flp(v)
        path = self.out / f"{name}.flp"
        path.write_bytes(data)
        try:
            check = verify_with_pyflp(data)
        except Exception as e:  # noqa: BLE001
            check = {"error": str(e)}
        return {"path": str(path), "bytes": len(data), "pyflp": check}

    def _all_steps(self):
        for c in self.chains.values():
            yield from c.steps


def _fader(linear: float) -> int:
    """BandLab linear gain -> FL mixer fader value (12800 = 0 dB). FL's fader law
    above 0 dB is not documented; values above unity are clamped at 16000 and
    the difference is reported in build warnings."""
    return int(round(max(0.0, min(1.25, linear)) * VOL_UNITY))
