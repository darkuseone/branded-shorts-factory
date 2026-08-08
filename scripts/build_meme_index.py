#!/usr/bin/env python3
"""Generate assets/memes/index.json with humor roles for the REDSHIFT irony layer.

Run: PYTHONPATH=src python3 scripts/build_meme_index.py
"""

from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMES = ROOT / "assets" / "memes"


def _norm(name: str) -> str:
    """Collapse Cyrillic combining marks so 'й' matches across NFD/NFC forms."""
    decomposed = unicodedata.normalize("NFD", name)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", stripped).casefold()


# Curated Russian sci-pop meta-irony catalog. Filenames are the source of truth;
# this map adds humor role, safe beats, and trim windows for long clips.
CURATED: dict[str, dict] = {
    "a-vy-o-chem-sprosili.mp4": {
        "title": "А вы о чём спросили",
        "humor": "misconception",
        "beats": ["hook_punch", "misconception"],
        "tags": ["вопрос", "не-в-тему", "hook", "irony"],
        "intensity": "soft",
        "max_use": 1.3,
        "note": "после странного/слишком широкого вопроса в хуке",
    },
    "alo-alo-ne-otvlekaemsya.mp4": {
        "title": "Ало не отвлекаемся",
        "humor": "focus",
        "beats": ["context_end"],
        "tags": ["фокус", "внимание", "назад-к-теме"],
        "intensity": "soft",
        "trim_start": 0.4,
        "max_use": 1.4,
        "note": "вернуть зрителя после отступления",
    },
    "andruha-u-nas-trup.mp4": {
        "title": "Андрюха у нас труп",
        "humor": "deadpan_accept",
        "beats": ["absurd_scale", "deadpan_accept"],
        "tags": ["смерть", "конец", "катастрофа", "deadpan"],
        "intensity": "hard",
        "max_use": 1.5,
        "safe_for_science": True,
        "note": "когда объект «уже мёртв» — чёрная дыра, зонд, звезда",
    },
    "cho-kavo-suchara.mp4": {
        "title": "Чо каво",
        "humor": "hook_punch",
        "beats": ["hook_punch"],
        "tags": ["привет", "реакция", "casual"],
        "intensity": "soft",
        "max_use": 1.1,
    },
    "chto-ty-skazal-seichas.mp4": {
        "title": "Что ты сказал сейчас",
        "humor": "doubt",
        "beats": ["misconception", "doubt"],
        "tags": ["переспросить", "wtf", "сомнение"],
        "intensity": "mid",
        "max_use": 1.4,
    },
    "da-chto-ty-chert-poberi.mp4": {
        "title": "Да что ты чёрт побери",
        "humor": "shock",
        "beats": ["absurd_scale", "reveal_twist"],
        "tags": ["шок", "неожиданность"],
        "intensity": "mid",
        "max_use": 1.4,
    },
    "davai-po-novoi.mp4": {
        "title": "Давай по новой",
        "humor": "retry",
        "beats": ["misconception"],
        "tags": ["заново", "ошибка", "пересчёт"],
        "intensity": "soft",
        "max_use": 1.3,
    },
    "estestvenno.mp4": {
        "title": "Естественно",
        "humor": "deadpan_accept",
        "beats": ["deadpan_accept", "absurd_scale"],
        "tags": ["естественно", "deadpan", "ирония", "принято"],
        "intensity": "soft",
        "max_use": 1.2,
        "note": "сухо принять невозможное — фирменный научпоп-приём",
    },
    "eto-dukhovnaua-skrepa.mp4": {
        "title": "Это духовная скрепа",
        "humor": "meta",
        "beats": ["reveal_twist"],
        "tags": ["мета", "традиция", "ирония"],
        "intensity": "soft",
        "max_use": 1.4,
        "safe_for_science": False,
        "note": "осторожно — политический оттенок; лучше не в космос",
    },
    "FEW MOMENTS.mp4": {
        "title": "A few moments later",
        "humor": "time_skip",
        "beats": ["context_end", "core1_to_core2"],
        "tags": ["время", "спустя", "later", "переход"],
        "intensity": "soft",
        "max_use": 1.5,
    },
    "gai-lost-palochku.mp4": {
        "title": "Гай потерял палочку",
        "humor": "fail",
        "beats": ["misconception"],
        "tags": ["фейл", "потерял", "star-wars"],
        "intensity": "mid",
        "trim_start": 8.0,
        "max_use": 1.5,
        "note": "длинный клип — берём только финальный панч",
    },
    "gde-dengi-lebovski.mp4": {
        "title": "Где деньги Лебовски",
        "humor": "demand",
        "beats": ["context_end"],
        "tags": ["деньги", "где", "лебовски"],
        "intensity": "mid",
        "trim_start": 2.5,
        "max_use": 1.4,
        "safe_for_science": False,
    },
    "genialno-eto-shedevr.mp4": {
        "title": "Гениально это шедевр",
        "humor": "praise_irony",
        "beats": ["praise_irony", "reveal_twist"],
        "tags": ["гениально", "шедевр", "сарказм", "praise"],
        "intensity": "mid",
        "trim_start": 3.5,
        "max_use": 1.5,
        "note": "саркастическая похвала после «очевидного» вывода",
    },
    "lyudi-stali-kretiny.mp4": {
        "title": "Люди стали кретины",
        "humor": "judge",
        "beats": ["misconception"],
        "tags": ["люди", "глупость", "судья"],
        "intensity": "hard",
        "trim_start": 6.5,
        "max_use": 1.4,
        "safe_for_science": False,
    },
    "minut-5-10-pyatogo.mp4": {
        "title": "Минут 5–10 пятого",
        "humor": "time_skip",
        "beats": ["context_end"],
        "tags": ["время", "примерно", "оценка"],
        "intensity": "soft",
        "trim_start": 3.5,
        "max_use": 1.4,
    },
    "mish-mne-pohui-tinkov.mp4": {
        "title": "Миш мне похуй",
        "humor": "dismiss",
        "beats": ["deadpan_accept"],
        "tags": ["пофиг", "тинькофф", "dismiss"],
        "intensity": "hard",
        "max_use": 1.3,
        "safe_for_science": False,
    },
    "nahui-etu-gotovku.mp4": {
        "title": "Нахуй эту готовку",
        "humor": "reject",
        "beats": ["misconception"],
        "tags": ["отмена", "не-надо", "reject"],
        "intensity": "hard",
        "trim_start": 5.0,
        "max_use": 1.4,
        "safe_for_science": False,
    },
    "net.mp4": {
        "title": "Нет",
        "humor": "doubt",
        "beats": ["misconception", "doubt", "hook_punch"],
        "tags": ["нет", "отказ", "сомнение", "devito"],
        "intensity": "soft",
        "max_use": 1.2,
        "note": "тихое «нет» на миф или тупой вопрос",
    },
    "NICE.mp4": {
        "title": "Nice",
        "humor": "praise_irony",
        "beats": ["praise_irony", "deadpan_accept"],
        "tags": ["nice", "ок", "ирония"],
        "intensity": "soft",
        "max_use": 1.3,
    },
    "nihuya-tinkov.mp4": {
        "title": "Нихуя Тинькофф",
        "humor": "shock",
        "beats": ["absurd_scale", "reveal_twist"],
        "tags": ["нихуя", "шок", "тинькофф"],
        "intensity": "hard",
        "max_use": 1.2,
        "safe_for_science": False,
    },
    "nihuya-ty-viebal-po-mne.mp4": {
        "title": "Нихуя ты выебал по мне",
        "humor": "shock",
        "beats": ["absurd_scale"],
        "tags": ["шок", "удар", "жёстко"],
        "intensity": "hard",
        "trim_start": 1.5,
        "max_use": 1.3,
        "safe_for_science": False,
    },
    "NOOOOOOOOOOOO.mp4": {
        "title": "NOOOOO",
        "humor": "despair",
        "beats": ["absurd_scale", "reveal_twist"],
        "tags": ["no", "отчаяние", "нет"],
        "intensity": "mid",
        "trim_start": 0.0,
        "max_use": 1.4,
        "note": "длинный рёв — только первые ~1.4с",
    },
    "round-larin.mp4": {
        "title": "Round Ларин",
        "humor": "hook_punch",
        "beats": ["hook_punch"],
        "tags": ["раунд", "короткий", "панч"],
        "intensity": "soft",
        "max_use": 0.8,
    },
    "sho-opyat.mp4": {
        "title": "Шо опять",
        "humor": "retry",
        "beats": ["misconception", "hook_punch"],
        "tags": ["опять", "снова", "усталость"],
        "intensity": "soft",
        "max_use": 1.3,
    },
    "slazz-slazzz.mp4": {
        "title": "Слазь",
        "humor": "reject",
        "beats": ["misconception"],
        "tags": ["слазь", "уйди"],
        "intensity": "mid",
        "trim_start": 3.5,
        "max_use": 1.3,
        "safe_for_science": False,
    },
    "smeh-moment.mp4": {
        "title": "Смех момент",
        "humor": "laugh",
        "beats": ["reveal_twist"],
        "tags": ["смех", "ха", "реакция"],
        "intensity": "soft",
        "trim_start": 1.0,
        "max_use": 1.3,
    },
    "somnenie-okey-tinkov.mp4": {
        "title": "Сомнение окей",
        "humor": "doubt",
        "beats": ["doubt", "misconception"],
        "tags": ["сомнение", "окей", "скепсис"],
        "intensity": "soft",
        "trim_start": 0.5,
        "max_use": 1.4,
    },
    "strashno-ochen.mp4": {
        "title": "Страшно очень",
        "humor": "fear",
        "beats": ["absurd_scale", "deadpan_accept"],
        "tags": ["страшно", "ужас", "страх"],
        "intensity": "mid",
        "trim_start": 3.5,
        "max_use": 1.4,
        "note": "после жуткой цифры — температура, давление, tidал",
    },
    "this-is-russia-zhirinovskij.mp4": {
        "title": "This is Russia",
        "humor": "meta",
        "beats": ["reveal_twist"],
        "tags": ["россия", "жириновский", "this-is"],
        "intensity": "hard",
        "trim_start": 7.0,
        "max_use": 1.5,
        "safe_for_science": False,
    },
    "troe-detei-oba-uchatsya.mp4": {
        "title": "Трое детей оба учатся",
        "humor": "logic_fail",
        "beats": ["misconception", "doubt"],
        "tags": ["логика", "абсурд", "математика"],
        "intensity": "soft",
        "max_use": 1.5,
        "note": "идеально под парадокс / противоречие в объяснении",
    },
    "ty-po-moemu-pereputal.mp4": {
        "title": "Ты по-моему перепутал",
        "humor": "misconception",
        "beats": ["misconception", "doubt"],
        "tags": ["перепутал", "миф", "ошибка"],
        "intensity": "soft",
        "trim_start": 0.8,
        "max_use": 1.5,
        "note": "разнос популярного мифа",
    },
    "v-tennise=mne-nravitsya-tennis.mp4": {
        "title": "В теннисе мне нравится теннис",
        "humor": "tautology",
        "beats": ["deadpan_accept"],
        "tags": ["тавтология", "очевидно", "мета"],
        "intensity": "soft",
        "trim_start": 4.5,
        "max_use": 1.5,
    },
    "vy-kto-takie.mp4": {
        "title": "Вы кто такие",
        "humor": "gatekeep",
        "beats": ["hook_punch"],
        "tags": ["кто-такие", "ворота", "не-ваше"],
        "intensity": "mid",
        "trim_start": 2.5,
        "max_use": 1.4,
    },
    "zalet-uzhe-upal.mp4": {
        "title": "Залёт уже упал",
        "humor": "too_late",
        "beats": ["absurd_scale", "deadpan_accept"],
        "tags": ["поздно", "уже", "упал"],
        "intensity": "soft",
        "max_use": 1.3,
    },
    "zhal-dobryaka.mp4": {
        "title": "Жаль добряка",
        "humor": "pity",
        "beats": ["deadpan_accept"],
        "tags": ["жаль", "жалость", "добряк"],
        "intensity": "soft",
        "max_use": 1.3,
    },
    "Вот это поворот!.mp4": {
        "title": "Вот это поворот",
        "humor": "reveal_twist",
        "beats": ["reveal_twist", "core1_to_core2"],
        "tags": ["поворот", "твист", "неожиданно"],
        "intensity": "soft",
        "max_use": 1.2,
        "note": "стык ядер — лучший твист-мем канала",
    },
    "ДИЧЬ.mp4": {
        "title": "Дичь",
        "humor": "judge",
        "beats": ["absurd_scale", "misconception"],
        "tags": ["дичь", "бред", "странно"],
        "intensity": "mid",
        "trim_start": 1.5,
        "max_use": 1.3,
    },
    "ЛУЧШАЯ РАБОТА В МИРЕ.mp4": {
        "title": "Лучшая работа в мире",
        "humor": "praise_irony",
        "beats": ["praise_irony"],
        "tags": ["работа", "лучшая", "ирония"],
        "intensity": "soft",
        "trim_start": 1.0,
        "max_use": 1.4,
    },
    "ЛЯ ТЫ КРЫСА.mp4": {
        "title": "Ля ты крыса",
        "humor": "accuse",
        "beats": ["reveal_twist"],
        "tags": ["крыса", "обвинение"],
        "intensity": "mid",
        "max_use": 1.2,
        "safe_for_science": False,
    },
    "МАЛЬЧИК ОБИДЕЛСЯ.mp4": {
        "title": "Мальчик обиделся",
        "humor": "pity",
        "beats": ["deadpan_accept"],
        "tags": ["обида", "малыш"],
        "intensity": "soft",
        "max_use": 1.3,
    },
    "Оу, май!.mp4": {
        "title": "Оу май",
        "humor": "shock",
        "beats": ["absurd_scale", "hook_punch", "reveal_twist"],
        "tags": ["oh-my", "шок", "вау"],
        "intensity": "soft",
        "max_use": 1.3,
    },
    "ПАЦАН К УСПЕХУ ШЕЛ.mp4": {
        "title": "Пацан к успеху шёл",
        "humor": "fail",
        "beats": ["absurd_scale", "deadpan_accept"],
        "tags": ["успех", "фейл", "шёл"],
        "intensity": "soft",
        "max_use": 1.5,
        "note": "попытка → катастрофа (зонд, экспедиция)",
    },
    "СИЛЬНОЕ ЗАЯВЛЕНИЕ.mp4": {
        "title": "Сильное заявление",
        "humor": "praise_irony",
        "beats": ["hook_punch", "praise_irony"],
        "tags": ["заявление", "сильно", "тезис"],
        "intensity": "soft",
        "trim_start": 2.0,
        "max_use": 1.4,
        "note": "сразу после жирного тезиса в хуке",
    },
    "ТИТРЫ РОБЕРТ.mp4": {
        "title": "Титры Роберт",
        "humor": "outro_joke",
        "beats": ["context_end"],
        "tags": ["титры", "конец", "роберт"],
        "intensity": "soft",
        "trim_start": 5.5,
        "max_use": 1.5,
        "safe_for_science": False,
    },
    "УПАЛА ЧЕЛЮСТЬ.mp4": {
        "title": "Упала челюсть",
        "humor": "shock",
        "beats": ["absurd_scale", "reveal_twist", "hook_punch"],
        "tags": ["челюсть", "шок", "вау", "цифра"],
        "intensity": "soft",
        "max_use": 1.3,
        "note": "после невозможной цифры — давление, масса, температура",
    },
    "ЧЕГО БЛЯТЬ.mp4": {
        "title": "Чего блять",
        "humor": "shock",
        "beats": ["absurd_scale", "hook_punch", "reveal_twist"],
        "tags": ["wtf", "чего", "шок"],
        "intensity": "hard",
        "max_use": 1.2,
        "safe_for_science": False,
        "note": "жёсткий wtf — только если тон ролика позволяет",
    },
    "ЧЕТКО, МОГЕТЕ.mp4": {
        "title": "Чётко можете",
        "humor": "praise_irony",
        "beats": ["praise_irony"],
        "tags": ["чётко", "можете", "praise"],
        "intensity": "soft",
        "trim_start": 4.5,
        "max_use": 1.4,
    },
    "и-так-сойдет.mp4": {
        "title": "И так сойдёт",
        "humor": "deadpan_accept",
        "beats": ["deadpan_accept"],
        "tags": ["сойдёт", "приближение", "модель", "deadpan"],
        "intensity": "soft",
        "max_use": 1.3,
        "note": "когда модель «достаточно хороша» — сферическая корова vibe",
    },
    # Still memes (global reaction faces) — short holds, no trim needed.
    "IMG_5530.jpeg": {
        "title": "Hide the Pain Harold",
        "humor": "deadpan_accept",
        "beats": ["deadpan_accept", "absurd_scale"],
        "tags": ["harold", "улыбка", "боль", "deadpan"],
        "intensity": "soft",
        "max_use": 1.2,
        "note": "Forced smile while spacetime tears you apart",
    },
    "IMG_5600.gif": {
        "title": "Homelander smile",
        "humor": "forced_composure",
        "beats": ["doubt", "misconception"],
        "tags": ["homelander", "улыбка", "напряг"],
        "intensity": "soft",
        "max_use": 1.2,
    },
}

