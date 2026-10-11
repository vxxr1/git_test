"""
Windows-side Phase 1 check: find FL Studio, its version, and installed plugins.

    python windows\fl_inventory.py  [--out "C:\\...\\Migration Report\\fl_inventory.json"]

Reads only; writes a JSON inventory used by the migration report.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


def find_fl_installs() -> list[dict]:
    found = []
    roots = [Path(p) for p in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                               os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))]
    for root in roots:
        il = root / "Image-Line"
        if not il.exists():
            continue
        for d in il.iterdir():
            if d.is_dir() and d.name.lower().startswith("fl studio"):
                exe = next((d / n for n in ("FL64.exe", "FL.exe") if (d / n).exists()), None)
                entry = {"folder": str(d), "exe": str(exe) if exe else None, "version": None, "edition": None}
                if exe and sys.platform == "win32":
                    try:
                        import ctypes
                        from ctypes import wintypes
                        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(exe), None)
                        if size:
                            buf = ctypes.create_string_buffer(size)
                            ctypes.windll.version.GetFileVersionInfoW(str(exe), 0, size, buf)
                            val = ctypes.c_void_p(); ln = wintypes.UINT()
                            ctypes.windll.version.VerQueryValueW(buf, "\\", ctypes.byref(val), ctypes.byref(ln))
                            fixed = ctypes.cast(val, ctypes.POINTER(ctypes.c_uint32 * 13)).contents
                            ms, ls = fixed[2], fixed[3]
                            entry["version"] = f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
                    except Exception as e:  # noqa: BLE001
                        entry["version_error"] = str(e)
                found.append(entry)
    if sys.platform == "win32":
        try:
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\Image-Line") as k:
                        i = 0
                        while True:
                            try:
                                name = winreg.EnumKey(k, i); i += 1
                                found.append({"registry": name})
                            except OSError:
                                break
                except OSError:
                    pass
        except ImportError:
            pass
    return found


def list_plugins(install: dict) -> dict:
    out = {"stock_effects": [], "stock_generators": [], "vst_effects": [], "vst_generators": [], "vst_scan_folders": []}
    folder = Path(install.get("folder") or "")
    for kind, key in (("Effects", "stock_effects"), ("Generators", "stock_generators")):
        d = folder / "Plugins" / "Fruity" / kind
        if d.exists():
            out[key] = sorted(p.name for p in d.iterdir() if p.is_dir())
    docs = Path(os.environ.get("USERPROFILE", "")) / "Documents" / "Image-Line"
    for base in docs.glob("FL Studio*/Presets/Plugin database/Installed"):
        for kind, key in (("Effects", "vst_effects"), ("Generators", "vst_generators")):
            for sub in ("VST", "VST3", "New"):
                d = base / kind / sub
                if d.exists():
                    out[key] += sorted(f"{sub}: {p.stem}" for p in d.glob("*.fst"))
    out["vst_scan_folders"] = [str(p) for p in docs.glob("FL Studio*/Presets/Plugin database")]
    return out


def check_capabilities(install: dict, plugins: dict) -> dict:
    wanted = ["Fruity Parametric EQ 2", "Fruity Compressor", "Fruity Limiter", "Fruity Reeverb 2", "Fruity Delay 3",
              "Pitcher", "NewTone", "Fruity Balance", "Fruity Blood Overdrive", "Fruity Stereo Enhancer",
              "Fruity Soft Clipper", "Fruity Chorus", "Fruity Phaser", "Maximus", "Fruity Convolver"]
    have = {n: any(n.lower() == e.lower() for e in plugins["stock_effects"] + plugins["stock_generators"]) for n in wanted}
    pitch = [e for e in plugins["vst_effects"] if re.search(r"auto-?tune|melodyne|waves tune|graillon|mautopitch|pitch", e, re.I)]
    return {"stock_plugins_present": have, "third_party_pitch_correction": pitch,
            "notes": ["Pitcher/NewTone need FL Studio Producer Edition or higher; Fruity Edition users get the "
                      "AutoPitch settings sheet only." if not have.get("Pitcher") else "Pitcher available for AutoPitch recreation"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="fl_inventory.json")
    a = ap.parse_args()
    installs = find_fl_installs()
    result = {"installs": installs, "plugins": {}, "capabilities": {}}
    for inst in installs:
        if inst.get("folder"):
            pl = list_plugins(inst)
            result["plugins"][inst["folder"]] = pl
            result["capabilities"][inst["folder"]] = check_capabilities(inst, pl)
    Path(a.out).write_text(json.dumps(result, indent=2), "utf-8")
    print(json.dumps(result, indent=2)[:4000])
    if not installs:
        print("\nFL Studio was not found under Program Files\\Image-Line. Install it or pass the folder manually.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
