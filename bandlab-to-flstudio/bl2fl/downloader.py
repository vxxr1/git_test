"""
Phase 1 + 2: inventory every song and download everything BandLab will give us.

Per-song output layout (inside the migration root):

    <Song Name> [<id8>]/
        manifest.json                     resumable state for this song
        BandLab Reference Mix/            server mixdown (.m4a always, .wav when offered)
        Original Stems/                   raw recordings exactly as BandLab stores them (WAV)
        Processed Stems/                  per-track renders made by BandLab's own engine
        MIDI/                             .mid files rebuilt from instrument-track patterns
        BandLab Project Data/             raw revision JSON, normalised project.json

Everything is idempotent: a file that already exists with a sane size is never
downloaded again, and the manifest records what succeeded, failed or was
unavailable so a re-run only does the missing parts.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import midi_export
from .bandlab_api import ApiError, BandLabClient, BandLabError, STEM_OPTION_M4A, STEM_OPTION_WAV
from .models import Project, Track, parse_revision
from .util import read_json, safe_name, short_id, write_json

log = logging.getLogger("bl2fl.download")

DIR_REF = "BandLab Reference Mix"
DIR_ORIG = "Original Stems"
DIR_WET = "Processed Stems"
DIR_MIDI = "MIDI"
DIR_DATA = "BandLab Project Data"


def song_folder_name(song: dict[str, Any]) -> str:
    return f"{safe_name(song.get('name') or 'Untitled')} [{short_id(song.get('id', ''))}]"


@dataclass
class Manifest:
    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        return cls(path=path, data=read_json(path, {}) or {})

    def save(self) -> None:
        self.data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        write_json(self.path, self.data)

    def item(self, key: str) -> dict[str, Any]:
        return self.data.setdefault("items", {}).setdefault(key, {})

    def mark(self, key: str, status: str, **info: Any) -> None:
        it = self.item(key)
        it.update(info)
        it["status"] = status
        it["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.save()

    def done(self, key: str, path: Optional[Path] = None) -> bool:
        it = self.data.get("items", {}).get(key) or {}
        if it.get("status") != "done":
            return False
        if path is not None and not path.exists():
            return False
        return True


# --------------------------------------------------------------- inventory
def build_inventory(client: BandLabClient, root: Path) -> dict[str, Any]:
    """List every song the account can reach. Written to <root>/inventory.json."""
    me = client.me()
    user_id = me.get("id")
    if not user_id:
        raise BandLabError("Could not read the current user id from /me")
    songs = client.list_my_songs(user_id)
    inv = {
        "user": {"id": user_id, "username": me.get("username"), "name": me.get("name")},
        "fetched": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "songs": [],
    }
    for s in songs:
        inv["songs"].append(
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "slug": s.get("slug"),
                "isPublic": s.get("isPublic"),
                "canEdit": s.get("canEdit"),
                "createdOn": s.get("createdOn"),
                "modifiedOn": s.get("modifiedOn") or s.get("updatedOn"),
                "latestRevisionId": (
                    s.get("latestRevisionId")
                    or (s.get("latestRevision") or {}).get("id")
                    or (s.get("revision") or {}).get("id")
                ),
                "folder": song_folder_name(s),
                "raw": s,
            }
        )
    write_json(root / "inventory.json", inv)
    log.info("Inventory: %d songs for @%s", len(songs), me.get("username"))
    return inv


# --------------------------------------------------------------- revision
def fetch_latest_revision(client: BandLabClient, song_entry: dict[str, Any]) -> dict[str, Any]:
    rid = song_entry.get("latestRevisionId")
    if not rid:
        song = client.get_song(song_entry["id"])
        rid = (
            song.get("latestRevisionId")
            or (song.get("latestRevision") or {}).get("id")
            or (song.get("revision") or {}).get("id")
        )
        if not rid:
            revs = client.list_revisions(song_entry["id"])
            if not revs:
                raise BandLabError(f"Song {song_entry.get('name')} has no revisions")
            revs.sort(key=lambda r: r.get("createdOn") or "", reverse=True)
            rid = revs[0]["id"]
    rev = client.get_revision(rid)
    if not rev.get("tracks"):
        log.warning("Revision %s has no tracks in the API response", rid)
    return rev


# --------------------------------------------------------------- download
class SongDownloader:
    def __init__(
        self,
        client: BandLabClient,
        root: Path,
        want_wet_stems: bool = True,
        want_wav_mixdown: bool = True,
        progress: Optional[Callable[[str], None]] = None,
    ):
        self.client = client
        self.root = root
        self.want_wet_stems = want_wet_stems
        self.want_wav_mixdown = want_wav_mixdown
        self.progress = progress or (lambda m: log.info(m))

    # .................................................................
    def run(self, song_entry: dict[str, Any]) -> tuple[Project, Manifest, Path]:
        folder = self.root / song_entry.get("folder", song_folder_name(song_entry))
        folder.mkdir(parents=True, exist_ok=True)
        man = Manifest.load(folder / "manifest.json")
        man.data.setdefault("song", {"id": song_entry.get("id"), "name": song_entry.get("name")})

        # 1. Revision JSON ------------------------------------------------
        data_dir = folder / DIR_DATA
        rev_path = data_dir / "bandlab_revision.json"
        rev = read_json(rev_path)
        wanted = song_entry.get("latestRevisionId")
        if not rev or (wanted and rev.get("id") != wanted):
            self.progress(f"[{song_entry.get('name')}] fetching latest revision")
            rev = fetch_latest_revision(self.client, song_entry)
            write_json(rev_path, rev)
            man.data["items"] = {}  # a new revision invalidates earlier downloads
            man.data["revisionId"] = rev.get("id")
            man.save()
        project = parse_revision(rev, song_entry.get("raw") or {})
        write_json(data_dir / "project.json", project_summary(project))
        man.data["revisionId"] = project.revision_id
        man.data["bpm"] = project.bpm
        man.save()

        # 2. Reference mixdown --------------------------------------------
        self._download_mixdown(project, folder, man)

        # 3. Original (dry) samples ----------------------------------------
        self._download_samples(project, folder, man)

        # 4. Wet per-track stems -------------------------------------------
        if self.want_wet_stems:
            self._download_wet_stems(project, folder, man)

        # 5. MIDI -----------------------------------------------------------
        self._export_midi(project, folder, man)
        return project, man, folder

    # .................................................................
    def _download_mixdown(self, p: Project, folder: Path, man: Manifest) -> None:
        ref = folder / DIR_REF
        base = safe_name(p.song_name)
        m4a = ref / f"{base} (BandLab mixdown).m4a"
        key = "mixdown_m4a"
        if not man.done(key, m4a):
            url = (p.mixdown or {}).get("file")
            status = (p.mixdown or {}).get("status")
            if url and status in (None, "Ready"):
                try:
                    self.progress(f"[{p.song_name}] mixdown (m4a)")
                    self.client.download(url, m4a, expected_min_bytes=10_000)
                    man.mark(key, "done", path=str(m4a), url=url, duration=p.mixdown.get("duration"))
                except (BandLabError, ApiError) as e:
                    man.mark(key, "failed", error=str(e), url=url)
            else:
                man.mark(key, "unavailable", reason=f"mixdown status={status}")
        if self.want_wav_mixdown:
            wav = ref / f"{base} (BandLab mixdown).wav"
            key = "mixdown_wav"
            if not man.done(key, wav):
                option = None
                try:
                    opts = self.client.list_mixdown_options(p.revision_id)
                    for o in opts:
                        oid = str(o.get("id") or "")
                        if oid.startswith("wav"):
                            option = oid
                            break
                except BandLabError:
                    opts = []
                option = option or STEM_OPTION_WAV
                try:
                    self.progress(f"[{p.song_name}] mixdown (WAV via {option})")
                    url = self.client.request_render(p.revision_id, option)
                    self.client.download(url, wav, expected_min_bytes=10_000)
                    man.mark(key, "done", path=str(wav), url=url, option=option, options_seen=opts)
                except (BandLabError, ApiError) as e:
                    man.mark(key, "failed", error=str(e), option=option, options_seen=opts)
                    log.warning("WAV mixdown not available for %s: %s", p.song_name, e)

    # .................................................................
    def _download_samples(self, p: Project, folder: Path, man: Manifest) -> None:
        orig = folder / DIR_ORIG
        used: dict[str, list[tuple[Track, Any]]] = {}
        for t in p.tracks:
            for r in t.regions:
                if r.sample_id:
                    used.setdefault(r.sample_id, []).append((t, r))
        man.data["sampleCount"] = len(used)
        for sid, refs in used.items():
            track, region = refs[0]
            nice = safe_name(region.name or track.name or sid, 60)
            key = f"sample:{sid}"
            wav = orig / f"{nice} [{short_id(sid)}].wav"
            m4a = orig / f"{nice} [{short_id(sid)}].m4a"
            if man.done(key, wav) or man.done(key, m4a):
                continue
            self.progress(f"[{p.song_name}] original sample {nice}")
            try:
                self.client.download(self.client.sample_original_url(sid), wav, expected_min_bytes=100)
                man.mark(key, "done", path=str(wav), kind="original-wav", sampleId=sid,
                         tracks=sorted({t.name for t, _ in refs}))
                continue
            except ApiError as e:
                if e.status not in (403, 404):
                    man.mark(key, "failed", error=str(e), sampleId=sid)
                    continue
            except BandLabError as e:
                man.mark(key, "failed", error=str(e), sampleId=sid)
                continue
            # Fall back to the lossy formatted copy, or the url BandLab reported.
            fallback = (p.samples.get(sid).file if p.samples.get(sid) else None) or self.client.sample_formatted_url(sid)
            try:
                self.client.download(fallback, m4a, expected_min_bytes=100)
                man.mark(key, "done", path=str(m4a), kind="formatted-m4a", sampleId=sid,
                         tracks=sorted({t.name for t, _ in refs}),
                         note="original WAV not served by CDN; lossy copy used")
            except (BandLabError, ApiError) as e:
                man.mark(key, "failed", error=str(e), sampleId=sid)

    # .................................................................
    def _download_wet_stems(self, p: Project, folder: Path, man: Manifest) -> None:
        wet = folder / DIR_WET
        for idx, t in enumerate(p.tracks, start=1):
            if not t.regions:
                continue
            key = f"wet:{t.id}"
            name = f"{idx:02d}_{safe_name(t.name, 50)}"
            wav = wet / f"{name}.wav"
            if man.done(key, wav):
                continue
            self.progress(f"[{p.song_name}] rendering stem {t.name}")
            try:
                url = self.client.request_render(p.revision_id, STEM_OPTION_WAV, track_id=t.id)
                self.client.download(url, wav, expected_min_bytes=1_000)
                man.mark(key, "done", path=str(wav), trackId=t.id, option=STEM_OPTION_WAV, url=url)
            except (BandLabError, ApiError) as e:
                log.warning("WAV stem failed for %s/%s (%s); trying M4A", p.song_name, t.name, e)
                m4a = wet / f"{name}.m4a"
                try:
                    url = self.client.request_render(p.revision_id, STEM_OPTION_M4A, track_id=t.id)
                    self.client.download(url, m4a, expected_min_bytes=1_000)
                    man.mark(key, "done", path=str(m4a), trackId=t.id, option=STEM_OPTION_M4A,
                             note=f"WAV render failed: {e}")
                except (BandLabError, ApiError) as e2:
                    man.mark(key, "failed", error=f"wav: {e}; m4a: {e2}", trackId=t.id)

    # .................................................................
    def _export_midi(self, p: Project, folder: Path, man: Manifest) -> None:
        mdir = folder / DIR_MIDI
        for idx, t in enumerate(p.tracks, start=1):
            if not t.patterns:
                continue
            key = f"midi:{t.id}"
            out = mdir / f"{idx:02d}_{safe_name(t.name, 50)}.mid"
            if man.done(key, out):
                continue
            try:
                info = midi_export.track_to_midi(p, t, out)
                man.mark(key, "done" if info.get("notes") else "empty", path=str(out), **info)
            except Exception as e:  # noqa: BLE001 - we want the manifest to carry the reason
                man.mark(key, "failed", error=str(e), trackId=t.id)
                write_json(mdir / f"{idx:02d}_{safe_name(t.name, 50)}.patterns.json", t.patterns)


# --------------------------------------------------------------- summary
def project_summary(p: Project) -> dict[str, Any]:
    return {
        "song": {"id": p.song_id, "name": p.song_name, "slug": p.song_slug},
        "revision": {"id": p.revision_id, "createdOn": p.revision_created},
        "bpm": p.bpm,
        "timeSignature": f"{p.time_sig[0]}/{p.time_sig[1]}",
        "key": p.key,
        "masterVolume": p.master_volume,
        "mastering": p.mastering,
        "mixdown": p.mixdown,
        "durationSeconds": p.duration,
        "auxChannels": p.aux_channels,
        "tracks": [
            {
                "index": i + 1,
                "id": t.id,
                "name": t.name,
                "type": t.type,
                "role": t.role,
                "volume": t.volume,
                "pan": t.pan,
                "muted": t.is_muted,
                "solo": t.is_solo,
                "color": t.color_name,
                "fxMix": t.fx_mix,
                "preset": {"name": t.preset_name, "id": t.preset_id, "link": t.preset_link},
                "autoPitch": t.auto_pitch,
                "effects": [
                    {"slug": e.slug, "bypass": e.bypass, "params": e.params, "automated": sorted(e.automation.keys())}
                    for e in t.effects
                ],
                "auxSends": [{"id": s.id, "level": s.level} for s in t.aux_sends],
                "automation": {k: len(v) for k, v in (t.automation or {}).items() if isinstance(v, list)},
                "regions": [
                    {
                        "id": r.id, "name": r.name, "sampleId": r.sample_id,
                        "start": r.start, "end": r.end, "sampleOffset": r.sample_offset,
                        "loopLength": r.loop_length, "playbackRate": r.playback_rate,
                        "pitchShift": r.pitch_shift, "pitchTimeCorrection": r.pitch_time_correction,
                        "gain": r.gain, "fadeIn": r.fade_in, "fadeOut": r.fade_out,
                    }
                    for r in t.regions
                ],
                "midiPatterns": len(t.patterns or []),
                "instrumentPreset": t.instrument_preset,
                "soundbank": t.soundbank,
            }
            for i, t in enumerate(p.tracks)
        ],
    }
