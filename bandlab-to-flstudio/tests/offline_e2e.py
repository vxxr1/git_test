"""
Offline end-to-end exercise of the whole pipeline with a synthetic BandLab song.

No network: a fake client serves synthetic WAV/M4A files for the CDN/render
URLs, and a hand-written revision JSON stands in for the API response. This
proves download bookkeeping, resumability, clip rendering, FLP generation
(validated with PyFLP), preset docs, stem analysis and the report.

    python tests/offline_e2e.py [workdir]
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bl2fl import analysis  # noqa: E402
from bl2fl.builder import SongBuilder  # noqa: E402
from bl2fl.downloader import SongDownloader, Manifest  # noqa: E402
from bl2fl.models import parse_revision  # noqa: E402
from bl2fl.presets import write_shared_presets  # noqa: E402
from bl2fl.report import build_report  # noqa: E402
from bl2fl.util import write_json  # noqa: E402

FF = shutil.which("ffmpeg") or "ffmpeg"


def synth(path: Path, kind: str, seconds: float, freq: float = 220.0, sr: int = 44100):
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "tone":
        src = f"sine=frequency={freq}:sample_rate={sr}:duration={seconds}"
        af = "aformat=channel_layouts=stereo,volume=0.3,tremolo=f=2:d=0.6"
    else:
        src = f"anoisesrc=color=pink:sample_rate={sr}:duration={seconds}:seed=7"
        af = "aformat=channel_layouts=stereo,volume=0.2,lowpass=f=4000"
    codec = ["-c:a", "aac", "-b:a", "192k"] if path.suffix == ".m4a" else ["-c:a", "pcm_s16le"]
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", src, "-af", af, *codec, str(path)], check=True)


def process(src: Path, dst: Path, chain: str):
    subprocess.run([FF, "-v", "error", "-y", "-i", str(src), "-af", chain, "-c:a", "pcm_s16le", str(dst)], check=True)


REVISION = {
    "id": "rev-0001", "createdOn": "2026-10-01T10:00:00Z", "key": "Am",
    "metronome": {"bpm": 93.5, "signature": {"notesCount": 4, "noteValue": 4}},
    "mastering": {"preset": "cdMaster", "bypass": True}, "volume": 1.0,
    "mixdown": {"id": "mix-1", "file": "fake://mixdown.m4a", "status": "Ready", "duration": 20.0},
    "song": {"id": "song-0001", "name": "Test Song: Night Drive", "slug": "night-drive"},
    "auxChannels": [{"id": "aux1", "preset": "sharedReverb", "returnLevel": 1, "effects": None}],
    "samples": [{"id": "samp-vox", "name": "Vocal take 1", "status": "Ready", "duration": 12.0},
                {"id": "samp-beat", "name": "Beat", "status": "Ready", "duration": 20.0}],
    "tracks": [
        {"id": "trk-vox", "name": "Lead Vocal", "type": "voice", "order": 0, "volume": 0.8, "pan": -0.1, "fxMix": 1,
         "colorName": "Red", "isMuted": False, "isSolo": False,
         "effectsData": {"displayName": "Studio Vocals", "originalPresetId": "preset-123", "link": None},
         "autoPitch": {"version": "0.3", "scale": "scale_minor", "tonic": "tonic_a", "intensity": 0.8, "responseTime": 0.1, "slug": "classic"},
         "effects": [
             {"slug": "expGate", "bypass": False, "params": {"threshold": -45.0, "attack": 0.005, "release": 0.2}, "automation": {}},
             {"slug": "compressor", "bypass": False, "params": {"threshold": -22.0, "ratio": 4, "attack": 0.01, "release": 0.12, "knee": 6, "makeupGain": 1}, "automation": {}},
             {"slug": "threeBandEq", "bypass": False, "params": {"lowGain": -3, "midGain": 1.5, "highGain": 4, "midFreq": 2500, "lowFreq": 220, "highFreq": 7000, "midQ": 1.2}, "automation": {}},
             {"slug": "deEsser", "bypass": False, "params": {"frequency": 7200, "threshold": -20}, "automation": {}},
             {"slug": "ddelayPlus2", "bypass": False, "params": {"delayUnitSelection": "delayTimeBeats", "delayTimeBeats": "quarterNote", "feedback": 0.2, "wet": 0.15, "dry": 1, "lpf": 5000, "hpf": 200, "spread": 0.5}, "automation": {}},
             {"slug": "simpleStudioReverb", "bypass": False, "params": {"mix": 2.5, "size": 3, "color": 6}, "automation": {}},
         ],
         "auxSends": [{"id": "aux1", "sendLevel": 0.3, "automation": []}],
         "automation": {"volume": [{"position": 10.0, "value": 1}, {"position": 12.0, "value": 0.5}]},
         "regions": [
             {"id": "reg-1", "name": "Verse", "sampleId": "samp-vox", "startPosition": 1.2345, "endPosition": 6.0,
              "sampleOffset": 0.5, "loopLength": 0, "playbackRate": 1, "pitchShift": 0, "pitchTimeCorrection": True, "gain": 1, "fadeIn": 0.05, "fadeOut": 0.1},
             {"id": "reg-2", "name": "Hook", "sampleId": "samp-vox", "startPosition": 8.0, "endPosition": 12.0,
              "sampleOffset": 0.0, "loopLength": 0, "playbackRate": 1, "pitchShift": 2, "pitchTimeCorrection": True, "gain": 1.2, "fadeIn": 0, "fadeOut": 0},
         ]},
        {"id": "trk-beat", "name": "Trap Beat", "type": "voice", "order": 1, "volume": 1.0, "pan": 0.0, "fxMix": 1,
         "colorName": "Blue", "isMuted": False, "isSolo": False, "effectsData": None, "effects": [], "auxSends": [],
         "automation": {}, "regions": [
             {"id": "reg-3", "name": "Beat", "sampleId": "samp-beat", "startPosition": 0, "endPosition": 20.0,
              "sampleOffset": 0, "loopLength": 0, "playbackRate": 1, "pitchShift": 0, "gain": 1, "fadeIn": 0, "fadeOut": 0}]},
        {"id": "trk-keys", "name": "Piano", "type": "piano", "order": 2, "volume": 0.9, "pan": 0.2, "effects": [], "auxSends": [],
         "automation": {}, "regions": [], "preset": "grand-piano",
         "patterns": [{"id": "pat-1", "startPosition": 0, "notes": [{"pitch": 60, "startPosition": 0, "duration": 1, "velocity": 0.8},
                                                                    {"pitch": 64, "startPosition": 1, "duration": 1, "velocity": 0.7},
                                                                    {"pitch": 67, "startPosition": 2, "duration": 2, "velocity": 0.9}]}]},
    ],
}


class FakeClient:
    """Serves synthetic files for every URL the downloader asks for."""

    def __init__(self, assets: Path):
        self.assets = assets
        self.calls: list[str] = []

    def get_song(self, sid):
        return {"id": sid, "latestRevisionId": "rev-0001"}

    def get_revision(self, rid):
        return json.loads(json.dumps(REVISION))

    def list_mixdown_options(self, rid):
        return [{"id": "aac-44100-16bit-192kbps"}, {"id": "wav-44100-16bit"}]

    def request_render(self, revision_id, option_id, track_id=None, **kw):
        self.calls.append(f"render:{option_id}:{track_id}")
        return f"fake://render/{track_id or 'mixdown'}.{ 'wav' if option_id.startswith('wav') else 'm4a'}"

    def sample_original_url(self, sid):
        return f"fake://orig/{sid}.wav"

    def sample_formatted_url(self, sid):
        return f"fake://fmt/{sid}.m4a"

    def download(self, url, dest: Path, expected_min_bytes=1, overwrite=False, auth=False):
        self.calls.append(f"download:{url}")
        name = url.split("/")[-1]
        src = self.assets / name
        if not src.exists():
            from bl2fl.bandlab_api import ApiError
            raise ApiError(f"GET {url} -> 404", 404, "")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists() or overwrite:
            shutil.copy2(src, dest)
        return dest


def main() -> int:
    work = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="bl2fl_e2e_"))
    assets = work / "_assets"
    root = work / "BandLab to FL Studio"
    synth(assets / "samp-vox.wav", "tone", 12.0, 220.0)
    synth(assets / "samp-beat.wav", "noise", 20.0)
    synth(assets / "mixdown.m4a", "noise", 20.0)
    synth(assets / "mixdown.wav", "noise", 20.0)
    # "wet" stems: BandLab would render the chain; simulate with an EQ+reverb-ish filter at fader level
    process(assets / "samp-beat.wav", assets / "trk-beat.wav", "volume=1.0")
    # region 1: sample offset 0.5 s, placed at 1.2345 s, 4.7655 s long; region 2: offset 0, at 8.0 s, 4 s long (+2 st)
    subprocess.run([FF, "-v", "error", "-y", "-i", str(assets / "samp-vox.wav"), "-filter_complex",
                    "[0:a]atrim=0.5:5.2655,asetpts=PTS-STARTPTS,adelay=1234.5|1234.5[a];"
                    "[0:a]atrim=0:4,asetpts=PTS-STARTPTS,volume=1.2,adelay=8000|8000[b];"
                    "[a][b]amix=inputs=2:normalize=0,volume=0.8,treble=g=4,aecho=0.8:0.6:120:0.3,apad=whole_dur=20[out]",
                    "-map", "[out]", "-c:a", "pcm_s16le", str(assets / "trk-vox.wav")], check=True)

    client = FakeClient(assets)
    entry = {"id": "song-0001", "name": REVISION["song"]["name"], "latestRevisionId": "rev-0001",
             "folder": "Test Song_ Night Drive [song-000]", "raw": REVISION["song"]}
    dl = SongDownloader(client, root, progress=print)   # type: ignore[arg-type]
    project, man, folder = dl.run(entry)
    first_calls = len(client.calls)
    dl.run(entry)                       # resumable: second run must not download again
    assert len(client.calls) == first_calls, "re-run downloaded again"
    print("manifest items:", {k: v["status"] for k, v in man.data["items"].items()})
    assert man.done("mixdown_m4a") and man.done("mixdown_wav")
    assert man.done("sample:samp-vox") and man.done("sample:samp-beat")
    assert man.done("wet:trk-vox") and man.done("wet:trk-beat")
    assert man.done("midi:trk-keys")

    sc = {}
    for t in project.tracks:
        if not t.regions:
            continue
        wet = Path(man.data["items"][f"wet:{t.id}"]["path"])
        srcs = [(Path(man.data["items"][f"sample:{r.sample_id}"]["path"]), r.start, r.sample_offset, r.duration) for r in t.regions]
        sc[t.name] = analysis.stem_vs_dry(wet, srcs, t.volume)
        print("stem check", t.name, sc[t.name]["verdict"], sc[t.name]["fit_gain_db"], sc[t.name]["lag_ms"])
    assert sc["Lead Vocal"]["effects_present"], "processed stem not detected"
    assert abs(sc["Lead Vocal"]["lag_ms"]) < 2.0, sc["Lead Vocal"]
    assert not sc["Trap Beat"]["effects_present"], "dry stem misdetected"

    b = SongBuilder(folder, project, man, windows_root=r"C:\Users\test\Music\BandLab to FL Studio")
    info = b.build()
    print("variants:", {k: v["pyflp"] for k, v in info["variants"].items()})
    for v in info["variants"].values():
        assert "error" not in v["pyflp"], v["pyflp"]
        assert v["pyflp"]["channels"] == 5 and v["pyflp"]["clips"] == 5   # 2 vocal regions + beat + 2 wet references
    assert (folder / "FL Studio Project" / "build.json").exists()
    assert list((folder / "Vocal Preset Documentation").glob("*.md"))
    assert list((folder / "Preset Screenshots").glob("*.png"))
    assert list((folder / "Dry Vocals").glob("*.wav"))
    # timing: the first vocal clip must land on tick floor(1.2345 s) with a silence pad
    clip = next(c for c in info["clips"] if c["region"] == "reg-1")
    assert abs(clip["tick"] * 60 / (93.5 * 960) + clip["pad_s"] - 1.2345) < 1e-6, clip
    rendered = analysis.decode(folder / "FL Studio Project" / next(c["file"] for c in info["clips"] if c["region"] == "reg-1").__str__())
    print("clip reg-1 length s:", len(rendered) / 44100)

    chains = [(project.song_name, b.chains[t.id]) for t in project.tracks if t.id in b.chains]
    shared_dir = root / "Shared FL Studio Vocal Presets"; shared_dir.mkdir(exist_ok=True)
    shared = write_shared_presets(chains, shared_dir)
    cmp = analysis.compare_mix(next((folder / "BandLab Reference Mix").glob("*.wav")), assets / "mixdown.wav")
    mid = next((folder / "MIDI").glob("*.mid")).read_bytes()
    assert mid[:4] == b"MThd" and b"MTrk" in mid and len(mid) > 40
    assert (root / "Shared FL Studio Vocal Presets" / "presets.json").exists()
    print("compare verdict:", cmp["verdict"], cmp["alignment_offset_ms"], cmp["loudness_diff_lu"])
    rep = build_report(root, {"user": {"username": "test"}, "songs": [entry]},
                       [{"name": entry["name"], "folder": entry["folder"], "status": "migrated", "manifest": man.data,
                         "build": info, "stem_check": {k: v["verdict"] for k, v in sc.items()}, "compare": cmp["verdict"]}], shared)
    print("report:", root / "Migration Report" / "migration_report.md")
    print("OK - offline pipeline passed; work dir:", work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
