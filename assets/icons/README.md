# Icon bank

Company and app marks used by infographic cards (`redshift.render.icons`).

A card that says «OpenAI → Hugging Face» reads as a diagram when both names
carry their real mark, and as a slide when they carry a grey monogram. This
folder is what makes the mark free: everything downloaded once lands here and
is committed, so the second video that mentions a company pays nothing and
needs no network at all.

## Naming

One file per entity, named by its slug:

    assets/icons/openai.svg
    assets/icons/huggingface.png
    assets/icons/nvidia.svg

The slug comes from `icons.slugify()`: casefolded, non-word runs collapsed to
`-`. Cyrillic works. Aliases for names that do not slugify to how a stock
library indexes them (`hugging face` → `huggingface`, `claude` → `anthropic`)
live in `icons.ALIASES`.

Accepted: `.svg`, `.png`, `.webp`, `.jpg`. Keep each file under 512 KB — these
are marks, not photographs, and they are inlined into the card as base64.

## Where they come from

0. The marks committed here came from [simple-icons](https://github.com/simple-icons/simple-icons),
   whose icon set is released under CC0 1.0. They are recoloured to the
   brandbook's ink (`#F1F5F9`) and otherwise unaltered. To add more:

       npm pack simple-icons && tar xzf simple-icons-*.tgz
       cp package/icons/<name>.svg assets/icons/

   A mark the current release has dropped is usually still in an older one.
1. This folder. Free, offline, identical on every runner.
2. The Magnific subscription library — free, but 100 downloads a day on the
   current plan, tracked in `.state/magnific-downloads.json`. Anything fetched
   is written here, so commit it after a run that filled a gap.
3. A drawn monogram, when neither of the above produced anything.

A missing icon is never an error. It costs the card some polish, nothing more.

## Trademarks

These are third-party marks used nominatively — to refer to the company being
discussed. Keep them unaltered in shape and colour, and do not use them in a
way that suggests the channel is endorsed by or affiliated with the owner.
