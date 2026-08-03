# Music library

Drop your five tracks here (`.mp3`, `.wav`, `.m4a`, `.aac`, `.ogg`, `.flac`).

The scenario JSON selects one by filename stem:

```json
"music": { "track": "tension_01", "volume": 0.16, "ducking": true }
```

`tension_01` matches `tension_01.mp3`. A prefix is enough — `"tension"` finds it
too. If the named track is missing, the first file in this folder is used and the
run report says so.

## Optional `index.json`

Add tags and titles if you want them in reports:

```json
{
  "items": [
    { "file": "tension_01.mp3", "title": "Tension 01", "tags": ["dark", "science"], "duration": 128 }
  ]
}
```

Ducking is applied at mix time with a real sidechain compressor, so pick tracks
with headroom rather than pre-ducked stems.

Tracks are **not** committed to git — see `.gitignore`.
