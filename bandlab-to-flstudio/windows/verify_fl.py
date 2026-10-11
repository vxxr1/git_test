"""
Windows-side Phase 5: render every generated FLP with FL Studio's command-line
renderer, compare each render with the BandLab mixdown, and write results.

    python windows\verify_fl.py "C:\\Users\\you\\Music\\BandLab to FL Studio" [--fl "C:\\Program Files\\Image-Line\\FL Studio 2024\\FL64.exe"]

FL Studio's documented switches (Image-Line, FL 12.3.1 notes):
    FL64.exe /R[filename] project.flp /Eformat   -> renders project to <filename>.wav

The script renders "<Song>.flp" (BandLab stems) and "<Song> (editable mix).flp"
(dry + recreated chains) and reports loudness, timing offset, residual and
band differences against "BandLab Reference Mix/*.wav|m4a". A project that
FL cannot open leaves no render; that is recorded as a verification failure
and the "(safe).flp" variant is tried next.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bl2fl.analysis import compare_mix  # noqa: E402


def find_fl() -> Path | None:
    import os
    for pf in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
        if not pf:
            continue
        for d in sorted(Path(pf, "Image-Line").glob("FL Studio*"), reverse=True):
            for n in ("FL64.exe", "FL.exe"):
                if (d / n).exists():
                    return d / n
    return None


def render(fl: Path, flp: Path, out_wav: Path, timeout: int = 1800) -> dict:
    cmd = [str(fl), f"/R{out_wav.with_suffix('')}", str(flp), "/Ewav"]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "cmd": cmd}
    ok = out_wav.exists() and out_wav.stat().st_size > 1000
    return {"ok": ok, "rc": rc, "seconds": round(time.time() - t0, 1), "cmd": cmd, "output": str(out_wav)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--fl", default=None)
    ap.add_argument("--only", default=None, help="substring filter on song folder")
    a = ap.parse_args()
    root = Path(a.root)
    fl = Path(a.fl) if a.fl else find_fl()
    if not fl or not fl.exists():
        print("FL Studio executable not found; pass --fl")
        return 1
    results = {"fl_exe": str(fl), "songs": []}
    for song_dir in sorted(p for p in root.iterdir() if p.is_dir() and (p / "FL Studio Project").exists()):
        if a.only and a.only.lower() not in song_dir.name.lower():
            continue
        proj = song_dir / "FL Studio Project"
        ref_dir = song_dir / "BandLab Reference Mix"
        ref = next(iter(sorted(ref_dir.glob("*.wav"))), None) or next(iter(sorted(ref_dir.glob("*.m4a"))), None)
        entry = {"song": song_dir.name, "renders": {}}
        for variant in sorted(proj.glob("*.flp")):
            out = proj / "Renders" / (variant.stem + " (FL render).wav")
            out.parent.mkdir(exist_ok=True)
            r = render(fl, variant, out)
            if r["ok"] and ref:
                try:
                    r["compare"] = compare_mix(ref, out)
                except Exception as e:  # noqa: BLE001
                    r["compare_error"] = str(e)
            entry["renders"][variant.name] = r
            print(f"{song_dir.name} / {variant.name}: {'rendered' if r['ok'] else 'FAILED'}"
                  + (f" -> {r['compare']['verdict']}" if r.get("compare") else ""))
        results["songs"].append(entry)
        (proj / "verification.json").write_text(json.dumps(entry, indent=2), "utf-8")
    (root / "Migration Report" / "fl_verification.json").write_text(json.dumps(results, indent=2), "utf-8")
    print("wrote", root / "Migration Report" / "fl_verification.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
