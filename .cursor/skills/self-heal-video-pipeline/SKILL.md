---
name: self-heal-video-pipeline
description: Self-heal Shorts render loop — diagnose HF failures, strip paid APIs, apply known fixes, re-run until green. Use when render/verify fails, ring/logo/b-roll looks wrong, or the user asks to keep fixing until it works.
paths: scripts/verify_short.py,scripts/render_fix_loop.py,.github/workflows/render-short.yml,src/shorts_factory/render/**,.cursor/rules/render-fix-loop.mdc
---

# Self-Heal Video Pipeline

Long cycle: **write → run → diagnose → fix → continue**. Do not stop at the first failure.

## Entry command (local / free only)

```bash
unset ELEVENLABS_API_KEY ELEVEN_API_KEY PEXELS_API_KEY PIXABAY_API_KEY \
      GROK_API_KEY MAGNIFIC_API_KEY HEYGEN_API_KEY POND5_API_KEY
export HYPERFRAMES_DOCKER=0 \
       HYPERFRAMES_RENDER_ARGS='--low-memory-mode --workers 1' \
       PYTHONPATH=src

python3 scripts/render_fix_loop.py jobs/<id>.json --render --max-attempts 2
```

Read `build/reports/<id>.loop.json` after each run — `findings[].category` + `findings[].fix`.

## Failure taxonomy (apply before inventing new theories)

| Symptom | Category | Fix |
| --- | --- | --- |
| Flat matte ring | `svg_glow_dropped` / `flat_ring` | CSS neon layers only; no SVG blur |
| Face too far in circle | `video_transform_ignored` | `avatar_close.mp4` + `.avatar-face` wrapper |
| Tall red ellipse on left + cyan tip | `logo_stretch` | Logo `160×160; object-fit:contain` (never `height:auto`) |
| Left red ghost / network lines | `network_svg_ghost` | Do not mount `ring-network` SVG |
| Only first b-roll slot paints | `dead_broll` | One baked `jobs/<id>/broll/broll.mp4` master |
| Missing MP4 / HF crash | `render_failed` | `HYPERFRAMES_DOCKER=0`, `--low-memory-mode --workers 1` |
| Wrong size / duration | `wrong_canvas` / `duration_mismatch` | Check 1080×1920 + `duration_target` |

## Paid API policy

If the user said not to spend tokens — or keys were already burned — **strip paid keys** and reuse:

- `jobs/<id>/avatar.mp4` or `avatar_close.mp4`
- `jobs/<id>/voice_from_avatar.mp3`
- `jobs/<id>/broll/broll.mp4`
- Local `assets/music`, `assets/sfx`, `assets/memes`

## Agent loop checklist

1. `pytest tests/test_ring.py` (fast)
2. Minimal code/asset patch for the **top** finding only
3. `render_fix_loop.py --render`
4. If FAIL → patch from `findings` → commit → push → repeat
5. If OK → copy `build/output/<id>.mp4` → `/opt/cursor/artifacts/` → update PR
6. Stop only when `ok: true` or the **same** finding repeats twice with no progress

## Related files

- `scripts/verify_short.py` — assertions
- `scripts/render_fix_loop.py` — loop + diagnosis report
- `.cursor/rules/render-fix-loop.mdc` — always-on cycle rule
- Skills: `neon-ring-animation`, `talking-head-circle-behavior`, `motion-graphics-style`
