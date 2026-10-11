"""Migration report (Markdown + JSON) assembled from every song's manifest/build info."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .util import read_json, write_json


def build_report(root: Path, inventory: dict[str, Any], song_results: list[dict[str, Any]],
                 shared: dict[str, Any], windows_results: dict[str, Any] | None = None) -> dict[str, Any]:
    rep: dict[str, Any] = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "bandlab_user": inventory.get("user"),
        "projects_discovered": len(inventory.get("songs", [])),
        "projects_migrated": 0, "projects_failed": 0,
        "exports": {"mixdown_m4a": 0, "mixdown_wav": 0, "original_samples": 0, "original_samples_lossy_fallback": 0,
                    "processed_stems": 0, "midi_files": 0},
        "failed_exports": [], "missing_tracks": [],
        "presets_total": 0, "presets_exact": 0, "presets_needing_manual_adjustment": [],
        "distinct_preset_chains": shared.get("distinct_chains", 0),
        "fl_verification": windows_results or {"status": "not run - requires FL Studio on Windows (see windows/README.md)"},
        "songs": [],
    }
    for s in song_results:
        man = s.get("manifest") or {}
        items = man.get("items", {})
        entry = {"song": s.get("name"), "folder": s.get("folder"), "status": s.get("status"),
                 "revision": man.get("revisionId"), "bpm": man.get("bpm"), "error": s.get("error"),
                 "stems": {"processed": 0, "failed": 0}, "samples": {"wav": 0, "m4a": 0, "failed": 0},
                 "warnings": (s.get("build") or {}).get("warnings", []),
                 "stem_check": s.get("stem_check"), "compare": s.get("compare")}
        if s.get("status") == "migrated":
            rep["projects_migrated"] += 1
        else:
            rep["projects_failed"] += 1
        for key, it in items.items():
            st = it.get("status")
            if key == "mixdown_m4a" and st == "done": rep["exports"]["mixdown_m4a"] += 1
            if key == "mixdown_wav" and st == "done": rep["exports"]["mixdown_wav"] += 1
            if key.startswith("sample:"):
                if st == "done" and it.get("kind") == "original-wav": rep["exports"]["original_samples"] += 1; entry["samples"]["wav"] += 1
                elif st == "done": rep["exports"]["original_samples_lossy_fallback"] += 1; entry["samples"]["m4a"] += 1
                else: entry["samples"]["failed"] += 1
            if key.startswith("wet:"):
                if st == "done": rep["exports"]["processed_stems"] += 1; entry["stems"]["processed"] += 1
                else: entry["stems"]["failed"] += 1
            if key.startswith("midi:") and st == "done": rep["exports"]["midi_files"] += 1
            if st in ("failed", "unavailable"):
                rep["failed_exports"].append({"song": s.get("name"), "item": key, "status": st,
                                              "error": it.get("error") or it.get("reason")})
        for w in entry["warnings"]:
            if "missing" in w or "no BandLab per-track render" in w:
                rep["missing_tracks"].append({"song": s.get("name"), "detail": w})
        for tid, c in ((s.get("build") or {}).get("chains") or {}).items():
            rep["presets_total"] += 1
            if not c.get("warnings"):
                rep["presets_exact"] += 1
            else:
                rep["presets_needing_manual_adjustment"].append({"song": s.get("name"), "track": tid, "notes": c["warnings"]})
        rep["songs"].append(entry)
    write_json(root / "Migration Report" / "migration_report.json", rep)
    (root / "Migration Report" / "migration_report.md").write_text(render_md(rep), "utf-8")
    return rep


def render_md(r: dict[str, Any]) -> str:
    L = [f"# BandLab -> FL Studio migration report", "", f"Generated {r['generated']} for BandLab user "
         f"@{(r.get('bandlab_user') or {}).get('username')}", "",
         "## Totals", "", "| metric | value |", "|---|---|",
         f"| Projects discovered | {r['projects_discovered']} |", f"| Projects migrated | {r['projects_migrated']} |",
         f"| Projects failed | {r['projects_failed']} |",
         f"| Mixdowns (M4A / WAV) | {r['exports']['mixdown_m4a']} / {r['exports']['mixdown_wav']} |",
         f"| Original samples (WAV / lossy fallback) | {r['exports']['original_samples']} / {r['exports']['original_samples_lossy_fallback']} |",
         f"| Processed stems rendered by BandLab | {r['exports']['processed_stems']} |",
         f"| MIDI files | {r['exports']['midi_files']} |",
         f"| Effect chains documented | {r['presets_total']} |",
         f"| Chains recreated with only exact conversions | {r['presets_exact']} |",
         f"| Chains needing manual adjustment | {len(r['presets_needing_manual_adjustment'])} |",
         f"| Distinct chains (shared presets) | {r['distinct_preset_chains']} |", ""]
    L += ["## FL Studio verification", "", "```", str(r["fl_verification"]), "```", ""]
    L += ["## Songs", ""]
    for s in r["songs"]:
        L += [f"### {s['song']}", "", f"* status: **{s['status']}**  folder: `{s['folder']}`  bpm: {s['bpm']}  revision: `{s['revision']}`",
              f"* samples: {s['samples']}  stems: {s['stems']}"]
        if s.get("error"):
            L.append(f"* error: {s['error']}")
        if s.get("stem_check"):
            L.append(f"* BandLab stem check: {s['stem_check']}")
        if s.get("compare"):
            L.append(f"* mix comparison: {s['compare']}")
        for w in s["warnings"]:
            L.append(f"* warning: {w}")
        L.append("")
    if r["failed_exports"]:
        L += ["## Failed or unavailable exports", ""] + [f"* {f['song']}: {f['item']} ({f['status']}) {f['error'] or ''}" for f in r["failed_exports"]] + [""]
    if r["missing_tracks"]:
        L += ["## Missing tracks", ""] + [f"* {m['song']}: {m['detail']}" for m in r["missing_tracks"]] + [""]
    if r["presets_needing_manual_adjustment"]:
        L += ["## Presets requiring manual adjustment", ""]
        for p in r["presets_needing_manual_adjustment"]:
            L.append(f"* {p['song']} / track {p['track']}:")
            L += [f"    * {n}" for n in p["notes"]]
    return "\n".join(L) + "\n"
