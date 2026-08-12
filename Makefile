# REDSHIFT Shorts Factory. Targets follow ТЗ §20.2 and §23.2.
# Anything not yet implemented says so and exits non-zero, rather than
# pretending to succeed.

PY ?= python3
export PYTHONPATH := src

RUN ?=
TOPIC ?=
DRY ?= false
FROM ?=
FORCE_REFILL ?= false
DAYS ?= 180

.PHONY: help test lint fmt check shotlist critic produce resume gc clean

help:
	@echo "make test              — run the suite (no network, no tokens)"
	@echo "make lint / fmt        — ruff check / ruff format"
	@echo "make check             — lint + format check + tests, what CI runs"
	@echo "make shotlist SCRIPT=… — build a shotlist from a script.json and check its rhythm"
	@echo "make produce TOPIC=… DRY=true — full pipeline (stages still landing)"
	@echo "make resume RUN=… [FROM=s05] [FORCE_REFILL=true]"
	@echo "make gc DAYS=180       — expire query/llm caches; objects are never touched"

# -- development -------------------------------------------------------------

test:
	$(PY) -m pytest

lint:
	ruff check src tests

fmt:
	ruff format src tests

check: lint
	ruff format --check src tests
	$(PY) -m pytest

# -- pipeline stages ---------------------------------------------------------

SCRIPT ?= tests/fixtures/script_jwst.json

## Stage 03 on its own: every stage must be runnable alone (§0.2).
shotlist:
	@$(PY) -c "from pathlib import Path; \
from redshift.core.config import load_config; \
from redshift.core.schemas import Script; \
from redshift.pipeline.s03_shotlist import build_shotlist, check_rhythm; \
cfg = load_config(); \
sl = build_shotlist(Script.load(Path('$(SCRIPT)')), cfg); \
print(f'{len(sl.shots)} shots, {sl.total_duration_s:.2f}s, median {sl.median_shot_s():.2f}s, presenter {sl.presenter_share():.0%}'); \
problems = check_rhythm(sl, cfg); \
print('rhythm:', '; '.join(problems) if problems else 'clean'); \
raise SystemExit(1 if problems else 0)"

critic:
	@echo "s05 needs candidates.json from s04, which is not landed yet."
	@echo "The L0 filter and the contact-sheet builder are done and unit-tested:"
	@echo "  $(PY) -m pytest tests/test_critic_l0.py"
	@exit 1

produce:
	@echo "The orchestrator (s00-s02, s04, s06-s13) is not landed yet."
	@echo "Done and runnable today: make shotlist, make test."
	@echo "See docs/MIGRATION_STATUS.md for what is next."
	@exit 1

resume:
	@test -n "$(RUN)" || (echo "usage: make resume RUN=<run_id>" && exit 2)
	@echo "resume needs the orchestrator; see docs/MIGRATION_STATUS.md"
	@exit 1

# -- housekeeping ------------------------------------------------------------

## Only query/llm caches expire. cache/objects is the main saving and is
## removed by hand only (§20.4).
gc:
	@$(PY) -c "from redshift.core.cache import Cache; \
from redshift.core.config import load_config; \
cfg = load_config(); \
cache = Cache(cfg.cache_dir, \
  queries_ttl_days=int(cfg.budget('cache.queries_ttl_days')), \
  llm_ttl_days=int(cfg.budget('cache.llm_ttl_days'))); \
print(f'expired {cache.purge_expired()} entries; objects untouched')"

clean:
	rm -rf build/ .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
