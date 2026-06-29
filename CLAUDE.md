# CLAUDE.md — гайд для Claude Code

Dev-инструкция для работы над репозиторием. **Это НЕ системный промпт продукта.**
Рантайм-промпт декомпозиции живёт в `prompts/decompose_en.md` (дефолт) и `prompts/decompose.md`;
полный архив исходных правил — в `docs/SYSTEM_PROMPT_FULL.md`. Поведение модели меняется правкой
`prompts/*.md`, а не этого файла.

## Что это за проект

«Созвон → Задачи»: детерминированный конвейер превращает запись созвона (или заметку SimpleNote)
в черновики задач для YouGile. Менеджер ревьюит каждую задачу и одобряет запись в трекер.
Подробности продукта и интерфейса — в `README.md`.

## Команды

```bash
# Зависимости и ключи
pip3 install -r requirements.txt
cp .env.example .env                 # LLM_API_KEY/BASE_URL/MODEL, опц. SimpleNote

# Тесты (детерминированные — офлайн; интеграционный — SKIP без LLM_API_KEY)
python3 test_pipeline.py

# E0 — замер качества декомпозиции на raw/*.txt|*.md → out/, logs/e0.jsonl
python3 run_e0.py

# БД и миграции (локально SQLite по умолчанию; прод — Postgres)
docker compose up -d                 # Postgres для прод-режима
alembic upgrade head                 # применить миграции

# Справочники из YouGile
python3 sync_yougile.py list-users
python3 sync_yougile.py list-projects

# Веб (FastAPI отдаёт собранную статику из web/dist)
cd web && npm install && npm run build && cd ..
python3 -m uvicorn api:app --host 127.0.0.1 --port 8077   # http://127.0.0.1:8077
cd web && npm run dev                 # dev-режим фронта с прокси на бэкенд
```

## Архитектура

Детерминированный код обрамляет **единственный** LLM-вызов (`decompose.py`): код готовит чистый
вход и проверяет выход, модель отвечает только за языковое суждение. Пайплайн двухфазный —
ревью менеджером разрывает его между фазой 1 (авто, до `awaiting_review`) и фазой 2 (запись в
YouGile по явному одобрению).

```
transcribe.py        аудио/транскрипт → текст (Whisper)
simplenote_source.py второй вход: заметки SimpleNote (структурные → быстрый путь без LLM)
chunker.py           нарезка транскрипта по границам реплик
anchors.py           regex-парсинг голосовых якорей (ASR-устойчивый)
decompose.py         ← ЕДИНСТВЕННАЯ точка LLM; грузит prompts/decompose_en.md
providers.py         пул LLM-провайдеров: failover + round-robin
dedup.py             дедуп задач со стыков чанков (без векторов)
people_lookup.py     имя → YouGile user ID по справочнику
validate.py          проставляет validator_flags (подсветка рисков в UI)
orchestrator.py      дирижёр пайплайна, состояние созвона (meetings), фазы 1/2
yougile_writer.py    идемпотентная запись задач + стикеры в YouGile
sync_yougile.py      наполнение справочников из YouGile API
db.py / repo.py      модели и доступ (projects, people, meetings, task_writes)
api.py               FastAPI: /api/upload, /status, /tasks, /approve, /people, /simplenote/*
web/                 React + Vite: загрузка → обработка → ревью → запись
prompts/             рантайм-промпты декомпозиции (ru/en, A/B-варианты)
migrations/          Alembic
run_e0.py            E0-замер качества
test_pipeline.py     тесты
```

## Конвенции

- **LLM только в `decompose.py`.** Остальной пайплайн детерминирован — не добавляй вызовы модели
  в другие модули.
- **Grounding.** Каждая задача и поле опираются на фрагмент-источник (`source`). Нет данных →
  `null`, не выдумываем (сроки, исполнителей, чек-листы, критерии). Полные правила —
  `docs/SYSTEM_PROMPT_FULL.md`; рабочая версия — `prompts/*.md`.
- **Риски — детерминированными флагами валидатора** (`due_invented`, `assignee_unmatched`,
  `controller_unmatched`, `no_grounding`), не самооценкой модели.
- **Идемпотентность записи** в YouGile: маркер `task_writes` + дедуп по названию в колонке.
- **Провайдер LLM за конфигом** (`LLM_BASE_URL` + `LLM_MODEL` + `LLM_API_KEY`): тест по API →
  прод локально без смены кода.
- БД: код работает на SQLite локально и на Postgres в проде через один SQLAlchemy-слой.

## Скиллы проекта

В `skills/` лежат справочные SKILL.md: `decompose-prompt`, `pipeline-conventions`,
`yougile-integration` — читай их перед правкой соответствующих частей.
