# Stock bank

Reusable pieces the cards and overlays are built from: plates, arrows, badges,
textures. Company and app marks live next door in `assets/icons/`.

Everything here is **committed**. That is the point of the folder: a piece
fetched once costs nothing the second time, needs no network on the runner,
and cannot change under a video that already shipped.

```
assets/stock/plates/    backing plates and cards
assets/stock/arrows/    arrow shapes and draw-on animations
assets/stock/badges/    labels, chips, callouts
assets/stock/textures/  grain, scanlines, gradients
```

## Where pieces come from

1. This folder. Free, offline, identical on every runner.
2. The Magnific subscription library — free, but rationed to 100 downloads a
   day on the current plan, tracked in `.state/magnific-downloads.json`.
   Anything fetched is written here, so commit it after a run that filled a
   gap.
3. Drawn by us in HTML and CSS (`redshift/render/infographics.py`). Always
   available, always on brand, never absent.

A missing piece is never an error. It costs the card some polish, nothing more.

## Resolution

Source video is capped at the 1080p class — the Short is 1080×1920 and a 4K
master is bandwidth spent on pixels the downscale throws away. Stills and
overlays should arrive at roughly the size they are drawn at; anything under
the frame gets lifted with Magnific's upscaler, free model first
(`MagnificClient.upscale`, `allow_paid` is the only door to a metered one).

## Licensing

Subscription-library assets are covered by the Magnific plan. Third-party
marks under `assets/icons/` are used nominatively — to refer to the company
being discussed — kept unaltered, and never used to imply endorsement.
