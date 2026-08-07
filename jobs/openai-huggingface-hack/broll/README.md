# Pre-staged b-roll (openai-huggingface-hack)

## Masters
- `aleko-master.mp4` — single HyperFrames-safe 9:16 bed (host / footage / 50–50).
  Rebuild: `PYTHONPATH=src python3 scripts/bake_aleko_master.py jobs/openai-huggingface-hack`

## Packs
- `stock/` — Magnific Freepik free stills (gitignored; re-fetch via Magnific `stock_search`/`stock_download`).
- `news/` — generated news-card stills with Russian headlines (Ken Burns in the bake).
- `top-*.mp4` — short cyber plates used as video cuts (~4s).

## Dense edit rules
See `src/shorts_factory/search/footage_pack.py`:
- photo plates 1.2–2.8s with kenburns/parallax/zoom/pan
- video plates ~2.6–4.2s
- theme-matched selection from staged stock/news/top clips
