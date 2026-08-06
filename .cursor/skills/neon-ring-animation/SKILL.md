---
name: neon-ring-animation
description: REDSHIFT Pulse Ring neon animation — idle/emphasis/transition/exit states, CSS-only bloom (no SVG filters), HyperFrames-safe glow. Use when editing ring CSS, brandbook ring, composition neon layers, or debugging a flat matte ring.
paths: brand/brandbook.json,src/shorts_factory/render/ring.py,src/shorts_factory/render/composition.py,scripts/verify_short.py
---

# Neon Ring Animation (Pulse Ring)

The Pulse Ring is the REDSHIFT signature — not decoration. It frames the talking head and **reacts to the script**.

## Brand tokens (`brand/brandbook.json` → `ring`)

| Token | Default | Role |
| --- | --- | --- |
| `stroke` | `#FF2A3C` | Signal Red tube |
| `glow` | `#FF4D63` | Soft corona |
| `diameter_ratio` | `0.30` | ~576px on 1080×1920 |
| `default_anchor` | `bottom_right` | Presenter pocket |
| `cta_anchor` | `bottom_center` | CTA beat |

Keep ≥ **140px** right margin so stage `overflow:hidden` does not shear the bloom.

## States (must stay in sync with `ring.py` + brandbook)

| State | Motion | Timing |
| --- | --- | --- |
| **IDLE** | Breath ±5% scale | ~2.4s ease-in-out alternate |
| **EMPHASIS** | Double pulse +18% | 0.25s × 2 on high-emphasis beats |
| **TRANSITION** | Expand to 140% then settle | 0.35s on topic-change cuts |
| **EXIT / RETURN** | Scale→0 / reverse | 0.25s at avatar window edges |

Drive states from absolute-time CSS keyframes spanning the **full composition duration** (seek-safe for HyperFrames).

## Neon that survives HyperFrames

**Never** rely on SVG `feGaussianBlur` / `filter:url(#ringGlow)` — Puppeteer capture drops them → flat matte stroke.

Required DOM layers (composition):

```html
<div class="pulse-ring-halo"></div>   <!-- radial soft bloom -->
<div class="pulse-ring-neon"></div>   <!-- border + multi-stop box-shadow -->
<svg class="pulse-ring-svg">…</svg>   <!-- crisp stroke only, NO filters -->
```

Neon recipe:

- White-hot core: `0 0 1px 1px #fff` + inset light rim
- Mid corona: `#FF8A96` / `#FF2A3C` box-shadow stops
- Outer bloom: `.pulse-ring-halo` radial-gradient (`closest-side`)
- Do **not** stack large CSS `filter: drop-shadow` on the SVG (paint leaks in HF)

## Do not mount

- `ring-network` SVG — even with `display:none` HF still painted a tall left ghost arc. Skip the node entirely.

## Verify

```bash
python3 scripts/verify_short.py jobs/<id>.json
# checks: neon bloom red-hits, no feGaussianBlur, no left ghost
```

## When changing animation

1. Edit brandbook `ring.states.*` first
2. Mirror in `RingConfig` / `ring_css()`
3. Keep neon layers CSS-only
4. Run `pytest tests/test_ring.py` then `render_fix_loop.py`
