# state.md — pipeline hybrid modes + life-bio-er100

## Status
IN_PROGRESS — QA fixes after first render review (awaiting studio avatar regen + re-render)

## Deliverables
- Pipeline: Pulse Ring removed; `mode: split|full_host|full_footage`
- SPLIT = fixed **40% host / 60% footage** (was 45%)
- No logo watermark, no orbital semi-ovals
- Avatar video audio stripped at compose (fixes double VO: HeyGen AAC + mix)
- SFX: ≤5 short accents, accent_score prefers oneshots; long cinematic demoted
- Footage search: strip "deep void" bias, luma gate on local b-roll, lit rescue queries
- Job: `jobs/life-bio-er100.json` + `PRODUCTION_BRIEF.md`
- Avatar: `jobs/life-bio-er100/avatar.mp4` (old plate until user regenerates studio look)
- B-roll: `jobs/life-bio-er100/broll/*.mp4` (18 clips)
- Last successful artifact (OLD look/render): https://github.com/darkuseone/branded-shorts-factory/actions/runs/31234740841/artifacts/9015125316

## Next
1. User regenerates HeyGen avatar with studio look `54172d7a…`
2. Commit new `avatar.mp4` + re-run Render Short
3. Verify single audio track + 40/60 split + visible footage

## Notes
- Host frame background is transparent so studio plate shows (not Deep Void fill)
- PR: https://github.com/darkuseone/branded-shorts-factory/pull/9
