# Windows side

1. `python windows\fl_inventory.py --out "<root>\Migration Report\fl_inventory.json"`
   finds FL Studio under Program Files\Image-Line, reads its version, lists stock and VST
   plugins and reports which of the plugins the migration relies on are present.
2. `python windows\verify_fl.py "<root>"` renders every `*.flp` with
   `FL64.exe /R<out> <project.flp> /Ewav`, then compares each render with
   `BandLab Reference Mix` (alignment offset, loudness, residual after level match, band balance,
   stereo width, clipping) and writes `FL Studio Project\verification.json` per song and
   `Migration Report\fl_verification.json`. Run `python migrate.py report` afterwards to fold the
   results into the migration report.
3. If a `<Song>.flp` fails to render but `<Song> (safe).flp` works, the constructed plugin state
   of one of Fruity Parametric EQ 2 / Fruity Compressor / Fruity Delay 3 was rejected: open the
   safe project and dial in the values from `Vocal Preset Documentation`.
