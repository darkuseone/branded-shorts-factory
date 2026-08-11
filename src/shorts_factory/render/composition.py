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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import RenderError
from ..logging_utils import get_logger
from ..media.ffmpeg import is_available as ffmpeg_available
from ..media.ffmpeg import remux_without_audio
from ..spec import Spec
from .camera import CameraMove, move_for
from .host_presence import (
    HOST_FOCUS,
    HostPlan,
    host_chrome_css,
    host_lower_layout,
    plan_host,
)
from .timeline import (
    BOTTOM_UI_RESERVE,
    SAFE_MARGIN,
    TRACK_AUDIO_MUSIC,
    Element,
    Timeline,
)

log = get_logger("composition")


def _reencode_silent(source: Path, target: Path) -> bool:
    """Fallback when stream-copy strip fails (odd containers / webm)."""
    import subprocess

    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        "20",
        "-movflags",
        "+faststart",
        str(target),
    ]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and target.exists() and target.stat().st_size > 0


#: No "Helvetica Neue" or "Arial" in the fallbacks. The renderer substitutes
#: both with Inter for cross-platform consistency and says so ("system font
#: will alias"), which means the preview and the render disagree about metrics
#: whenever a fallback is reached. Naming Inter directly makes them agree.
FONT_STACK = '"{family}", "Inter", system-ui, sans-serif'

_CAPTION_ANCHORS = {
    "center": {"top": "45%", "translate": "-50%"},
    "center_bottom": {"top": "62%", "translate": "-50%"},
    "bottom": {"top": "72%", "translate": "-50%"},
    "lower_third": {"top": "68%", "translate": "-50%"},
    "upper_third": {"top": "26%", "translate": "-50%"},
    "top": {"top": "16%", "translate": "-50%"},
}

