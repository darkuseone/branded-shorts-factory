# state.md — pipeline hybrid modes + life-bio-er100

## Status
IN_PROGRESS

## Current step
Pushing local Freepik b-roll + data-chip track fix; waiting for GHA Render.

## Done
- [x] Hybrid host modes; Pulse Ring removed
- [x] Brand Deep Void / Crimson + caption FX
- [x] CI green (ruff + pytest)
- [x] `jobs/life-bio-er100.json` + PRODUCTION_BRIEF
- [x] HeyGen avatar + voice_from_avatar
- [x] Local broll staged (18 clips, Magnific Freepik free)
- [x] Data chips → TRACK_DATA_CHIP (no HyperFrames overlap)
- [x] Meme history gap for life-bio meme

## Next
- GHA Render Short → verify_short → DONE

## Notes
- First Render failed: track-2 chip/footage overlap + 12 unfilled (Magnific API 404 + QA)
- Fix: local_broll + chip track + footage-only job
