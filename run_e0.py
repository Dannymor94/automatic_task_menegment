"""
E0 — Замер: прогон транскриптов через LLM, парсинг задач, логирование.
Читает raw/*.md и raw/*.txt, пишет out/<имя>.json и logs/e0.jsonl.

LLM-логика вынесена в decompose.py (единственная точка LLM). Здесь — только
прогон по файлам и структурный лог для замера двух чисел go/no-go.
"""

import json
import logging
import time
from pathlib import Path

from decompose import (
    build_client,
    count_low_confidence,
    decompose_raw,
    load_system_prompt,
)

BASE_DIR = Path(__file__).parent
RAW_DIR = BASE_DIR / "raw"
OUT_DIR = BASE_DIR / "out"
LOGS_DIR = BASE_DIR / "logs"
LOG_FILE = LOGS_DIR / "e0.jsonl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def write_jsonl(record: dict) -> None:
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def process_file(path: Path, client, model: str, system: str) -> None:
    out_path = OUT_DIR / f"{path.stem}.json"
    log.info("Processing %s", path.name)

    transcript = path.read_text(encoding="utf-8")
    t0 = time.monotonic()
    error_msg = None
    tasks: list[dict] = []

    try:
        tasks, _ = decompose_raw(transcript, client, model, system)
    except Exception as exc:  # noqa: BLE001
        error_msg = str(exc)
        log.error("Error processing %s: %s", path.name, error_msg)

    elapsed = time.monotonic() - t0
    task_count = len(tasks)
    low_conf = count_low_confidence(tasks)
    is_empty = task_count == 0

    out_path.write_text(
        json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    write_jsonl(
        {
            "file": path.name,
            "task_count": task_count,
            "low_confidence_count": low_conf,
            "is_empty": is_empty,
            "elapsed_seconds": round(elapsed, 2),
            "model": model,
            "error": error_msg,
        }
    )

    status = "OK" if error_msg is None else "ERROR"
    log.info(
        "%s [%s] tasks=%d low_conf=%d empty=%s elapsed=%.1fs",
        path.name,
        status,
        task_count,
        low_conf,
        is_empty,
        elapsed,
    )


def main() -> None:
    client, model = build_client()
    system = load_system_prompt()

    files = sorted(
        p for p in RAW_DIR.iterdir() if p.suffix in {".md", ".txt"} and p.is_file()
    )
    if not files:
        log.warning("No .md/.txt files found in %s", RAW_DIR)
        return

    log.info("Found %d file(s) in %s, model=%s", len(files), RAW_DIR, model)
    for path in files:
        process_file(path, client, model, system)
    log.info("Done. Results in %s, logs in %s", OUT_DIR, LOG_FILE)


if __name__ == "__main__":
    main()
