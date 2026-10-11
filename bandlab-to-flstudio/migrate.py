#!/usr/bin/env python3
"""
BandLab -> FL Studio migration driver.

    python migrate.py auth-check                       # confirms the token works, prints who you are
    python migrate.py inventory                        # lists every song into <root>/inventory.json
    python migrate.py run --one                        # migrate the first song (pilot)
    python migrate.py run --song "Song name"           # one specific song
    python migrate.py run                              # everything (resumable; skips finished work)
    python migrate.py report                           # (re)build the migration report

Root folder defaults to  ~/Music/BandLab to FL Studio  (override with --root).
Credentials: BANDLAB_REFRESH_TOKEN or BANDLAB_SESSION_KEY in the environment or
a .env file next to this script (never a password).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bl2fl import analysis  # noqa: E402
from bl2fl.bandlab_api import AuthError, BandLabClient, BandLabError, Session  # noqa: E402
from bl2fl.builder import SongBuilder  # noqa: E402
from bl2fl.downloader import DIR_DATA, DIR_REF, Manifest, SongDownloader, build_inventory, song_folder_name  # noqa: E402
from bl2fl.models import parse_revision  # noqa: E402
from bl2fl.presets import build_chain, write_shared_presets  # noqa: E402
from bl2fl.report import build_report  # noqa: E402
from bl2fl.util import read_json, setup_logging, write_json  # noqa: E402

log = logging.getLogger("bl2fl.migrate")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def default_root() -> Path:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    return home / "Music" / "BandLab to FL Studio"


def make_client(root: Path) -> BandLabClient:
    sess = Session.load(root / ".state" / "session.json")
    if not sess.has_credentials():
        raise AuthError(
            "No BandLab credentials. Log in at https://www.bandlab.com in your browser, open DevTools "
            "(F12) > Application > Cookies > https://www.bandlab.com and copy the value of 'refreshToken' "
            "into BANDLAB_REFRESH_TOKEN (in .env next to migrate.py). Never paste your password.")
    return BandLabClient(sess, on_progress=lambda m: log.info(m))


def stem_check_for(project, folder: Path, manifest: Manifest) -> dict:
    """Measure (never assume) whether BandLab's per-track renders are wet/post-fader."""
    results = {}
    for t in project.tracks:
        if not t.regions:
            continue
        wet = (manifest.data.get("items", {}).get(f"wet:{t.id}") or {}).get("path")
        if not wet or not Path(wet).exists():
            continue
        srcs = []
        for r in t.regions:
            it = manifest.data.get("items", {}).get(f"sample:{r.sample_id}") or {}
            if it.get("path") and Path(it["path"]).exists():
                srcs.append((Path(it["path"]), r.start, r.sample_offset, r.duration))
        if not srcs:
            continue
        try:
            results[t.name] = analysis.stem_vs_dry(Path(wet), srcs, t.volume)
        except Exception as e:  # noqa: BLE001
            results[t.name] = {"error": str(e)}
        if len(results) >= 3:      # three tracks are enough to characterise the export
            break
    verdicts = [r for r in results.values() if "effects_present" in r]
    summary = {
        "tracks_checked": list(results.keys()),
        "stems_contain_effects": bool(verdicts) and all(v["effects_present"] for v in verdicts),
        "stems_include_fader": bool(verdicts) and any(v["fader_in_stem"] for v in verdicts),
        "details": results,
    }
    write_json(folder / DIR_DATA / "stem_check.json", summary)
    return summary


