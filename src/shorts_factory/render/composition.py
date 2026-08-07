"""Write the HyperFrames composition: a self-contained 1080×1920 HTML project.

Design decisions worth knowing:

* **Absolute-time keyframes.** Every element gets one CSS animation that spans
  the whole composition, with keyframe stops expressed as percentages of the
  total duration. That makes the result deterministic under a seeking renderer —
  no animation depends on when an element happened to be inserted.
* **Media stays `.clip`.** Video, image and audio elements keep
  `data-start` / `data-duration` / `data-track-index` so HyperFrames drives
  playback and the audio mux itself.
* **Everything is local.** Assets are copied into `media/` and referenced with
  relative paths, so `--docker` renders see the same files the preview does.
"""

from __future__ import annotations

import html
import json
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..logging_utils import get_logger
from ..spec import Spec
from .ring import RingConfig, RingPlan, layout_box, lightning_overlay_svg, plan_ring, ring_css, ring_svg
from .timeline import (
    BOTTOM_UI_RESERVE,
    SAFE_MARGIN,
    TRACK_AUDIO_MUSIC,
    Element,
    Timeline,
)

log = get_logger("composition")

FONT_STACK = '"{family}", "Inter", "Helvetica Neue", Arial, system-ui, sans-serif'

_CAPTION_ANCHORS = {
    "center": {"top": "45%", "translate": "-50%"},
    "center_bottom": {"top": "62%", "translate": "-50%"},
    "bottom": {"top": "72%", "translate": "-50%"},
    "lower_third": {"top": "68%", "translate": "-50%"},
    "upper_third": {"top": "26%", "translate": "-50%"},
    "top": {"top": "16%", "translate": "-50%"},
}

_MOTION_KEYFRAMES = {
    "kenburns": ("scale(1.04) translate(0, 0)", "scale(1.16) translate(-1.5%, -1.5%)"),
    "zoom_in": ("scale(1.0)", "scale(1.18)"),
    "zoom_out": ("scale(1.18)", "scale(1.0)"),
    "pan_left": ("scale(1.12) translateX(3%)", "scale(1.12) translateX(-3%)"),
    "pan_right": ("scale(1.12) translateX(-3%)", "scale(1.12) translateX(3%)"),
    "parallax": ("scale(1.08) translateY(2%)", "scale(1.14) translateY(-2%)"),
    "none": ("scale(1.02)", "scale(1.02)"),
}


@dataclass
class CompositionResult:
    directory: Path
    index_html: Path
    manifest: Path
    media_files: int

    def to_dict(self) -> dict[str, object]:
        return {
            "directory": str(self.directory),
            "index_html": str(self.index_html),
            "manifest": str(self.manifest),
            "media_files": self.media_files,
        }


