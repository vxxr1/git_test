# BandLab -> FL Studio migration toolkit

Migrates every song in a BandLab account into FL Studio projects while keeping
the original recordings, arrangement, levels, effect chains and the BandLab
sound. Built and tested headlessly; the last mile (opening/rendering in FL
Studio) runs on the Windows machine with `windows/verify_fl.py`.

## What you get per song  (`~/Music/BandLab to FL Studio/<Song> [id]/`)

| folder | contents |
|---|---|
| `FL Studio Project/` | `<Song>.flp` (BandLab's own per-track renders active - sounds like BandLab), `<Song> (editable mix).flp` (dry regions + recreated FL effect chains active), `<Song> (safe).flp` (fallback without constructed plugin states), every referenced WAV beside the FLPs, `build.json` |
| `Original Stems/` | the raw recordings exactly as BandLab stores them (`samples-original` WAV) |
| `Processed Stems/` | per-track WAV renders made by BandLab's engine (effects, fader - measured, not assumed; see `stem_check.json`) |
| `Dry Vocals/` | tick-aligned dry vocal clips (region gain, fades, pitch/rate and the BandLab fader baked in) |
| `MIDI/` | `.mid` files rebuilt from instrument-track pattern notes + raw pattern JSON |
| `BandLab Reference Mix/` | server mixdown (`.m4a` always, `.wav` when BandLab renders it) |
| `Vocal Preset Documentation/` | per-track `.md` + `.json` (every effect, order, exact parameter values, AutoPitch, fader/pan/sends, automation) and an FL mixer-state `.fst` holding the recreated chain |
| `Preset Screenshots/` | generated parameter cards (PNG) - BandLab's UI cannot be screenshotted headlessly, so these show the stored values |
| `BandLab Project Data/` | untouched revision JSON, normalised `project.json`, `stem_check.json` |

Plus `Shared FL Studio Vocal Presets/` (one entry + `.fst` per distinct chain,
listing every song/track that uses it) and `Migration Report/`.

## Setup (Windows, Python 3.10+)

```powershell
git clone <this repo> ; cd bandlab-to-flstudio
pip install -r requirements.txt
winget install Gyan.FFmpeg          # ffmpeg + ffprobe on PATH (any build with rubberband is ideal)
copy .env.example .env               # then paste your refreshToken cookie (see below)
python migrate.py auth-check
python migrate.py inventory
python migrate.py run --one          # pilot: first song end to end
python migrate.py run                # everything (resumable; re-runs skip finished downloads)
python windows\fl_inventory.py --out "%USERPROFILE%\Music\BandLab to FL Studio\Migration Report\fl_inventory.json"
python windows\verify_fl.py "%USERPROFILE%\Music\BandLab to FL Studio"
python migrate.py report
```

### Logging in without a password

1. Log in at https://www.bandlab.com in Chrome/Edge.
2. Press F12 -> Application -> Cookies -> `https://www.bandlab.com` -> copy the value of `refreshToken`.
3. Put it in `.env` as `BANDLAB_REFRESH_TOKEN=...`. The tool exchanges it for a 24 h
   access token at BandLab's own identity server and renews it automatically
   (BandLab rotates refresh tokens; the current one is kept in `.state/session.json`, mode 600).

Alternative: copy the `Authorization: Bearer ...` header of any `api/v1.3`
request from the Network tab into `BANDLAB_SESSION_KEY` (expires in ~24 h).

## How it works

1. **Inventory** `GET /users/{me}/songs` (cursor pagination) -> `inventory.json`.
2. **Download** per song, resumable through `manifest.json`:
   latest revision JSON; mixdown `.m4a` from `revision.mixdown.file`; WAV mixdown
   and per-track stems via `POST /revisions/{id}/(mixdown|tracks/{tid})/download-options/wav-44100-16bit`
   + polling until a URL appears (exactly what the web app does); raw samples from
   `static.bandlab.com/samples-original/{id}/{id}.wav` (lossy `samples-formatted` m4a fallback);
   MIDI from pattern notes.
3. **Verify wet vs dry** - `analysis.stem_vs_dry()` lays the dry regions on a timeline and
   measures correlation, best-fit gain and spectral difference against BandLab's render.
   The result (effects present? fader included?) drives how the wet reference inserts are set.
4. **Presets** - every chain is read from `tracks[].effects[]` (slug, bypass, params,
   automation) and `tracks[].autoPitch`, documented, hashed to find chains shared across
   songs, and mapped to FL stock plugins (`bl2fl/presets.py`: BandLab -> FL plugin -> settings ->
   unavoidable differences). Nothing is claimed identical: every approximation is flagged.
5. **FLP** - `bl2fl/flp_writer.py` writes FL Studio 24-format projects directly: audio clips on
   named/coloured playlist tracks at tick-exact positions (960 PPQ + per-clip silence pad for
   sample accuracy), one mixer insert per track with name/colour/pan/sends and the effect slots,
   a Reverb Bus for BandLab's shared aux, markers, tempo and time signature. Output is validated
   by round-tripping through PyFLP (an independent parser).
6. **Report** - `Migration Report/migration_report.md|json`.

Signal-order note: in BandLab the volume lane sits before the effect chain and aux sends are
pre-fader. The dry clips therefore carry the BandLab fader so FL's fader can stay at 0 dB and
the recreated chain sees the same level.

## What needs the Windows machine

* FL Studio version/plugin inventory (`windows/fl_inventory.py`).
* Opening the FLPs, rendering them (`windows/verify_fl.py` uses FL's `/R` command-line render)
  and the A/B comparison against the BandLab mixdown (`bl2fl/analysis.compare_mix`).
* Constructed plugin states (Fruity Parametric EQ 2, Fruity Compressor, Fruity Delay 3) are built from
  reverse-engineered layouts; if FL rejects one, the `(safe).flp` variant loads the same plugins
  with default state and the settings sheet gives the values to dial in.

## Limits (documented, not hidden)

* BandLab's exact pedal models (LA-2A/1176/amp sims/convolution IRs) have no stock FL twin;
  the wet stems preserve the sound, the mapping gives the nearest stock plugin.
* Fruity Edition lacks Pitcher/Parametric EQ 2/Fruity Limiter; the settings sheets still apply.
* Volume/pan automation lanes are exported as data (`...json`) and listed as manual follow-ups.
* Instrument tracks (MIDI) come as `.mid` + BandLab's rendered audio; the BandLab instrument
  itself cannot be loaded in FL.
* FL's fader law above 0 dB is undocumented; levels are baked into audio instead of relying on it.

Run `python tests/offline_e2e.py` for a full no-network self-test (synthetic song).