#: Who paints over whom. Kept in one table because the alternative — a number
#: written wherever each element happens to be built — is how the subtitles
#: ended up *underneath* the footage: no z-index at all put them below every
#: positioned layer that had one.
Z_BACKDROP = 1
Z_MEDIA = 10
Z_HOST = 30
Z_CHIP = 40
Z_TEXT = 55
Z_CAPTION = 60
Z_LOGO = 70
Z_SAFE_AREA = 80


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

    def __init__(self, spec: Spec, timeline: Timeline, directory: Path, host: HostPlan | None = None):
        self.spec = spec
        self.timeline = timeline
        self.directory = directory
        self.media_dir = directory / "media"
        self._copied: dict[tuple[Path, bool], str] = {}
        self.host = host or plan_host(spec)

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
        self._assert_video_is_silent()

        log.info(
            "composition written to %s (%d media files)",
            self.directory,
            len(self._copied),
            extra={"stage": "compose"},
        )
        return CompositionResult(self.directory, index_html, manifest, len(self._copied))

    def _assert_video_is_silent(self) -> None:
        """No clip in the composition may carry its own audio.

        The timeline mix is the only soundtrack. Every visual clip is stripped
        on the way in and marked `muted`, so in principle this can never fire —
        and yet the first cut came back with two voices talking over each
        other, which means one of those steps failed without saying so. There
        are three ways it can: the remux fails and the re-encode fails too,
        HyperFrames muxes a muted <video> anyway, or the no-ffmpeg fallback
        copies a clip verbatim.

        Rather than guess which, check the result. A file that still has an
        audio stream is stripped here by force; if even that fails the run
        stops, because a Short with two narrators is not worth shipping and a
        silent failure is how it reached the client in the first place.
        """
        from ..media.ffmpeg import probe

        offenders: list[Path] = []
        for path in sorted(self.media_dir.glob("*")):
            if path.suffix.lower() not in {".mp4", ".webm", ".mov", ".m4v"}:
                continue
            info = probe(path, self.settings_ffprobe)
            if info is None or not info.has_audio:
                continue
            log.error("%s still carries audio; stripping it", path.name, extra={"stage": "compose"})
            silent = path.with_name(f"{path.stem}_forced_silent{path.suffix}")
            if _reencode_silent(path, silent):
                silent.replace(path)
                self.timeline.warnings.append(f"{path.name} needed a forced audio strip")
                continue
            offenders.append(path)

        if offenders:
            names = ", ".join(path.name for path in offenders)
            raise RenderError(
                f"these clips still carry audio and could not be silenced: {names}. "
                "The timeline mix is the only soundtrack; shipping this would put "
                "two voices in the Short."
            )

    @property
    def settings_ffprobe(self) -> str:
        return "ffprobe"

    # -- media --------------------------------------------------------------

    def _media_src(self, path: Path | None, *, strip_audio: bool = False) -> str | None:
        """Copy an asset into the project and return its relative URL.

        ``strip_audio=True`` remuxes video-only. Required for muted avatar /
        b-roll clips: HTML ``muted`` alone does not stop HyperFrames from
        muxing embedded AAC into the final Short (double VO with the mix).
        """
        if path is None:
            return None
        source = Path(path)
        if not source.exists():
            log.warning("missing asset, skipping: %s", source, extra={"stage": "compose"})
            return None
        cache_key = (source, strip_audio)
        if cache_key in self._copied:
            return self._copied[cache_key]

        if strip_audio:
            target = self.media_dir / f"{source.stem}_silent{source.suffix}"
            counter = 1
            while target.exists() and target.stat().st_size == 0:
                target = self.media_dir / f"{source.stem}_silent_{counter}{source.suffix}"
                counter += 1
            if not target.exists() and not remux_without_audio(source, target):
                # Re-encode without audio — never copy AAC into the composition
                # (HyperFrames may mux muted <video> tracks → "two soundtracks").
                reencoded = self.media_dir / f"{source.stem}_silent_reenc.mp4"
                if not _reencode_silent(source, reencoded):
                    if not ffmpeg_available("ffmpeg"):
                        # No ffmpeg at all is an environment problem, not a bad
                        # clip. Dropping every asset over it produces a
                        # composition with no b-roll and no error — far worse
                        # than the double-VO risk stripping guards against.
                        log.error(
                            "no ffmpeg: copying %s with its audio intact — "
                            "mute it in the timeline or the mix may double up",
                            source.name,
                            extra={"stage": "compose"},
                        )
                        self.timeline.warnings.append(
                            f"{source.name} kept its audio track: ffmpeg is not installed"
                        )
                        return self._copy_verbatim(source, cache_key)
                    log.error(
                        "cannot strip audio from %s; skipping clip",
                        source.name,
                        extra={"stage": "compose"},
                    )
                    return None
                target = reencoded
                log.warning(
                    "strip via remux failed; re-encoded silent: %s",
                    source.name,
                    extra={"stage": "compose"},
                )
        else:
            target = self.media_dir / source.name
            counter = 1
            while target.exists() and not _same_file(target, source):
                target = self.media_dir / f"{source.stem}_{counter}{source.suffix}"
                counter += 1
            if not target.exists():
                shutil.copy2(source, target)

        relative = f"media/{target.name}"
        self._copied[cache_key] = relative
        return relative

    def _copy_verbatim(self, source: Path, cache_key: tuple) -> str:
        """Copy an asset as-is, audio and all."""
        target = self.media_dir / source.name
        counter = 1
        while target.exists() and not _same_file(target, source):
            target = self.media_dir / f"{source.stem}_{counter}{source.suffix}"
            counter += 1
        if not target.exists():
            shutil.copy2(source, target)
        relative = f"media/{target.name}"
        self._copied[cache_key] = relative
        return relative

    # -- markup -------------------------------------------------------------

    def _body(self) -> str:
        rows: list[str] = [
            # `data-no-timeline` is the honest declaration for a composition
            # animated entirely in CSS. Without it the producer polls for a
            # GSAP timeline registration for 45 seconds before giving up — on
            # every render — and the alternative, registering a look-alike
            # object under `window.__timelines`, invites the runtime to drive a
            # timeline that does not exist.
            f'<div id="stage" data-composition-id="{self.timeline.composition_id}" data-no-timeline '
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
        attrs = (
            f'id="el_{element.id}" class="clip {element.kind}" '
            f'data-start="{element.start:.3f}" data-duration="{element.duration:.3f}" '
            f'data-track-index="{element.track}"'
        )

        if element.kind in {"video", "avatar"}:
            # Always strip embedded audio from visual clips (avatar, b-roll,
            # memes). HTML ``muted`` is not enough — HyperFrames/Chromium can
            # still mux AAC into the output and it reads as a second soundtrack.
            strip = True
            src = self._media_src(element.src, strip_audio=strip)
            if not src:
                return ""
            loop = " loop" if element.props.get("loop") else ""
            if element.kind == "avatar" and self.host.visible:
                return self._avatar_with_host(attrs, src, loop)
            # Animate a wrapper DIV — CSS opacity/transform on <video> itself
            # is dropped by HyperFrames capture (avatar works because the
            # parent .host-wrap is animated, not the video node).
            return (
                f'<div id="wrap_{element.id}" class="media-shell">'
                f'<video {attrs} src="{src}" muted playsinline preload="auto"{loop}></video>'
                f"</div>"
            )

        if element.kind == "image":
            src = self._media_src(element.src)
            if not src:
                return ""
            return f'<div id="wrap_{element.id}" class="media-shell"><img {attrs} src="{src}" alt=""></div>'

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
            else:
                inner = f'<span class="text-inner">{html.escape(element.text)}</span>'
            return f'<div {attrs} data-role="{role}">{inner}</div>'

        if element.kind == "shape":
            return f'<div {attrs} data-role="{element.props.get("role", "shape")}"></div>'

        return ""

    def _avatar_with_host(self, attrs: str, src: str, loop: str) -> str:
        """Host box only — wrap matches the host rectangle, never full-bleed.

        A full-screen ``host-wrap`` overlay was compositing above b-roll in
        HyperFrames and wiped the upper 60% to black even when opacity CSS
        looked correct. Layout keyframes animate this wrap between SPLIT and
        FULL_HOST; FULL_FOOTAGE collapses the wrap via host_chrome (layout+opacity).
        """
        layout = host_lower_layout()
        return (
            f'<div id="host_wrap" class="host-wrap" style="z-index:{Z_HOST};'
            f"top:{layout['top']}px;left:{layout['left']}px;"
            f'width:{layout["width"]}px;height:{layout["height"]}px;">'
            f'<div class="host-frame">'
            f'<div class="host-video">'
            f'<video {attrs} src="{src}" muted playsinline preload="auto"{loop}></video>'
            f"</div></div></div>"
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
        skin = _caption_skin(captions, brand)

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
                caption_size=skin["size"],
                caption_color=skin["color"],
                caption_highlight=captions.highlight_color,
                caption_top=anchor["top"],
                caption_stroke=skin["stroke"],
                caption_glow=skin["glow"],
                z_caption=Z_CAPTION,
                z_text=Z_TEXT,
                z_chip=Z_CHIP,
                z_safe=Z_SAFE_AREA,
            ),
        ]

        for element in self.timeline.visual_elements:
            blocks.append(self._element_css(element))

        if self.host.visible:
            blocks.append(host_chrome_css(self.host, duration=self.timeline.duration))
            blocks.append(self._host_base_css())
            self._copy_brand_fonts()
        elif any(el.props.get("role") == "data_chip" for el in self.timeline.elements):
            self._copy_brand_fonts()

        return "\n".join(block for block in blocks if block)

    def _host_base_css(self) -> str:
        """Rectangular host frame. Transparent fill — studio avatar is the plate."""
        return _HOST_BASE_CSS.format(background="transparent", focus=HOST_FOCUS)

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
            # Animate the shell; the inner media fills it at opacity 1.
            #
            # A card is a transparent WebM: its plate sits in the middle and
            # the rest of the frame shows whatever is underneath, which is the
            # page — flat near-black. The graphic beat then reads as the video
            # having stopped rather than as a designed frame. Giving the shell
            # the brand's own ground costs nothing and makes the card land on
            # something.
            ground = ""
            if self._is_card(element):
                brand = self.spec.brand
                ground = (
                    f"background:radial-gradient(120% 90% at 50% 20%, "
                    f"{brand.color_primary}1F 0%, {brand.color_background} 68%);"
                )
            rules.append(
                f"position:absolute;top:{layout['top']}px;left:{layout['left']}px;"
                f"width:{layout['width']}px;height:{layout['height']}px;"
                f"z-index:{Z_MEDIA};overflow:hidden;{ground}"
                + (f"border-radius:{radius}px;" if radius else "")
            )
            # Inner clip fills the shell; object-fit lives on the media node.
            keyframes.append(
                f"#el_{element.id}{{width:100%;height:100%;object-fit:{layout.get('fit', 'cover')};"
                f"display:block;opacity:1;}}"
            )
        elif element.kind == "avatar":
            rules.append(self._avatar_css(element))
        elif element.kind == "caption":
            rules.append("")  # positioning comes from the shared .caption class
        elif element.kind == "logo":
            rules.append(self._logo_css(element))
        elif element.kind == "shape" and element.props.get("role") == "backdrop":
            rules.append(
                f"position:absolute;inset:0;z-index:{Z_BACKDROP};"
                "background:radial-gradient(120% 90% at 50% 12%, "
                f"{element.props.get('accent', '#0F62FE')}22 0%, "
                f"{element.props.get('color', '#050608')} 62%);"
            )

        # Which clock this element's keyframes are written against.
        #
        # HyperFrames seeks the animation on a `.clip` element in *clip-local*
        # time — the contract is "give timed elements a data-start so local
        # animation time matches the clip". Keyframes written against the
        # composition clock therefore evaluate at ~0% for the clip's whole life
        # and the element never appears. That is why captions, the CTA and the
        # outro were missing from rendered frames while looking perfect in a
        # browser, which runs the same CSS on the wall clock.
        #
        # Video and image shells animate a plain wrapper `<div>`, which is not a
        # clip and is driven on the composition clock. So the two cases need
        # two timebases, and each one has to match how the runtime drives it.
        local = _is_clip_animated(element)
        span = max(element.duration, 0.001) if local else duration
        origin = element.start if local else 0.0

        def pct(when: float) -> float:
            return _pct(when - origin, span)

        # Timing: one animation per element with percentage stops.
        fade_in = min(0.45, element.duration * 0.35)
        fade_out = min(0.45, element.duration * 0.35)
        p0 = pct(element.start)
        p1 = pct(element.start + fade_in)
        p2 = pct(element.end - fade_out)
        p3 = pct(element.end)

        if element.kind in {"video", "image"}:
            move = self._camera_move(element)
            motion_from, motion_to = move.as_pair()
            peak = (
                "0"
                if element.props.get("hidden_under_host")
                else ("0.22" if element.props.get("dim") else "1")
            )
            # A cut, not a dissolve. The shot is at full opacity from its own
            # first frame to its last, and the move runs edge to edge across
            # that window — so the cut lands mid-motion on both sides, which is
            # what carries the eye across it. Cross-fading instead settles the
            # outgoing shot to a stop and starts the incoming one from rest:
            # two dead beats at every cut, and twenty-five of them read as a
            # slide show however well the lengths are chosen.
            keyframes.append(
                _keyframes(
                    name,
                    _cut_stops(
                        p0,
                        p3,
                        on=f"opacity:{peak};transform:{motion_from};",
                        off=f"opacity:0;transform:{motion_from};",
                        holding=f"opacity:{peak};transform:{motion_to};",
                        after=f"opacity:0;transform:{motion_to};",
                    ),
                )
            )
        elif element.kind == "avatar":
            if self.host.visible:
                # Visibility is driven by .host-wrap; keep the video opaque.
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
        elif element.kind == "caption" and self.spec.captions.style == "word":
            # One word at a time means each cue ends exactly where the next
            # begins. A cross-fade there would put two words on top of each
            # other for a moment, so the switch is a cut: full opacity from
            # the first frame of the cue to a frame before the last.
            keyframes.append(_keyframes(name, _word_cut_stops(element, pct)))
        else:
            enter, exit_ = _entrance_for(element)
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

        rules.append(f"animation:{name} {span:.3f}s linear 0s 1 normal both;")
        # Video/image shells are animated; avatar/caption/text keep element id.
        target = (
            f"#wrap_{element.id}"
            if element.kind in {"video", "image"} and "layout" in element.props
            else selector
        )
        css = [f"{target}{{{''.join(rule for rule in rules if rule)}}}"]
        css.extend(keyframes)

        if element.kind == "caption" and self.spec.captions.style in {"karaoke", "word_pop"}:
            css.append(self._karaoke_css(element, span, pct))
        return "\n".join(css)

    def _is_card(self, element: Element) -> bool:
        """An infographic we rendered ourselves — a transparent WebM plate."""
        if element.props.get("card") or element.props.get("infographic"):
            return True
        return element.src is not None and element.src.suffix.lower() == ".webm"

    def _camera_move(self, element: Element) -> CameraMove:
        """This shot's move, chosen by what it carries and where it falls.

        A card is locked because it has to be read, the last shot of the film
        rises because that is the vector reserved for a conclusion, and
        everything else travels along the current at the film's one speed.
        """
        readable = self._is_card(element)
        closing = element.end >= self.timeline.duration - 0.05
        return move_for(
            duration=element.duration,
            hint=str(element.props.get("motion", "")),
            readable=readable,
            closing=closing,
        )

    def _karaoke_css(self, element: Element, span: float, pct: Callable[[float], float]) -> str:
        words = element.props.get("words") or []
        blocks: list[str] = []
        for index, word in enumerate(words):
            start = float(word.get("start", element.start))
            end = float(word.get("end", start + 0.2))
            name = f"kw_{element.id}_{index}"
            pre = pct(start)
            hit = pct(min(start + 0.08, end))
            done = pct(end)
            blocks.append(
                _keyframes(
                    name,
                    [
                        (0.0, "color:var(--caption-color);"),
                        (max(pre - 0.0001, 0.0), "color:var(--caption-color);"),
                        (hit, "color:var(--caption-highlight);"),
                        # Karaoke marks the word being said, then hands the
                        # line back. Holding the highlight to 100% left every
                        # spoken word crimson, so by the end of a line the
                        # whole subtitle was red.
                        (done, "color:var(--caption-highlight);"),
                        (min(done + 0.0001, 100.0), "color:var(--caption-color);"),
                        (100.0, "color:var(--caption-color);"),
                    ],
                )
            )
            blocks.append(f"#w_{element.id}_{index}{{animation:{name} {span:.3f}s linear 0s 1 normal both;}}")
        return "\n".join(blocks)

    def _avatar_css(self, element: Element) -> str:
        if self.host.visible:
            # Positioning lives on .host-frame; the video fills the rectangle.
            #
            # object-position matters more than it looks. The avatar render is
            # 1080x1920, and the SPLIT band is 1080x768, so `cover` centred
            # keeps the middle 768 rows of a 1920-row frame — the chin and
            # chest of a head-and-shoulders shot, or forehead and half a face
            # when the source is already a close crop. Anchoring near the top
            # keeps the face in the band without scaling the presenter at all.
            return (
                "position:absolute;inset:0;width:100%;height:100%;"
                f"object-fit:cover;object-position:{HOST_FOCUS};"
            )
        layout = element.props.get("layout", {})
        scale = float(layout.get("scale", 0.34))
        width = int(self.timeline.width * scale)
        anchor = layout.get("anchor", "split")
        base = f"position:absolute;width:{width}px;height:auto;object-fit:contain;"
        if anchor in {"fullscreen", "full_host"}:
            return "position:absolute;inset:0;width:100%;height:100%;object-fit:cover;"
        if anchor == "center":
            return base + "left:50%;top:50%;transform:translate(-50%,-50%);"
        # Default rectangular lower band.
        box = host_lower_layout()
        return (
            f"position:absolute;top:{box['top']}px;left:0;"
            f"width:{box['width']}px;height:{box['height']}px;"
            f"object-fit:cover;object-position:{box.get('focus', HOST_FOCUS)};"
        )

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
            f"position:absolute;{vertical}{horizontal}z-index:{Z_LOGO};"
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