class CompositionWriter:
    """Turns a `Timeline` into an on-disk HyperFrames project."""

    def __init__(self, spec: Spec, timeline: Timeline, directory: Path, ring: RingPlan | None = None):
        self.spec = spec
        self.timeline = timeline
        self.directory = directory
        self.media_dir = directory / "media"
        self._copied: dict[Path, str] = {}
        self.ring = ring or plan_ring(spec, RingConfig())

    # -- entry point --------------------------------------------------------

    def write(self) -> CompositionResult:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)

        body = self._body()
        styles = self._styles()
        document = _PAGE.format(
            title=html.escape(self.spec.title),
            styles=styles,
            body=body,
            script=_SCRIPT.format(composition_id=self.timeline.composition_id),
        )

        index_html = self.directory / "index.html"
        index_html.write_text(document, encoding="utf-8")

        manifest = self.directory / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "spec_id": self.spec.id,
                    "title": self.spec.title,
                    "timeline": self.timeline.to_dict(),
                    "credits": self._credits(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._write_design_tokens()

        log.info(
            "composition written to %s (%d media files)",
            self.directory,
            len(self._copied),
            extra={"stage": "compose"},
        )
        return CompositionResult(self.directory, index_html, manifest, len(self._copied))

    # -- media --------------------------------------------------------------

    def _media_src(self, path: Path | None) -> str | None:
        """Copy an asset into the project and return its relative URL."""
        if path is None:
            return None
        source = Path(path)
        if not source.exists():
            log.warning("missing asset, skipping: %s", source, extra={"stage": "compose"})
            return None
        if source in self._copied:
            return self._copied[source]

        target = self.media_dir / source.name
        counter = 1
        while target.exists() and not _same_file(target, source):
            target = self.media_dir / f"{source.stem}_{counter}{source.suffix}"
            counter += 1
        if not target.exists():
            shutil.copy2(source, target)

        relative = f"media/{target.name}"
        self._copied[source] = relative
        return relative

    # -- markup -------------------------------------------------------------

    def _body(self) -> str:
        rows: list[str] = [
            f'<div id="stage" data-composition-id="{self.timeline.composition_id}" '
            f'data-start="0" data-duration="{self.timeline.duration:.3f}" '
            f'data-width="{self.timeline.width}" data-height="{self.timeline.height}" '
            f'data-fps="{self.timeline.fps}">'
        ]
        for element in self.timeline.visual_elements:
            markup = self._element_markup(element)
            if markup:
                rows.append("  " + markup)
        for element in self.timeline.audio_elements:
            markup = self._audio_markup(element)
            if markup:
                rows.append("  " + markup)
        if self.spec.brand.safe_area:
            rows.append('  <div class="safe-area" aria-hidden="true"></div>')
        rows.append("</div>")
        return "\n".join(rows)

    def _element_markup(self, element: Element) -> str:
        # Absolute-clock overlays (subscribe/logo-style) stay mounted for the
        # full composition so HF CSS animations are not restarted at data-start.
        if element.props.get("absolute_clock"):
            data_start = 0.0
            data_duration = self.timeline.duration
        else:
            data_start = element.start
            data_duration = element.duration
        attrs = (
            f'id="el_{element.id}" class="clip {element.kind}" '
            f'data-start="{data_start:.3f}" data-duration="{data_duration:.3f}" '
            f'data-track-index="{element.track}"'
        )

        if element.kind in {"video", "avatar"}:
            src = self._media_src(element.src)
            if not src:
                return ""
            loop = " loop" if element.props.get("loop") else ""
            trim = float(element.props.get("trim_start") or 0.0)
            trim_attr = f' data-trim-start="{trim:.3f}"' if trim > 0 else ""
            if element.kind == "avatar" and self.ring.visible:
                return self._avatar_with_ring(attrs + trim_attr, src, loop)
            return (
                f'<video {attrs}{trim_attr} src="{src}" muted playsinline preload="auto"{loop}></video>'
            )

        if element.kind == "image":
            src = self._media_src(element.src)
            if not src:
                return ""
            return f'<img {attrs} src="{src}" alt="">'

        if element.kind == "logo":
            src = self._media_src(element.src)
            if not src:
                return ""
            return f'<img {attrs} src="{src}" alt="">'

        if element.kind == "caption":
            return f"<div {attrs}>{self._caption_inner(element)}</div>"

        if element.kind == "text":
            role = element.props.get("role", "text")
            if role == "data_chip":
                inner = (
                    f'<span class="data-chip-inner">'
                    f'<span class="data-chip-label">{html.escape(element.text)}</span>'
                    f"</span>"
                )
            elif role == "cta":
                inner = f'<span class="cta-inner">{html.escape(element.text)}</span>'
            elif role == "subscribe":
                inner = (
                    f'<span class="subscribe-inner">'
                    f'<span class="subscribe-bell" aria-hidden="true"></span>'
                    f'{html.escape(element.text)}'
                    f"</span>"
                )
            else:
                inner = f'<span class="text-inner">{html.escape(element.text)}</span>'
            return f'<div {attrs} data-role="{role}">{inner}</div>'

        if element.kind == "shape":
            return f'<div {attrs} data-role="{element.props.get("role", "shape")}"></div>'

        return ""

    def _avatar_with_ring(self, attrs: str, src: str, loop: str) -> str:
        """Presenter clipped to a circle, wrapped by the neon pulse ring."""
        cfg = self.ring.config
        box = layout_box(cfg, anchor=cfg.default_anchor)
        size = box["size"]
        # Oval on inner shell so pulse_ring_life transform on the wrap still works.
        # Lightning uses class ring-lightning (never ring-network — HF left ghost).
        scale_y = cfg.scale_y
        return (
            f'<div id="pulse_ring" class="pulse-ring-wrap" '
            f'style="left:{box["left"]}px;top:{box["top"]}px;width:{size}px;height:{size}px;">'
            f"{lightning_overlay_svg(cfg, size)}"
            f'<div class="pulse-ring-oval" style="transform:scaleY({scale_y:.3f});transform-origin:center bottom;">'
            f'<div class="pulse-ring-breath">'
            f'<div class="pulse-ring-halo" aria-hidden="true"></div>'
            f'<div class="pulse-ring-neon" aria-hidden="true"></div>'
            f"{ring_svg(cfg, size)}"
            f'<div class="avatar-clip">'
            f'<div class="avatar-face">'
            f'<video {attrs} src="{src}" muted playsinline preload="auto"{loop}></video>'
            f"</div></div></div></div></div>"
        )

    def _caption_inner(self, element: Element) -> str:
        words: Iterable[dict[str, Any]] = element.props.get("words") or []
        if not words:
            return f'<span class="w">{html.escape(element.text)}</span>'
        spans = [
            f'<span class="w" id="w_{element.id}_{index}">{html.escape(str(word.get("text", "")))}</span>'
            for index, word in enumerate(words)
        ]
        return " ".join(spans)

    def _audio_markup(self, element: Element) -> str:
        src = self._media_src(element.src)
        if not src:
            return ""
        volume = float(element.props.get("volume", 1.0))
        loop = " loop" if element.props.get("loop") and element.track == TRACK_AUDIO_MUSIC else ""
        return (
            f'<audio id="el_{element.id}" class="clip" data-start="{element.start:.3f}" '
            f'data-duration="{element.duration:.3f}" data-track-index="{element.track}" '
            f'data-volume="{volume:.3f}" src="{src}" preload="auto"{loop}></audio>'
        )

    # -- styles -------------------------------------------------------------

    def _styles(self) -> str:
        brand = self.spec.brand
        captions = self.spec.captions
        anchor = _CAPTION_ANCHORS.get(captions.position, _CAPTION_ANCHORS["center_bottom"])

        blocks: list[str] = [
            self._brand_font_css(),
            _BASE_CSS.format(
                width=self.timeline.width,
                height=self.timeline.height,
                background=brand.color_background,
                primary=brand.color_primary,
                accent=brand.color_accent,
                font=FONT_STACK.format(family=brand.font_family),
                safe_margin=SAFE_MARGIN,
                bottom_reserve=BOTTOM_UI_RESERVE,
                caption_size=captions.size,
                caption_color=captions.text_color,
                caption_highlight=captions.highlight_color,
                caption_top=anchor["top"],
                caption_stroke=(
                    "-webkit-text-stroke: 3px rgba(0,0,0,.85); paint-order: stroke fill;"
                    if captions.stroke
                    else ""
                ),
            )
        ]

        for element in self.timeline.visual_elements:
            blocks.append(self._element_css(element))

        if self.ring.visible:
            blocks.append(ring_css(self.ring, duration=self.timeline.duration))
            blocks.append(self._ring_base_css())
            self._copy_brand_fonts()
        elif any(el.props.get("role") == "data_chip" for el in self.timeline.elements):
            self._copy_brand_fonts()

        return "\n".join(block for block in blocks if block)

    def _ring_base_css(self) -> str:
        """Neon bloom + face crop — CSS only (SVG filters die in HF capture)."""
        cfg = self.ring.config
        stroke = cfg.stroke
        glow = cfg.glow
        zoom = max(1.0, float(cfg.face_zoom))
        position = cfg.face_position or "center 30%"
        return _RING_BASE_CSS.format(
            stroke=stroke,
            glow=glow,
            face_zoom=f"{zoom:.3f}",
            face_position=position,
        )

    def _element_css(self, element: Element) -> str:
        duration = max(self.timeline.duration, 0.001)
        selector = f"#el_{element.id}"
        rules: list[str] = []
        keyframes: list[str] = []
        name = f"anim_{element.id}"

        # Position and box.
        if element.kind in {"video", "image"} and "layout" in element.props:
            layout = element.props["layout"]
            radius = layout.get("radius", 0)
            rules.append(
                f"position:absolute;top:{layout['top']}px;left:{layout['left']}px;"
                f"width:{layout['width']}px;height:{layout['height']}px;"
                f"object-fit:{layout.get('fit', 'cover')};"
                + (f"border-radius:{radius}px;overflow:hidden;" if radius else "")
            )
        elif element.kind == "avatar":
            rules.append(self._avatar_css(element))
        elif element.kind == "caption":
            rules.append("")  # positioning comes from the shared .caption class
        elif element.kind == "logo":
            rules.append(self._logo_css(element))
        elif element.kind == "shape" and element.props.get("role") == "backdrop":
            # Reference stage: black lower third under the oval, soft tint upward.
            rules.append(
                "position:absolute;inset:0;"
                "background:"
                "linear-gradient(180deg, "
                f"{element.props.get('accent', '#37E4FF')}18 0%, "
                f"{element.props.get('color', '#0A0C10')} 42%, "
                "#000000 78%, #000000 100%);"
            )

        # Timing: a single full-length animation with percentage stops.
        fade_in = min(0.35, element.duration * 0.3)
        fade_out = min(0.35, element.duration * 0.3)
        p0 = _pct(element.start, duration)
        p1 = _pct(element.start + fade_in, duration)
        p2 = _pct(element.end - fade_out, duration)
        p3 = _pct(element.end, duration)

        if element.kind in {"video", "image"}:
            motion_from, motion_to = _MOTION_KEYFRAMES.get(
                element.props.get("motion", "none"), _MOTION_KEYFRAMES["none"]
            )
            # The move itself runs between p1 and p2; CSS interpolates linearly
            # between those stops, so the fades never eat into the motion.
            keyframes.append(
                _keyframes(
                    name,
                    [
                        (0.0, f"opacity:0;transform:{motion_from};"),
                        (p0, f"opacity:0;transform:{motion_from};"),
                        (p1, f"opacity:1;transform:{motion_from};"),
                        (p2, f"opacity:1;transform:{motion_to};"),
                        (p3, f"opacity:0;transform:{motion_to};"),
                        (100.0, f"opacity:0;transform:{motion_to};"),
                    ],
                )
            )
        elif element.kind == "avatar":
            if self.ring.visible:
                # Visibility is driven by the ring wrapper; keep the video opaque.
                keyframes.append(
                    _keyframes(
                        name,
                        [
                            (0.0, "opacity:1;"),
                            (100.0, "opacity:1;"),
                        ],
                    )
                )
            else:
                keyframes.append(
                    _keyframes(
                        name,
                        [
                            (0.0, "opacity:0;transform:translateY(40px);"),
                            (p0, "opacity:0;transform:translateY(40px);"),
                            (p1, "opacity:1;transform:translateY(0);"),
                            (p2, "opacity:1;transform:translateY(0);"),
                            (p3, "opacity:0;transform:translateY(20px);"),
                            (100.0, "opacity:0;"),
                        ],
                    )
                )
        else:
            enter, exit_ = _entrance_for(element)
            role = element.props.get("role", "")
            # Subscribe/CTA text chips must keep horizontal centering in every stop.
            if role == "subscribe":
                hold = "translateX(-50%)"
                keyframes.append(
                    _keyframes(
                        name,
                        [
                            (0.0, f"opacity:0;transform:{enter};"),
                            (p0, f"opacity:0;transform:{enter};"),
                            (p1, f"opacity:1;transform:{hold};"),
                            (p2, f"opacity:1;transform:{hold};"),
                            (p3, f"opacity:0;transform:{exit_};"),
                            (100.0, "opacity:0;"),
                        ],
                    )
                )
            else:
                keyframes.append(
                    _keyframes(
                        name,
                        [
                            (0.0, f"opacity:0;transform:{enter};"),
                            (p0, f"opacity:0;transform:{enter};"),
                            (p1, "opacity:1;transform:none;"),
                            (p2, "opacity:1;transform:none;"),
                            (p3, f"opacity:0;transform:{exit_};"),
                            (100.0, "opacity:0;"),
                        ],
                    )
                )

        rules.append(f"animation:{name} {duration:.3f}s linear 0s 1 normal both;")
        css = [f"{selector}{{{''.join(rule for rule in rules if rule)}}}"]
        css.extend(keyframes)

        if element.kind == "caption" and self.spec.captions.style in {"karaoke", "word_pop"}:
            css.append(self._karaoke_css(element, duration))
        return "\n".join(css)

    def _karaoke_css(self, element: Element, duration: float) -> str:
        words = element.props.get("words") or []
        blocks: list[str] = []
        for index, word in enumerate(words):
            start = float(word.get("start", element.start))
            end = float(word.get("end", start + 0.2))
            name = f"kw_{element.id}_{index}"
            pre = _pct(start, duration)
            hit = _pct(min(start + 0.08, end), duration)
            blocks.append(
                _keyframes(
                    name,
                    [
                        (0.0, "color:var(--caption-color);transform:scale(1);"),
                        (max(pre - 0.0001, 0.0), "color:var(--caption-color);transform:scale(1);"),
                        (hit, "color:var(--caption-highlight);transform:scale(1.06);"),
                        (100.0, "color:var(--caption-highlight);transform:scale(1);"),
                    ],
                )
            )
            blocks.append(
                f"#w_{element.id}_{index}{{animation:{name} {duration:.3f}s linear 0s 1 normal both;}}"
            )
        return "\n".join(blocks)

    def _avatar_css(self, element: Element) -> str:
        if self.ring.visible:
            # Positioning lives on the wrapper; the video fills the clipped circle.
            return "position:absolute;inset:0;width:100%;height:100%;object-fit:cover;border-radius:50%;"
        layout = element.props.get("layout", {})
        # Explicit bottom plate (no ring): pixel box with head+mic framing.
        if layout.get("anchor") == "box" or (
            "top" in layout and "width" in layout and "height" in layout and "left" in layout
        ):
            pos = layout.get("object_position", "center 18%")
            radius = int(layout.get("radius") or 0)
            return (
                f"position:absolute;top:{int(layout['top'])}px;left:{int(layout['left'])}px;"
                f"width:{int(layout['width'])}px;height:{int(layout['height'])}px;"
                f"object-fit:{layout.get('fit', 'cover')};object-position:{pos};"
                + (f"border-radius:{radius}px;" if radius else "")
            )
        scale = float(layout.get("scale", 0.34))
        width = int(self.timeline.width * scale)
        anchor = layout.get("anchor", "bottom-right")
        base = f"position:absolute;width:{width}px;height:auto;object-fit:contain;"
        if anchor == "fullscreen":
            return "position:absolute;inset:0;width:100%;height:100%;object-fit:cover;"
        if anchor == "center":
            return base + "left:50%;top:50%;transform:translate(-50%,-50%);"
        vertical = (
            f"bottom:{layout.get('bottom', BOTTOM_UI_RESERVE)}px;"
            if "bottom" in anchor
            else f"top:{layout.get('top', 200)}px;"
        )
        if "center" in anchor:
            return base + vertical + "left:50%;transform:translateX(-50%);"
        horizontal = (
            f"right:{layout.get('right', SAFE_MARGIN)}px;"
            if "right" in anchor
            else f"left:{layout.get('left', SAFE_MARGIN)}px;"
        )
        return base + vertical + horizontal

    def _copy_brand_fonts(self) -> None:
        """Copy local woff2 files into media/ so the composition stays offline."""
        fonts_dir = Path(__file__).resolve().parents[3] / "brand" / "fonts"
        if not fonts_dir.is_dir():
            return
        for path in fonts_dir.glob("*.woff2"):
            self._media_src(path)

    def _brand_font_css(self) -> str:
        """Inline brand @font-face rules so HyperFrames lint sees declared families."""
        css = Path(__file__).resolve().parents[3] / "brand" / "fonts.css"
        if not css.exists():
            return ""
        try:
            return css.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _logo_css(self, element: Element) -> str:
        position = element.props.get("position", "top_left")
        opacity = float(element.props.get("opacity", 0.85))
        vertical = f"top:{SAFE_MARGIN}px;" if position.startswith("top") else f"bottom:{BOTTOM_UI_RESERVE}px;"
        horizontal = f"left:{SAFE_MARGIN}px;" if position.endswith("left") else f"right:{SAFE_MARGIN}px;"
        return (
            f"position:absolute;{vertical}{horizontal}"
            f"width:160px;height:160px;object-fit:contain;opacity:{opacity};"
        )

    # -- extras -------------------------------------------------------------

    def _credits(self) -> list[dict[str, str]]:
        seen: set[str] = set()
        credits: list[dict[str, str]] = []
        for element in self.timeline.visual_elements:
            source = str(element.props.get("source", ""))
            if not source or source in seen:
                continue
            seen.add(source)
            credits.append(
                {
                    "source": source,
                    "license": str(element.props.get("license", "")),
                    "credit": str(element.props.get("credit", "")),
                }
            )
        return credits

    def _write_design_tokens(self) -> None:
        """`DESIGN.md` is what HyperFrames' own skills read for house style."""
        brand = self.spec.brand
        content = f"""# Design tokens — {self.spec.title}

| Token | Value |
| --- | --- |
| Canvas | {self.timeline.width}×{self.timeline.height} @ {self.timeline.fps}fps |
| Primary | `{brand.color_primary}` |
| Accent | `{brand.color_accent}` |
| Background | `{brand.color_background}` |
| Font | {brand.font_family} |
| Safe margin | {SAFE_MARGIN}px |
| Bottom UI reserve | {BOTTOM_UI_RESERVE}px (YouTube overlays this area) |

Captions sit at `{self.spec.captions.position}`, {self.spec.captions.size}px,
highlight `{self.spec.captions.highlight_color}`.
Generated by branded-shorts-factory — edit the scenario JSON, not this file.
"""
        (self.directory / "DESIGN.md").write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Helpers and templates
# --------------------------------------------------------------------------- #


def _pct(time: float, duration: float) -> float:
    return max(0.0, min(100.0, (time / duration) * 100.0 if duration else 0.0))


def _keyframes(name: str, stops: list[tuple[float, str]]) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for percent, body in sorted(stops, key=lambda stop: stop[0]):
        key = f"{percent:.4f}"
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"{key}%{{{body}}}")
    return f"@keyframes {name}{{{''.join(parts)}}}"


