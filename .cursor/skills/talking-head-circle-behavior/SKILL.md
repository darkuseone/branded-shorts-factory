---
name: talking-head-circle-behavior
description: Talking-head inside the Pulse Ring circle — face crop, zoom, anchors, HF video-transform pitfalls, avatar_close.mp4. Use when the face looks too far/small, circle crop is wrong, or editing avatar layout / face_zoom / object-position.
paths: brand/brandbook.json,src/shorts_factory/render/ring.py,src/shorts_factory/render/composition.py,src/shorts_factory/voice/heygen.py,jobs/**/avatar*.mp4
---

# Talking-Head Circle Behavior

The presenter lives **inside** the Pulse Ring: circular clip + neon rim. The circle is the product UI; the face must read clearly on a phone.

## Layout

- Canvas: **1080×1920**, safe margin 96px, YouTube bottom reserve **340px**
- Default anchor: **`bottom_center`** (reference stage — host in lower third)
- Action Stage owns everything **above** the oval; never put fullscreen b-roll under the host for news/AI
- Diameter ≈ `0.34 × 1920` → ~653px wrap; face clip inset ~4.5%
- News/AI: **continuous oval for the whole VO** (`continuous_on_vo` + rubric). EXIT only when explicitly sparse and continuous is off.

## Critical HyperFrames rule

**CSS `transform` on `<video>` is ignored during HF capture.**

Wrong:

```css
.avatar-clip video { transform: scale(1.9); }  /* does nothing in render */
```

Right:

1. **Preferred:** bake a closer source with ffmpeg → `jobs/<id>/avatar_close.mp4`  
   Pipeline prefers `avatar_close.mp4` over `avatar.mp4` (`heygen.EXTERNAL_NAMES`).
2. **Also:** wrap video in `.avatar-face` and put `transform: scale(...)` on the **wrapper**, not the video.
3. Use `object-fit: cover` + `object-position` (brandbook `face_position`, e.g. `center 36%`).

## Face framing targets

| Goal | Guidance |
| --- | --- |
| Fill | Head + upper shoulders dominate the circle |
| Headroom | Small — hair near top inner rim, not floating mid-frame |
| Mouth | Visible for lip-sync (don’t crop above the nose) |
| Far talent | HeyGen often seats far → use ~2.5–3× ffmpeg crop around face, not CSS alone |

Example local crop (free, no APIs):

```bash
# Tune x/y/w/h to the face; output must stay 1080×1920
ffmpeg -y -i jobs/<id>/avatar.mp4 \
  -vf "crop=360:640:360:320,scale=1080:1920:flags=lanczos" \
  -c:v libx264 -preset fast -crf 18 -pix_fmt yuv420p -c:a copy \
  jobs/<id>/avatar_close.mp4
```

Brandbook knobs:

- `ring.face_zoom` — mild extra scale on `.avatar-face` (keep ~1.0–1.1 if `avatar_close` already tight)
- `ring.face_position` — vertical bias inside the circle

## Avatar windows

- Ring is **on** whenever avatar segments are on
- Gaps → EXIT → hidden → RETURN
- Avatar video: muted in composition; VO from mix / `voice_from_avatar.mp3`

## Don’ts

- Don’t enlarge the ring to “zoom” the face — crop the talent instead
- Don’t put cards, badges, or chips overlapping the circle
- Don’t use SVG masks that depend on filters for the face clip — `border-radius: 50%` + `overflow: hidden` on `.avatar-clip`

## Verify

Confirm face fill visually on a ring crop at t≈5–12s; `verify_short.py` must still pass neon/ghost checks.
