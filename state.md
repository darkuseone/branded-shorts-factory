# state.md — pipeline hybrid modes + life-bio-er100

## Status
DONE

## Deliverables
- Pipeline: Pulse Ring removed; `mode: split|full_host|full_footage`; Deep Void/Crimson; caption glow+CA; thin orbitals
- Job: `jobs/life-bio-er100.json` + `PRODUCTION_BRIEF.md` (A–F)
- Avatar: `jobs/life-bio-er100/avatar.mp4` (HeyGen NIKITA2, 1080×1920)
- B-roll: `jobs/life-bio-er100/broll/*.mp4` (18 Freepik free clips)
- Output: GHA Render Short success → `build/output/life-bio-er100.mp4` (48s, 9:16)
- Artifact: `/opt/cursor/artifacts/life-bio-er100.mp4`
- Verify: `scripts/verify_short.py` OK on GHA + local

## Notes
- Soft warning only: avatar.segments not contiguous across FULL_FOOTAGE windows (host opacity 0 there).
- Tone: Phase 1 safety honesty; metaphor «взлом кода», no «eternal life proven».
- PR: https://github.com/darkuseone/branded-shorts-factory/pull/9
