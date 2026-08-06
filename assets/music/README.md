# Music library

Drop tracks here (`.mp3`, `.wav`, `.m4a`, `.aac`, `.ogg`, `.flac`). The scenario
JSON picks one by filename stem:

```json
"music": { "track": "Orbital Drift", "volume": 0.16, "ducking": true }
```

A prefix is enough — `"Orbital"` finds `Orbital Drift.mp3`. If the named track
is missing, the first file in this folder is used and the run report says so.

Current bank (REDSHIFT mood set):

| File | Mood |
| --- | --- |
| `Biotic Pulse.mp3` | organic / science |
| `Digital Pulse.mp3` | IT / UI pulse |
| `Orbital Drift.mp3` | space / calm |
| `Overclocked.mp3` | tension / tech |
| `System Logic.mp3` | AI / data |

Two more files were moved here from `assets/sfx/` after the analyser classified
them as music beds (`alexzavesa-calm-elegant-logo-…`, `idoberg-relaxing-guitar-loop-…`).
They are fine as beds; they must not be used as montage accents.

## Optional `index.json`

```json
{
  "items": [
    { "file": "Orbital Drift.mp3", "title": "Orbital Drift", "tags": ["space", "calm"], "duration": 128 }
  ]
}
```

Ducking is applied at mix time with a real sidechain compressor, so pick tracks
with headroom rather than pre-ducked stems.

Треки **нужно коммитить**: GitHub Actions собирает ролик из свежего клона.