def _entrance_for(element: Element) -> tuple[str, str]:
    role = element.props.get("role", "")
    if role == "cta":
        return "translateY(60px) scale(.92)", "translateY(-20px) scale(.98)"
    if role == "subscribe":
        # Keep translateX(-50%) so left:50% centering survives the opacity animation.
        return (
            "translateX(-50%) translateY(40px) scale(.9)",
            "translateX(-50%) translateY(8px) scale(1)",
        )
    if role == "outro":
        return "scale(.94)", "scale(1.02)"
    if role == "data_chip":
        return "translateY(28px) translateX(-50%)", "translateY(-8px) translateX(-50%)"
    if element.kind == "caption":
        return "translateY(18px) scale(.98)", "translateY(-10px)"
    return "translateY(24px)", "translateY(-12px)"


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and a.name == b.name
    except OSError:
        return False


_BASE_CSS = """\
:root {{
  --primary: {primary};
  --accent: {accent};
  --caption-color: {caption_color};
  --caption-highlight: {caption_highlight};
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ background: #000; width: {width}px; height: {height}px; overflow: hidden; }}
#stage {{
  position: relative;
  width: {width}px;
  height: {height}px;
  background: {background};
  font-family: {font};
  overflow: hidden;
}}
#stage > * {{ position: absolute; }}
.safe-area {{
  inset: {safe_margin}px {safe_margin}px {bottom_reserve}px {safe_margin}px;
  border: 0;
  pointer-events: none;
}}
.caption {{
  left: 50%;
  top: {caption_top};
  transform: translateX(-50%);
  width: {width}px;
  padding: 0 {safe_margin}px;
  text-align: center;
  font-size: {caption_size}px;
  font-weight: 800;
  line-height: 1.12;
  letter-spacing: -0.01em;
  color: var(--caption-color);
  text-shadow: 0 6px 28px rgba(0,0,0,.55);
  {caption_stroke}
}}
.caption .w {{ display: inline-block; }}
.text {{
  left: 50%;
  transform: translateX(-50%);
  width: {width}px;
  padding: 0 {safe_margin}px;
  text-align: center;
}}
.text[data-role="cta"] {{ bottom: {bottom_reserve}px; }}
.text[data-role="subscribe"] {{
  bottom: calc({bottom_reserve}px + 8px);
  left: 50%;
  transform: translateX(-50%);
  width: auto;
  padding: 0;
}}
.text[data-role="lower_third"] {{ bottom: calc({bottom_reserve}px + 220px); text-align: left; }}
.text[data-role="outro"] {{ top: 46%; }}
.text[data-role="data_chip"] {{
  top: auto;
  bottom: calc({bottom_reserve}px + 420px);
  left: {safe_margin}px;
  transform: none;
  width: auto;
  max-width: 78%;
  padding: 0;
  text-align: left;
}}
.text-inner {{
  display: inline-block;
  font-size: 54px;
  font-weight: 700;
  color: #fff;
  text-shadow: 0 4px 20px rgba(0,0,0,.5);
}}
.text[data-role="outro"] .text-inner {{ font-size: 76px; letter-spacing: -0.02em; }}
.data-chip-inner {{
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 16px 24px;
  background: #14171C;
  border: 1px solid color-mix(in srgb, var(--accent) 45%, transparent);
  border-left: 3px solid var(--primary);
  border-radius: 6px;
  box-shadow: 0 12px 40px rgba(0,0,0,.45), inset 0 0 0 1px rgba(55, 228, 255, 0.08);
}}
.data-chip-label {{
  font-family: "JetBrains Mono", "Space Grotesk", ui-monospace, monospace;
  font-size: 34px;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: #F5F7FA;
  white-space: nowrap;
}}
/* Action Stage — soft plate above the oval, no fullscreen under the host.
   Do NOT use CSS mask-image on <video> — HyperFrames capture often drops paint. */
.clip.image[data-track-index="2"],
.clip.video[data-track-index="2"] {{
  border-radius: 0;
  box-shadow: 0 24px 80px rgba(0,0,0,.55);
}}
.cta-inner {{
  display: inline-block;
  padding: 26px 52px;
  border-radius: 999px;
  background: var(--accent);
  color: #08090c;
  font-size: 52px;
  font-weight: 800;
  box-shadow: 0 18px 60px rgba(0,0,0,.45);
}}
.subscribe-inner {{
  display: inline-flex;
  align-items: center;
  gap: 14px;
  padding: 18px 36px;
  border-radius: 999px;
  background: var(--primary);
  color: #fff;
  font-size: 40px;
  font-weight: 800;
  letter-spacing: 0.01em;
  box-shadow: 0 14px 40px rgba(255, 42, 60, 0.45), 0 0 0 1px rgba(255,255,255,0.12);
}}
.subscribe-bell {{
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: #fff;
  box-shadow: inset 0 -6px 0 rgba(0,0,0,.12);
  position: relative;
}}
.subscribe-bell::after {{
  content: "";
  position: absolute;
  left: 50%;
  top: 6px;
  width: 12px;
  height: 14px;
  margin-left: -6px;
  border-radius: 6px 6px 4px 4px;
  background: var(--primary);
}}
"""

