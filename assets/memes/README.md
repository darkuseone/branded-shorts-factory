# Meme bank — REDSHIFT meta-irony

Русский научпоп с сухой иронией. Мемы — **пунктуация**, не контент:
один короткий удар (≤ ~1.4 с) на ролик, и только когда есть иронический
бит.

## Когда вставляем

Алгоритм (`meme_policy.py`) сам ищет момент:

| Beat | Когда | Пример |
|------|--------|--------|
| `hook_punch` | сразу после вопроса в хуке | вопрос → мем → объяснение |
| `misconception` | после мифа / «кажется» | «это просто стена» → реакция |
| `absurd_scale` | после невозможной цифры | «миллиард атмосфер» |
| `deadpan_accept` | сухое принятие ужаса | «и это естественно» |
| `reveal_twist` | шов core1→core2 | поворот сюжета |
| `context_end` / `core1_to_core2` | мягкий time-skip | FEW MOMENTS LATER |

Жёсткие правила:

1. Медицина — бан. Космос / наука / IT / AI — ок.
2. Частота ~1 из 4 роликов (`brandbook.memes.frequency`).
3. Предпочитаем `safe_for_science` и `intensity: soft|mid`.
4. Длинные клипы режутся: `trim_start` + `max_use` из `index.json`.

## Индекс

`index.json` — теги, `humor`, `beats`, `trim_start`, `max_use`, заметки.
Пересобрать:

```bash
PYTHONPATH=src python3 scripts/build_meme_index.py
```

Файлы **нужно коммитить**: CI клонирует репо и без файла мем не существует.

## Сценарий

Либо явно:

```json
"memes": { "enabled": true, "tags": ["вопрос", "шок"], "max_duration": 1.3 }
```

Либо оставь `memes` пустым — brandbook с `"enabled": true` включит сам
и вставит слот на лучший иронический бит.