# Fallback tagging for unnamed stills — soft reaction pool.
STILL_DEFAULTS = {
    "humor": "reaction",
    "beats": ["hook_punch", "deadpan_accept"],
    "tags": ["reaction", "still", "лицо"],
    "intensity": "soft",
    "max_use": 1.1,
    "safe_for_science": True,
}


def probe_duration(path: Path) -> float:
    if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
        return 0.0
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            text=True,
        ).strip()
        return float(out) if out else 0.0
    except (OSError, ValueError, subprocess.CalledProcessError):
        return 0.0


def tags_from_stem(stem: str) -> list[str]:
    parts = [p for p in re.split(r"[^\w]+", stem.lower(), flags=re.UNICODE) if len(p) > 1]
    return parts[:8]


def main() -> None:
    items = []
    for path in sorted(MEMES.iterdir()):
        if path.suffix.lower() not in {".mp4", ".webm", ".gif", ".jpg", ".jpeg", ".png", ".webp"}:
            continue
        if path.name.startswith("."):
            continue
        duration = probe_duration(path)
        curated = CURATED.get(path.name) or CURATED.get(_norm(path.name)) or {}
        if not curated:
            # Combining-character filenames (й as two codepoints) — match NFC keys.
            for key, value in CURATED.items():
                if _norm(key) == _norm(path.name):
                    curated = value
                    break
        if not curated and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            curated = {
                **STILL_DEFAULTS,
                "title": path.stem,
                "tags": tags_from_stem(path.stem) + list(STILL_DEFAULTS["tags"]),
            }
        if not curated:
            curated = {
                "title": path.stem,
                "humor": "reaction",
                "beats": ["context_end"],
                "tags": tags_from_stem(path.stem),
                "intensity": "soft",
                "max_use": min(1.5, duration or 1.2),
            }

        max_use = float(curated.get("max_use") or min(1.5, duration or 1.2) or 1.2)
        if duration and duration > 0:
            max_use = min(max_use, duration)
        entry = {
            "file": path.name,
            "title": curated.get("title") or path.stem,
            "tags": sorted(set(str(t).lower() for t in curated.get("tags", []))),
            "humor": curated.get("humor", "reaction"),
            "beats": list(curated.get("beats") or ["context_end"]),
            "intensity": curated.get("intensity", "soft"),
            "safe_for_science": bool(curated.get("safe_for_science", True)),
            "duration": round(duration, 3),
            "trim_start": float(curated.get("trim_start") or 0.0),
            "max_use": round(max_use, 3),
            "note": curated.get("note", ""),
        }
        items.append(entry)

    payload = {
        "_comment": (
            "REDSHIFT meme bank — humor roles for meta-irony sci-pop. "
            "Regenerate via scripts/build_meme_index.py"
        ),
        "items": items,
    }
    out = MEMES / "index.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(items)} memes → {out}")


if __name__ == "__main__":
    main()