def _cut_stops(
    start: float, end: float, *, on: str, off: str, holding: str, after: str
) -> list[tuple[float, str]]:
    """Full opacity between two stops, nothing before or after — a cut.

    The leading "off" pair is emitted only when the shot does not begin at 0%.
    When it does — the first shot of the film, or any clip animated on its own
    local clock — an "off" stop at 0% wins de-duplication against the "on" stop
    at the same instant, and the cut silently becomes a fade from black across
    the whole shot.
    """
    stops: list[tuple[float, str]] = []
    if start > 0.0:
        stops.append((0.0, off))
        stops.append((max(start - 0.0001, 0.0), off))
    stops.append((start, on))
    stops.append((end, holding))
    if end < 100.0:
        stops.append((min(end + 0.0001, 100.0), after))
        stops.append((100.0, after))
    return stops


def _is_clip_animated(element: Element) -> bool:
    """Whether the animated node is the `.clip` itself rather than a wrapper.

    Video and image slots animate a wrapper `<div>` around the media, because
    CSS opacity on a `<video>` is dropped by HyperFrames capture. Everything
    else — captions, text, the logo, shapes, the avatar — carries the clip
    class on the animated node, and the runtime drives those on clip-local
    time. The distinction decides which clock the keyframes are written in.
    """
    return not (element.kind in {"video", "image"} and "layout" in element.props)


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