def run_song(client, root: Path, entry: dict, windows_root: str | None, skip_wet: bool) -> dict:
    name = entry.get("name")
    res = {"name": name, "folder": entry.get("folder"), "status": "started"}
    t0 = time.time()
    try:
        dl = SongDownloader(client, root, want_wet_stems=not skip_wet, progress=lambda m: log.info(m))
        project, man, folder = dl.run(entry)
        res["manifest"] = man.data
        try:
            res["stem_check"] = stem_check_for(project, folder, man)
        except Exception as e:  # noqa: BLE001
            res["stem_check"] = {"error": str(e)}
        post_fader = None
        sc = res.get("stem_check") or {}
        if sc.get("tracks_checked"):
            post_fader = bool(sc.get("stems_include_fader")) or not any(
                abs(t.volume - 1.0) > 0.06 for t in project.tracks if t.name in sc["tracks_checked"])
        builder = SongBuilder(folder, project, man, windows_root=windows_root, stems_post_fader=post_fader)
        res["build"] = builder.build()
        res["chains"] = [(project.song_name, builder.chains[t.id]) for t in project.tracks if t.id in builder.chains]
        res["status"] = "migrated"
        man.data["status"] = "migrated"
        man.save()
    except (BandLabError, AuthError) as e:
        res["status"] = "failed"
        res["error"] = str(e)
        log.error("%s: %s", name, e)
    except Exception as e:  # noqa: BLE001
        res["status"] = "failed"
        res["error"] = f"{type(e).__name__}: {e}"
        log.exception("%s failed", name)
    res["seconds"] = round(time.time() - t0, 1)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["auth-check", "inventory", "run", "report", "rebuild"])
    ap.add_argument("--root", default=None, help="migration root folder (default: ~/Music/BandLab to FL Studio)")
    ap.add_argument("--song", action="append", default=[], help="song name (substring) or id; repeatable")
    ap.add_argument("--one", action="store_true", help="pilot: migrate only the first song")
    ap.add_argument("--windows-root", default=None,
                    help=r'the migration root as FL Studio will see it, e.g. "C:\Users\me\Music\BandLab to FL Studio"; '
                         "written into the FLPs as absolute sample paths (default: file names only)")
    ap.add_argument("--skip-wet-stems", action="store_true", help="do not ask BandLab to render per-track stems")
    ap.add_argument("--verbose", "-v", action="store_true")
    a = ap.parse_args()

    load_dotenv(Path(__file__).resolve().parent / ".env")
    root = Path(a.root) if a.root else default_root()
    root.mkdir(parents=True, exist_ok=True)
    setup_logging(a.verbose, root / "Migration Report" / "migration.log")
    windows_root = a.windows_root
    if windows_root is None and os.name == "nt":
        windows_root = str(root.resolve())

    if a.command == "rebuild":
        return rebuild(root, a, windows_root)

    try:
        client = make_client(root)
    except AuthError as e:
        print(f"\n{e}\n")
        return 2

    if a.command == "auth-check":
        try:
            me = client.me()
        except (AuthError, BandLabError) as e:
            print(f"Token rejected: {e}")
            return 2
        print(f"OK: logged in as @{me.get('username')} ({me.get('name')}), id {me.get('id')}")
        return 0

    inv_path = root / "inventory.json"
    if a.command == "inventory" or not inv_path.exists():
        inv = build_inventory(client, root)
        print(f"{len(inv['songs'])} songs found; written to {inv_path}")
        if a.command == "inventory":
            for s in inv["songs"]:
                print(f"  - {s['name']}  [{s['id']}]  revision {s.get('latestRevisionId')}")
            return 0
    inv = read_json(inv_path)

    if a.command == "report":
        return rebuild(root, a, windows_root, report_only=True)

    songs = inv["songs"]
    if a.song:
        wanted = [w.lower() for w in a.song]
        songs = [s for s in songs if any(w in (s.get("name") or "").lower() or w == (s.get("id") or "").lower() for w in wanted)]
        if not songs:
            print("No song matched", a.song)
            return 1
    if a.one:
        songs = songs[:1]

    results = []
    all_chains = []
    for i, entry in enumerate(songs, 1):
        log.info("=== [%d/%d] %s", i, len(songs), entry.get("name"))
        res = run_song(client, root, entry, windows_root, a.skip_wet_stems)
        all_chains += res.pop("chains", [])
        results.append(res)
        write_json(root / "Migration Report" / "progress.json", {"done": i, "total": len(songs), "results": [
            {k: v for k, v in r.items() if k != "manifest"} for r in results]})
    shared_dir = root / "Shared FL Studio Vocal Presets"
    shared_dir.mkdir(exist_ok=True)
    shared = write_shared_presets(all_chains, shared_dir)
    export_shared_fst(all_chains, shared, shared_dir)
    rep = build_report(root, inv, results, shared, read_json(root / "Migration Report" / "fl_verification.json"))
    print(f"\nMigrated {rep['projects_migrated']}/{len(songs)} song(s). Report: {root / 'Migration Report' / 'migration_report.md'}")
    failed = [r for r in results if r["status"] != "migrated"]
    for r in failed:
        print(f"  FAILED {r['name']}: {r.get('error')}")
    return 0 if not failed else 1


def export_shared_fst(all_chains, shared, shared_dir: Path) -> None:
    from bl2fl.flp_writer import SlotSpec, build_insert_fst, encode_state
    by_hash = {c.chain_hash: c for _, c in all_chains}
    for g in shared.get("groups", []):
        c = by_hash.get(g["chain_hash"])
        if not c:
            continue
        slots = []
        for st in c.steps:
            if st.plugin.startswith("(") or " + " in st.plugin:
                continue
            state = encode_state(st.native_state)[0] if st.native_state else None
            slots.append(SlotSpec(st.plugin, state, enabled=not st.bypass))
        (shared_dir / g["fst_file"]).write_bytes(build_insert_fst(g["name"], slots[:10]))


def rebuild(root: Path, a, windows_root, report_only: bool = False) -> int:
    """Offline: rebuild FLPs/docs/report from already downloaded data (no network)."""
    inv = read_json(root / "inventory.json", {"songs": [], "user": {}})
    results, all_chains = [], []
    for entry in inv.get("songs", []):
        folder = root / entry.get("folder", song_folder_name(entry))
        rev = read_json(folder / DIR_DATA / "bandlab_revision.json")
        if not rev:
            continue
        if a.song and not any(w.lower() in (entry.get("name") or "").lower() for w in a.song):
            continue
        man = Manifest.load(folder / "manifest.json")
        project = parse_revision(rev, entry.get("raw") or {})
        res = {"name": entry.get("name"), "folder": entry.get("folder"), "manifest": man.data,
               "stem_check": read_json(folder / DIR_DATA / "stem_check.json")}
        if not report_only:
            try:
                b = SongBuilder(folder, project, man, windows_root=windows_root)
                res["build"] = b.build()
                all_chains += [(project.song_name, b.chains[t.id]) for t in project.tracks if t.id in b.chains]
                res["status"] = "migrated"
            except Exception as e:  # noqa: BLE001
                res["status"] = "failed"; res["error"] = str(e)
                log.exception("rebuild failed for %s", entry.get("name"))
        else:
            res["build"] = read_json(folder / "FL Studio Project" / "build.json", {})
            res["status"] = man.data.get("status", "unknown")
            all_chains += [(project.song_name, build_chain(project, t)) for t in project.tracks if t.regions]
        results.append(res)
    shared_dir = root / "Shared FL Studio Vocal Presets"
    shared_dir.mkdir(exist_ok=True)
    shared = write_shared_presets(all_chains, shared_dir)
    if not report_only:
        export_shared_fst(all_chains, shared, shared_dir)
    build_report(root, inv, results, shared, read_json(root / "Migration Report" / "fl_verification.json"))
    print("report:", root / "Migration Report" / "migration_report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
