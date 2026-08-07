---
name: motion-graphics-style
description: REDSHIFT After Effects-like motion graphics feel for Shorts — timing, easing, layer hierarchy, data chips, memes, captions, b-roll energy. Use when designing/composing visuals, transitions, overlays, or when the cut feels flat/PowerPoint instead of AE.
paths: brand/brandbook.json,src/shorts_factory/render/**,jobs/**/*.json,assets/**
---

# Motion Graphics Style (After Effects–like)

Target feel: **sci-pop documentary bumpers** — sharp, rhythmic, neon-accented — not template slideshows. Think broadcast lower-thirds + HUD, not Canva.

## Brand atmosphere

| Role | Value |
| --- | --- |
| Primary | `#FF2A3C` Signal Red |
| Accent | `#37E4FF` Cyan |
| Background | `#0A0C10` Graphite void |
| Panel | `#14171C` |
| Type | Inter / Space Grotesk / JetBrains Mono |

Backgrounds: dark space/tech plates, subtle particles, volumetric accents — not flat purple gradients or cream editorial layouts.

## AE-like principles (HyperFrames CSS constraints)

HyperFrames is DOM + CSS keyframes (full-duration absolute %). Emulate AE craft within that:

1. **One job per beat** — hook / context / payoff / CTA; don’t stack competing overlays
2. **Ease with intent** — snappy emphasis (≤0.25s), smoother topic wipes (~0.35s), idle breath only on the ring
3. **Anticipation → hit → settle** — especially for data chips and emphasis pulses
4. **Layer hierarchy** (z / track order):
   - 0 backdrop (black lower third) → 2 Action Stage (top, every 2–4s) → 3 avatar/ring (bottom-center, continuous on VO) → 4 captions → 5 brand → 6 CTA/subscribe → memes on their track
5. **Motion from the ring** — particles/lines should feel emitted *from* the circle, not floating stickers on top
6. **No fullscreen under the host** for news/AI — Action Stage only above the oval
7. **No card spam** — chips only when they carry a number/label the VO hits; no hero cards

## Cut rate (news / AI)

New Action Stage plate every **2–4 seconds**. A single 40s stock still behind the oval is a fail.

## Timing vocabulary

| Beat | Feel | Typical |
| --- | --- | --- |
| Hook punch | Hard cut + ring emphasis | first 3–4s |
| Data chip | Slide/fade in with VO number | ≤1s hold |
| Meme (irony) | Single short sting | ≤1.4s, max 1/video default |
| Topic change | Ring TRANSITION expand | ~0.35s |
| CTA | Ring → bottom_center, clean frame | last window |
| Outro | Brand lockup | final seconds |

Prefer **linear absolute** animations for seek-stability; fake AE ease with denser keyframe stops near hits.

## B-roll energy

- Prefer one **baked master** `broll.mp4` when multiple sequential fullscreen clips fail to paint
- Motion: subtle kenburns / none — avoid chaotic multi-axis zooms
- Never let b-roll fight captions: keep text in safe zones (96px / bottom 340px)

## Captions

- Large, heavy, high-contrast; highlight color = primary red
- Word-level punch on emphasis (scale ~1.06), not constant bounce
- Stroke/shadow for readability over neon plates

## Sound sync (graphics follow audio)

| SFX kit role | Visual cue |
| --- | --- |
| `thump` / emphasis | Ring EMPHASIS pulse |
| `whoosh` | Cut / transition expand |
| `ui` | Data chip appear |
| `power_down` / `power_up` | Ring exit / return |

## Anti-patterns (reject)

- Purple-on-white AI defaults, glow-everything, pill clusters, stat strips in the hero
- Inset hero cards around the avatar
- SVG filter glows (break in HF)
- Logo `height:auto` (stretches into a tall oval)
- Decorative lines that ignore the ring as the spatial origin

## Implementation map

| Feel | Code / asset |
| --- | --- |
| Ring life | `ring.py` + `_RING_BASE_CSS` |
| Chips | `data_chips.py` |
| Captions / timeline | `timeline.py`, `composition.py` |
| Memes | `assets/memes` + scenario `memes` |
| Music by rubric | brandbook `music_by_rubric` |

When the cut feels “PowerPoint”: reduce simultaneous overlays, tighten emphasis timing, strengthen ring reaction to VO, and ensure b-roll actually changes behind the host.