#: How long the word takes to settle out of its pop. Short enough that a
#: 0.22s cue still lands on its resting size before it is replaced.
WORD_POP_S = 0.07

#: One frame at 30fps. The cue is taken off screen this much early so the
#: outgoing and incoming word never share a frame.
WORD_GAP_S = 1.0 / 30.0


def _word_cut_stops(element: Element, pct: Callable[[float], float]) -> list[tuple[float, str]]:
    """Hard on, small pop, hard off — the timing of a spoken word."""
    settle = min(element.start + WORD_POP_S, element.end)
    leave = max(element.end - WORD_GAP_S, settle)
    on = pct(element.start)
    stops: list[tuple[float, str]] = []
    if on > 0.0:
        # Composition-clock timebase: hold the word off screen until its cue.
        # On the clip-local clock the cue starts at 0% and a leading "off" stop
        # would win de-duplication, turning the cut into a fade-in.
        stops.append((0.0, "opacity:0;transform:scale(.84);"))
        stops.append((max(on - 0.0001, 0.0), "opacity:0;transform:scale(.84);"))
    stops += [
        (on, "opacity:1;transform:scale(1.10);"),
        (pct(settle), "opacity:1;transform:scale(1);"),
        # Listed before the hold so that on a cue too short to separate them,
        # de-duplication keeps the stop that takes the word off screen.
        (pct(element.end), "opacity:0;transform:scale(1);"),
        (pct(leave), "opacity:1;transform:scale(1);"),
        (100.0, "opacity:0;"),
    ]
    return stops


