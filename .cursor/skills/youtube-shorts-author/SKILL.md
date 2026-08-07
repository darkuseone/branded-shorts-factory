---
name: youtube-shorts-author
description: Write and critique REDSHIFT YouTube Shorts scripts — hook in 0–3s, retention structure, Nikita2 ElevenLabs v3 Russian VO, anti-repeat visuals, personal-brand news/sci-pop tone. Use when drafting jobs/*.json, researching a topic, or fixing a weak hook.
paths: jobs/**/*.json,brand/brandbook.json,src/shorts_factory/author/**,assets/catalogs/**
---

# YouTube Shorts Author (REDSHIFT)

Personal-brand Shorts: the host is on camera and tells a news/sci-pop story. The picture follows the voice second-by-second. It must **not** look like generic AI slideshow spam.

## Retention frame (YouTube Shorts)

Swipe-away is decided in ~**2 seconds**. Structure every script as:

| Phase | Window | Job |
| --- | --- | --- |
| **Hook** | 0–3 s (max 5 s) | Question, bold claim, or news in one line. No greetings. |
| **Intrigue Bridge** | ~3–15 s | Why it matters / one unexpected detail — do not dump the answer yet |
| **Climax** | middle | The concrete fact / mechanism / twist |
| **Abrupt Resolution** | last 3–6 s | Payoff + optional loop rhyme with the hook |
| **CTA** | ≤3 s | One short line. No watery outro |

Rules:

- **One thesis** per Short. Cut ~30% of any first draft.
- First on-screen text (caption or tablet) **restates the hook** for muted viewers.
- Start speaking in the first 0.5 s — no silence bed.
- Prefer 35–55 s for news/explainer when every second earns its place; shorter if the idea is thinner.
- Loopable ending when natural (last line echoes the hook question).

### Hook patterns that work

- Direct question: «Знаешь, почему…?»
- Bold claim: «Модель OpenAI сама взломала Hugging Face.»
- Mid-action news: lead with the shocking fact, then rewind.

Forbidden: «Привет», «сегодня поговорим», logo cold-open, slow fades.

## Voice (non-negotiable)

Always from `brand/brandbook.json` → `voice`:

- Provider: ElevenLabs
- Model: **`eleven_v3`**
- Voice: brandbook `voice_id` / HeyGen **NIKITA2**
- Language: **`ru`**

Never invent another voice for the channel.

## Visual authorship

- Every `visuals[]` row needs `segment_ref` (or clear overlap) so the shot illustrates **that** line.
- News/IT: fast cuts, top tablets, icons. Space/science: longer holds on wow frames.
- Avatar on camera **~40–50%** of duration; EXIT the ring for fullscreen payoffs (50–60% without face is OK).
- Layout default for news: avatar lower in the Pulse Ring, content in `position: "top"`.
- **No asset reuse** across slots in one Short.
- Red is an accent (≈12–15% of brand surface), not a carpet. Cyan for UI tablets.

## Research before writing news

Run / use `jobs/<id>/research.json` (5–12 claims with dates and URLs). Do not script from a single headline. Prefer primary sources (vendor blogs, filings) then reputable tech press.

## Pipeline map

| Need | Where |
| --- | --- |
| Hook validation | `shorts_factory.author.scriptcraft` |
| News pack | `shorts_factory.author.research` / CLI `research` |
| Brand voice | `brand/brandbook.json` |
| Ring / MG | skills `neon-ring-animation`, `motion-graphics-style` |
