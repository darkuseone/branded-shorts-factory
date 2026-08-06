# jobs/

Сюда кладутся готовые сценарии. Двухфазный пайплайн:

1. Пуш `jobs/<id>.json` → workflow **Prepare Short**
   (озвучка ElevenLabs + тайминги слов + поиск/QA).
2. Скачай артефакт `prepare-<id>` (`voice.mp3` + `words.json`).
3. В чате агент загружает голос в HeyGen MCP и коммитит
   `jobs/<id>/avatar.mp4`.
4. Пуш аватара → workflow **Render Short** → готовый MP4 в артефактах.

```
jobs/
  2026-08-venus-hell.json
  2026-08-venus-hell/
    avatar.mp4          # после HeyGen, коммитится в репо
```

Полный однофазный прогон (если есть `HEYGEN_API_KEY`) — workflow **Build Short**
вручную через `workflow_dispatch`.

Перед пушем полезно прогнать локально:

```bash
PYTHONPATH=src python -m shorts_factory validate jobs/2026-08-venus-hell.json
PYTHONPATH=src python -m shorts_factory prepare  jobs/2026-08-venus-hell.json
```

Формат — `docs/JSON_SPEC.md`. Готовый пример — `examples/venus-hell.json`.

`id` внутри файла определяет имя выходного `.mp4` и папку аватара.