def _entrance_for(element: Element) -> tuple[str, str]:
    role = element.props.get("role", "")
    if role == "cta":
        return "translateY(60px) scale(.92)", "translateY(-20px) scale(.98)"
    if role == "outro":
        return "scale(.94)", "scale(1.02)"
    if role == "data_chip":
        # No translateX here: the chip is placed by its left inset, and a
        # transform written by the animation would drag it out of the frame.
        return "translateY(28px)", "translateY(-8px)"
    if element.kind == "caption":
        # Opacity-only — translateY/scale on captions reads as text jumping.
        return "none", "none"
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
  z-index: {z_safe};
}}
.caption {{
  /* Centred by the box, never by `transform`. Every element carries an
     entrance animation that writes `transform` with fill:both, so a
     translateX(-50%) sitting in the class is overwritten the moment the
     animation is attached — which put the block's *left* edge at mid-frame
     and ran the text off the side. Left/right insets cannot be overwritten. */
  left: 0;
  right: 0;
  top: {caption_top};
  /* Above everything. Without an explicit layer the subtitle painted *under*
     the b-roll shells (z-index 10) and the presenter (30): invisible over an
     opaque clip, a washed-out grey under a dimmed one, and readable only in
     the gaps — which is exactly "the subtitles sometimes appear". */
  z-index: {z_caption};
  /* The safe area, not the whole frame: a line as wide as the video has its
     first and last glyph on the bezel, and anything unbreakable ran clean off
     the edge. */
  width: {width}px;
  padding: 0 calc({safe_margin}px * 2);
  overflow-wrap: anywhere;
  word-break: normal;
  hyphens: auto;
  text-align: center;
  font-size: {caption_size}px;
  font-weight: 800;
  line-height: 1.12;
  letter-spacing: -0.01em;
  color: var(--caption-color);
  text-shadow: {caption_glow};
  {caption_stroke}
}}
.caption .w {{ display: inline-block; }}
.text {{
  left: 0;
  right: 0;
  width: {width}px;
  padding: 0 {safe_margin}px;
  text-align: center;
  z-index: {z_text};
}}
.text[data-role="cta"] {{ bottom: {bottom_reserve}px; }}
.text[data-role="lower_third"] {{ bottom: calc({bottom_reserve}px + 220px); text-align: left; }}
.text[data-role="outro"] {{ top: 46%; }}
.text[data-role="data_chip"] {{
  z-index: {z_chip};
  top: auto;
  bottom: calc({bottom_reserve}px + 420px);
  left: {safe_margin}px;
  right: auto;
  width: auto;
  max-width: 78%;
  padding: 0;
  text-align: left;
}}
.text-inner {{
  display: inline-block;
  font-size: 54px;
  font-weight: 700;
  color: #F1F5F9;
  text-shadow: {caption_glow};
}}
.text[data-role="outro"] .text-inner {{ font-size: 76px; letter-spacing: -0.02em; }}
.data-chip-inner {{
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 14px 22px;
  background: #0A0A0F;
  border: 1px solid color-mix(in srgb, var(--primary) 70%, transparent);
  border-left: 3px solid var(--primary);
  border-radius: 4px;
  box-shadow: 0 12px 40px rgba(0,0,0,.45);
}}
.data-chip-label {{
  font-family: "JetBrains Mono", "Space Grotesk", ui-monospace, monospace;
  font-size: 34px;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: #F1F5F9;
  white-space: nowrap;
}}
.cta-inner {{
  display: inline-block;
  padding: 26px 52px;
  border-radius: 8px;
  background: var(--primary);
  color: #F1F5F9;
  font-size: 52px;
  font-weight: 800;
  box-shadow: 0 18px 60px rgba(0,0,0,.45), 0 0 24px color-mix(in srgb, var(--primary) 45%, transparent);
}}
"""

_HOST_BASE_CSS = """\
.host-wrap {{
  position: absolute;
  z-index: 30;
  pointer-events: none;
  overflow: hidden;
}}
.host-frame {{
  position: absolute;
  inset: 0;
  overflow: hidden;
  background: {background};
}}
.host-video {{
  position: absolute;
  inset: 0;
}}
.host-video video {{
  width: 100%;
  height: 100%;
  object-fit: cover;
  /* Framing pinned to the face. The source is 1080x1920 and the split band is
     1080x768, so `cover` already discards two thirds of the frame; where that
     window sits is the whole difference between a portrait and a forehead. */
  object-position: {focus};
  /* No standing zoom. The presenter was permanently blown up 8%, on top of
     that crop, which is how a head-and-shoulders render came out as half a
     face. Emphasis is opt-in, per shot, and capped — see host_emphasis_css. */
  transform: none;
  transform-origin: center 18%;
}}
"""


#: One word alone on screen carries the frame, so it is set larger than a
#: line of the same subtitle would be — but not so large that an ordinary
#: long word has to wrap.
WORD_SIZE_FACTOR = 1.3
WORD_SIZE_CAP = 96


def _caption_skin(captions, brand) -> dict[str, str | int]:
    """How the subtitle is painted, per style.

    `word` is the requested look: one word at a time, plain white with a thin
    black outline. The brand halo is deliberately absent — a crimson glow on
    white glyphs at this size tints the whole word pink over bright footage,
    and the outline is what actually keeps it readable.
    """
    if captions.style == "word":
        size = min(int(round(captions.size * WORD_SIZE_FACTOR)), WORD_SIZE_CAP)
        # Thin: a hair over 3% of the cap height, which reads as a drawn edge
        # rather than a black slab around the letters.
        stroke_px = max(2, round(size * 0.035))
        return {
            "size": size,
            "color": "#FFFFFF",
            "stroke": (
                f"-webkit-text-stroke: {stroke_px}px #000000; paint-order: stroke fill; text-wrap: balance;"
            ),
            "glow": "0 2px 6px rgba(0,0,0,.55), 0 8px 26px rgba(0,0,0,.40)",
        }
    return {
        "size": captions.size,
        "color": captions.text_color,
        "stroke": (
            "-webkit-text-stroke: 1.5px rgba(5,5,8,.65); paint-order: stroke fill;" if captions.stroke else ""
        ),
        "glow": _caption_glow_css(brand.color_primary),
    }


def _caption_glow_css(primary: str) -> str:
    """Soft crimson glow + light chromatic aberration (no SVG filters)."""
    # A crimson halo this wide bleeds over white glyphs and the whole subtitle
    # reads red. Keep a hint of brand and let the drop shadow do the work of
    # separating text from footage.
    return f"0 0 10px {primary}44, 0 3px 10px rgba(0,0,0,.75), 0 8px 30px rgba(0,0,0,.55)"


_SCRIPT = """\
// A seek hook for our own preview only. HyperFrames drives the composition
// with its own runtime and its own clock; this exists so opening index.html
// by hand shows the same frame the renderer would.
//
// Two things it must not do. It must not register under `window.__timelines`:
// that key belongs to GSAP timelines, one per composition, and putting a
// look-alike object there invites the runtime to drive a timeline that does
// not exist. And it must not seek every animation to composition time — the
// runtime seeks a `.clip` element's animation in clip-local time, so doing
// otherwise here would show one thing in preview and render another, which is
// the exact failure this composition already shipped once.
(function () {{
  const stage = document.getElementById('stage');
  if (!stage) return;
  const duration = parseFloat(stage.dataset.duration || '0');
  const animated = () => Array.from(document.getAnimations ? document.getAnimations() : []);

  function originOf(animation) {{
    // A clip's animation runs on the clip's own clock; everything else (the
    // wrappers around video and image slots) runs on the composition's.
    const target = animation.effect && animation.effect.target;
    const clip = target && target.closest ? target.closest('.clip') : null;
    return clip === target && clip ? parseFloat(clip.dataset.start || '0') : 0;
  }}

  function seek(seconds) {{
    const now = Math.max(0, Math.min(seconds, duration));
    animated().forEach((animation) => {{
      animation.pause();
      try {{ animation.currentTime = Math.max(0, now - originOf(animation)) * 1000; }}
      catch (err) {{ /* not seekable */ }}
    }});
    stage.querySelectorAll('video, audio').forEach((media) => {{
      const start = parseFloat(media.dataset.start || '0');
      const local = seconds - start;
      const length = parseFloat(media.dataset.duration || '0');
      if (local < 0 || local > length) {{ media.pause(); return; }}
      if (media.readyState > 0) {{
        const target = media.loop && media.duration ? local % media.duration : local;
        if (Math.abs(media.currentTime - target) > 0.05) media.currentTime = target;
      }}
    }});
  }}

  window.__hyperframesSeek = seek;
  window.__redshiftComposition = {{ id: '{composition_id}', duration: duration, seek: seek }};
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
