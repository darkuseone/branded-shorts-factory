# jobs/

Сюда кладутся готовые сценарии. Пуш любого `.json` в эту папку запускает
workflow **Build Short** — ролик и отчёт появятся в артефактах запуска.

```
jobs/
  2026-08-venus-hell.json
  2026-08-mars-water.json
```

Перед пушем полезно прогнать локально:

```bash
PYTHONPATH=src python -m shorts_factory validate jobs/2026-08-venus-hell.json
```

Формат — `docs/JSON_SPEC.md`. Готовый пример — `examples/venus-hell.json`.

`id` внутри файла определяет имя выходного `.mp4`, поэтому пусть он будет
уникальным (дата в имени файла и в `id` — самый простой способ).
