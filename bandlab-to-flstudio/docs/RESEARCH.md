# Research notes: what was verified, how, and what is still assumed

## Environment facts (this build)

* Built in a Linux cloud container: Python 3.13, ffmpeg 7 (with `rubberband`, `ebur128`,
  `loudnorm`), numpy/scipy/soundfile/matplotlib/PyFLP 2.2.1, Chromium + Playwright present.
* No link to the Windows machine exists from the container, so FL Studio itself could not be
  inspected or driven. Everything FL-side is scripted for Windows (`windows/`).
* BandLab's web site and CDN are reachable; the private API needs a bearer token.

## BandLab (verified live, 2026-10-11, web app 10.2.86)

| fact | evidence |
|---|---|
| API base `https://www.bandlab.com/api/v1.3` | public `/posts?type=Revision` answered 200; `/revisions/{id}` answered 401 without token |
| song list `GET /users/{userId}/songs` | path string in `web-app/scripts/C49ns7cO.js` |
| per-track render `POST /revisions/{rev}/tracks/{track}/download-options/{wav-44100-16bit|aac-44100-16bit-192kbps}` then `GET` same URL until `{url}` | DownloadStemsModal code in the bundle; the web app uses the same render for playback of locked collaborator tracks |
| mixdown render `POST /revisions/{rev}/mixdown/download-options/{optionId}` (+poll) | bundle |
| server mixdown `revision.mixdown.file` -> `static.bandlab.com/revisions-formatted/{id}/{id}.m4a` | public feed JSON + HTTP 206 on the file |
| original sample `static.bandlab.com/samples-original/{id}/{id}.wav` (audio/wav) | HTTP 206 with `audio/wav` content type on a sample id taken from the bundle |
| formatted sample `static.bandlab.com/samples-formatted/{id}/{id}.m4a` | bundle + HTTP 206 |
| refresh via `accounts.bandlab.com/oauth/connect/token`, client `bandlab_web` | bundle + bandlab-mcp project notes |
| revision JSON shape (tracks, regions, effects, effectsData, auxSends, automation, autoPitch) | bandlab-mcp types verified against live responses; `autoPitch` is a track field (bundle: `getTrack().autoPitch`) |
| units: region positions **seconds**, automation positions **beats**, volume 0..2 linear, pan -1..1 | bandlab-mcp measurements (render lengths vs values) |
| volume lane sits **before** the effect chain; aux sends are pre-fader | bandlab-mcp render measurements |
| effect slugs + parameter names/ranges (54 slugs) | bandlab-mcp harvested catalogue, copied into `presets.py` |
| effect display names | **assumed** - BandLab builds them inside its WASM engine at runtime (`Katnip.getMetadataForEffect`), not readable statically; names in `presets.py` marked where uncertain |

Not verified (needs the user's token): whether the per-track WAV render is post-fader/post-effects.
The pipeline measures it (`stem_check.json`) instead of assuming.

## FL Studio project format (verified by parsing FL-saved files)

* Container: `FLhd` + 6-byte header (format 0, channel count, PPQ) + `FLdt` + events.
  Event ids <64: 1 byte, <128: 2 bytes, <192: 4 bytes, else varint length + data.
* Audio clip channel = channel block with Type (id 21) = 4, empty plugin name, `SamplePath`
  (id 196, UTF-16LE). Verified on PyFLP's FL 20.8.4 asset ("Audio Clip" channel) and DawVert's writer.
* Playlist item (id 233, 60-byte records in FL 21+): position, 20480, item index (channel id for
  audio clips), length, reversed track index (500 - n), group, flags, start/end offsets (-1 = whole
  sample) + 28-byte tail.
* Mixer: 127 insert blocks (each preceded by a 1-byte id-42 separator), each: Flags(236),
  Name(204), 10 slots (plugin events + Index 98), Routing(235, 127 bools), 165/166/Input/Output.
  Then one parameter table (id 225) of 12-byte records; `channel_data` carries `0x2000 | insert<<6 | slot`
  (`0x4000` on the leading global record). Verified byte-for-byte against an FL 24.2 project and
  FL 20.8 mixer-state files.
* Plugin state layouts (all little-endian int32): Fruity Reeverb 2 (template from an FL 24 file),
  Fruity Limiter (template), Fruity Balance (pan, vol=256 at 0 dB), Soft Clipper, Stereo Enhancer,
  Blood Overdrive, Fast Dist (templates from FL-saved .fst files); Fruity Parametric EQ 2 /
  Fruity Compressor / Fruity Delay 3 constructed from published field orders (DawVert datadef) -
  version numbers unknown, so these are **constructed** and the `(safe).flp` omits them.
* FL fader law above 0 dB is undocumented; 12800 = 0 dB is certain. Levels are baked into audio.

## Decisions

* Two active variants per song: BandLab-render-active (sound guaranteed) and editable-mix-active
  (dry + recreated chains) so neither set is processed twice; a third "safe" variant for plugin
  states.
* 960 PPQ + per-clip silence padding gives sample-accurate placement regardless of tempo.
* Writes to BandLab are limited to render requests; no revision is created, edited or deleted.
* Requests are serialised with a 1.2 s minimum spacing (human pace).

## Third-party references consulted

* BerkantACUN/bandlab-mcp (MIT) - API types, effect catalogue, unit measurements.
* demberto/PyFLP (GPL-3) - FLP event ids/structs, used as an independent validator at runtime.
* sbf-developer/experiment-with-py-flp - an FL 24-saved FLP used to extract structural byte templates.
* SatyrDiamond/DawVert (GPL-3) - field order of stock plugin states (read, not vendored).
* Image-Line forum/release notes - `FL64.exe /R<name> project.flp /Ewav` command-line render.
