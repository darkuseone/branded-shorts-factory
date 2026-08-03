# Meme bank

Drop 20–40 memes here (`.gif`, `.mp4`, `.webm`, `.png`, `.jpg`, `.webp`).

Memes are inserted **only** when the scenario asks for them — never as a
fallback, never automatically. Two conditions must both hold:

1. `"memes": { "enabled": true, "tags": [...] }` in the scenario, and
2. a visual slot of `"type": "meme"` whose keywords match a file.

Matching uses tags, falling back to words in the filename, so
`shock-pikachu.gif` is already findable by `shock` or `pikachu`.

## Optional `index.json`

```json
{
  "items": [
    { "file": "shock-pikachu.gif", "title": "Surprised Pikachu", "tags": ["shock", "surprise", "reaction"] }
  ]
}
```

Keep meme slots short (`memes.max_duration`, default 1.5s) — they are punctuation,
not content.

Files are **not** committed to git — see `.gitignore`.