_RING_BASE_CSS = """\
.pulse-ring-wrap {{
  position: absolute;
  z-index: 30;
  pointer-events: none;
  overflow: visible;
  isolation: isolate;
  contain: layout style;
}}
.pulse-ring-oval {{
  position: relative;
  width: 100%;
  height: 100%;
  overflow: visible;
}}
.pulse-ring-breath {{
  position: relative;
  width: 100%;
  height: 100%;
  overflow: visible;
}}
/* Soft outer bloom — radial paint survives HF capture better than SVG blur. */
.pulse-ring-halo {{
  position: absolute;
  inset: -12%;
  z-index: 0;
  border-radius: 50%;
  background: radial-gradient(
    circle closest-side,
    transparent 64%,
    rgba(255, 42, 60, 0.55) 78%,
    rgba(255, 90, 110, 0.28) 88%,
    transparent 100%
  );
  pointer-events: none;
}}
/* Neon tube rim — thinner / less carpet-red; white-hot core via box-shadow. */
.pulse-ring-neon {{
  position: absolute;
  inset: 2.5%;
  z-index: 2;
  border-radius: 50%;
  border: 4px solid {stroke};
  box-shadow:
    0 0 1px 1px #fff,
    0 0 3px 1px #FFE0E4,
    0 0 8px 2px #FF8A96,
    0 0 16px 5px {stroke},
    0 0 28px 10px {glow},
    0 0 44px 16px rgba(255, 42, 60, 0.35),
    inset 0 0 6px 1px rgba(255, 255, 255, 0.7),
    inset 0 0 14px 3px rgba(255, 140, 150, 0.55);
  pointer-events: none;
}}
.pulse-ring-svg {{
  position: absolute;
  inset: 0;
  z-index: 3;
  pointer-events: none;
  opacity: 0.95;
}}
.avatar-clip {{
  position: absolute;
  inset: 4.5%;
  border-radius: 50%;
  overflow: hidden;
  z-index: 1;
  background: #0A0C10;
}}
.avatar-face {{
  width: 100%;
  height: 100%;
  transform: scale({face_zoom});
  transform-origin: center 70%;
}}
.avatar-face video,
.avatar-clip video {{
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: {face_position};
}}
/* Legacy class — never paint. */
.ring-network {{
  display: none !important;
}}
.ring-lightning {{
  z-index: 4;
  overflow: visible;
  opacity: 0.9;
}}
.ring-lightning .bolt-line {{
  stroke-dasharray: 180;
  stroke-dashoffset: 180;
  animation: bolt_draw 2.4s ease-in-out infinite alternate;
}}
.ring-lightning .bolt-line:nth-child(2) {{ animation-delay: 0.15s; }}
.ring-lightning .bolt-line:nth-child(3) {{ animation-delay: 0.3s; }}
.ring-lightning .bolt-line:nth-child(4) {{ animation-delay: 0.45s; }}
.ring-lightning .bolt-orb {{
  animation: bolt_pulse 1.8s ease-in-out infinite alternate;
}}
@keyframes bolt_draw {{
  to {{ stroke-dashoffset: 0; }}
}}
@keyframes bolt_pulse {{
  from {{ opacity: 0.15; }}
  to {{ opacity: 0.45; }}
}}
@keyframes net_draw {{
  to {{ stroke-dashoffset: 0; }}
}}
"""

