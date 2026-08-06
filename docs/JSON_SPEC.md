# Формат сценария

Единственный вход пайплайна. Машинная схема — `schema/short.schema.json`,
рабочий пример — `examples/venus-hell.json`. Истина в последней инстанции —
`src/shorts_factory/spec.py`: там же живут кросс-проверки таймингов, которых
JSON Schema выразить не может.

Проверить файл:

```bash
PYTHONPATH=src python -m shorts_factory validate jobs/my-short.json
```

Валидатор выводит **все** проблемы сразу, каждая адресована путём в JSON.
`ERROR` останавливает сборку, `WARNING` — нет, но читать стоит.

---

## Верхний уровень

| Поле | Тип | Обяз. | Смысл |
| --- | --- | --- | --- |
| `id` | string | да | слаг: имя файла на выходе и имя отчёта |
| `title` | string | да | заголовок, он же текст аутро-карточки |
| `topic` | string | нет | тема одной строкой (нужна QA как контекст) |
| `rubric` | string | нет | рубрика канала: космос / IT / технологии / AI / наука — музыка и мемы |
| `language` | string | нет | `ru` по умолчанию |
| `duration_target` | number | да | 5–180 с. Больше 60 — предупреждение |
| `hook` | object \| string | нет | первый кадр-крючок |
| `script` | array \| string | да | закадровый текст по сегментам |
| `voice_settings` | object | нет | ElevenLabs v3 |
| `avatar` | object | нет | HeyGen Avatar 5 / `provider: external` |
| `ring` | object | нет | Pulse Ring overrides |
| `sfx.policy` | object | нет | потолок плотности звукового дизайна |
| `visuals` | array | да | визуальные слоты (минимум один) |
| `music` | object | нет | трек из `assets/music/` (пусто → по `rubric`) |
| `audio_fx` | array | нет | звуковой дизайн; пусто → предложим сами |
| `captions` | object | нет | субтитры |
| `brand_elements` | object | нет | бренд для конкретного ролика |
| `cta` | object \| string | нет | призыв к действию |
| `memes` | object | нет | разрешение на мемы |
| `constraints` | object | нет | бюджеты и строгость QA |

---

## `script[]` и `hook`

```json
{
  "id": "s2",
  "text": "Но эти облака — из серной кислоты.",
  "start": 8.2,
  "duration": 5.5,
  "emphasis": "high",
  "on_camera": false,
  "pause_after": 0.2
}
```

- `start` / `duration` — секунды на общем таймлайне. Если не указать
  `duration`, он оценивается по длине текста.
- `hook` может быть просто строкой — тогда тайминг подберётся сам, а тело
  скрипта сдвинется, чтобы не наехать на крючок.
- `script` может быть одной строкой: она разобьётся по предложениям (с
  предупреждением — авторская нарезка всегда лучше).

Что проверяется дополнительно:

- закадровый текст не должен вылезать за `duration_target`;
- плотность речи — 7.5–26 символов в секунду (метрика по буквам, а не по
  словам: русские слова длиннее английских);
- предложения длиннее 14 слов — предупреждение, в Shorts они не читаются.

---

## `visuals[]`

```json
{
  "id": "v3",
  "type": "motion_graphics",
  "query": "sulfuric acid cloud layers cross section",
  "keywords": ["atmosphere layers animation", "chemical cloud diagram"],
  "start": 8.2,
  "duration": 5.5,
  "position": "fullscreen",
  "motion": "parallax",
  "priority": "high",
  "segment_ref": "s2",
  "must_include": ["clouds"],
  "must_avoid": ["cartoon", "watermark"],
  "allow_magnific": true,
  "quality_floor": 0.7
}
```

| Поле | Что делает |
| --- | --- |
| `type` | `footage`, `image`, `motion_graphics`, `infographic`, `meme`, `screen_record`, `generated`. Определяет и тип медиа, и модификаторы поиска |
| `query` | основной запрос. **Пишите по-английски** — стоковые API англоязычные |
| `keywords` | 2–4 синонима; ищутся параллельно с основным, а не «если не нашлось» |
| `position` | `fullscreen`, `top`, `bottom`, `center`, `pip`, `left`, `right`, `background` |
| `motion` | `kenburns`, `parallax`, `zoom_in`, `zoom_out`, `pan_left`, `pan_right`, `none` |
| `priority` | `high` / `critical` — hero-кадр: эскалирует раньше и может тратить резерв токенов |
| `segment_ref` | к какому сегменту относится кадр; это и есть контекст для обеих проверок QA |
| `must_include` / `must_avoid` | жёсткие требования: влияют на ранжирование, на уровень 1 и на промпт зрению |
| `allow_magnific` | `false` — слот навсегда остаётся на бесплатных стоках |
| `quality_floor` | 0–1: поднять планку приёмки именно для этого слота |
| `source_hint` | ограничить слот одним источником (`nasa`, `wikimedia`, …) |

Русский `query` не ошибка: он переводится встроенным глоссарием. Но если слова
в глоссаре нет — будет предупреждение, и лучше дописать английский синоним в
`keywords`.

---

## `voice_settings`

```json
{
  "model": "eleven_v3",
  "voice_id": "…",
  "stability": 47,
  "similarity_boost": 0.82,
  "style": 0.38,
  "speed": 1.04
}
```

`stability` пишется в шкале 0–100 (значение 0–1 тоже принимается и
нормализуется). Рабочее окно для Shorts — **40–55**: ниже голос «плывёт», выше
становится монотонным. Выход за окно — предупреждение, не ошибка.

## `avatar`

`segments` перечисляет id сегментов, где аватар в кадре. Пусто — аватар идёт
через весь ролик. Аватар озвучивается нашей же дорожкой ElevenLabs (она
загружается в HeyGen как аудио-ассет); если загрузка недоступна — HeyGen
получает текст и `voice_id`.

## `audio_fx[]`

```json
{ "type": "whoosh", "at": 8.1, "intensity": 0.55, "duration": 0.7 }
```

Типы: `whoosh`, `impact`, `riser`, `swoosh`, `click`, `pop`, `glitch`,
`sub_drop`, `transition`, `ui`. Пустой массив → набор предлагается
автоматически: удар под крючок, переход на каждой склейке, «поп» под CTA.

Откуда берётся звук: сначала ищется файл в `assets/sfx/` по типу (с синонимами
и чередованием между одинаково подходящими файлами), и только если там ничего
нет — генерируется через ElevenLabs. `prompt` служит двум целям: уточняет поиск
по банку и задаёт описание для генерации.

## `music`

`track` — имя файла из `assets/music/` без расширения (хватит и префикса).
`ducking: true` включает настоящий сайдчейн-компрессор в FFmpeg-миксе.

## `memes`

Мем вставляется только когда совпало всё: `memes.enabled: true`, есть слот
`"type": "meme"`, и его ключи совпали с тегом файла из `assets/memes/`. Иначе
слот честно отмечается как незаполненный — случайный мем не подставляется.

## `constraints`

| Поле | По умолчанию | Смысл |
| --- | --- | --- |
| `magnific_token_budget` | из env (40) | потолок токенов на ролик |
| `allow_generative` | `true` | разрешить генерацию вообще |
| `require_vision_qa` | `true` | без Grok Vision слоты уходят на ручную проверку |
| `min_visual_score` | 0.62 | ниже — эскалация |
| `manual_review_ok` | `true` | `false` — сомнительный слот отбрасывается, а не помечается |

---

## Чек-лист перед сборкой

- [ ] `validate` без ошибок и без неожиданных предупреждений
- [ ] визуалы покрывают ≥ 85% таймлайна (иначе будет фирменная заливка)
- [ ] у каждого `visuals[]` есть 2–4 английских `keywords`
- [ ] hero-кадры (крючок, кульминация) помечены `priority`
- [ ] `voice_id` и `avatar_id` подставлены настоящие
- [ ] трек из `music.track` действительно лежит в `assets/music/`