_SCRIPT = """\
// HyperFrames reads the DOM for timing; this only exposes a seek hook so the
// preview and the renderer agree on the clock when driving CSS animations.
(function () {{
  const stage = document.getElementById('stage');
  if (!stage) return;
  const duration = parseFloat(stage.dataset.duration || '0');
  const animated = () => Array.from(document.getAnimations ? document.getAnimations() : []);

  function seek(seconds) {{
    const t = Math.max(0, Math.min(seconds, duration)) * 1000;
    animated().forEach((animation) => {{
      animation.pause();
      try {{ animation.currentTime = t; }} catch (err) {{ /* not seekable */ }}
    }});
    stage.querySelectorAll('video, audio').forEach((media) => {{
      const start = parseFloat(media.dataset.start || '0');
      const trim = parseFloat(media.dataset.trimStart || '0');
      const local = seconds - start;
      const length = parseFloat(media.dataset.duration || '0');
      if (local < 0 || local > length) {{ media.pause(); return; }}
      if (media.readyState > 0) {{
        const base = media.loop && media.duration ? local % media.duration : local;
        const target = Math.min((media.duration || base + trim) - 0.01, base + trim);
        if (Math.abs(media.currentTime - target) > 0.05) media.currentTime = target;
      }}
    }});
  }}

  window.__timelines = window.__timelines || {{}};
  window.__timelines['{composition_id}'] = {{ duration: duration, seek: seek }};
  window.__hyperframesSeek = seek;
}})();
"""

_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
{styles}
</style>
</head>
<body>
{body}
<script>
{script}
</script>
</body>
</html>
"""
