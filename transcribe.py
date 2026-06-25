"""
Шаг 1 пайплайна — транскрипция: аудио → текст (Groq Whisper API).

Принимает путь к аудиофайлу, возвращает строку транскрипта.
Форматы: mp3, mp4, m4a, wav, webm.
Файлы > 25 МБ режутся на части по длительности (пропорционально размеру),
каждая часть транскрибируется отдельно, результат склеивается.

Доступ — через OpenAI-совместимый клиент (тот же, что у LLM), но отдельная
конфигурация WHISPER_BASE_URL / WHISPER_MODEL, ключ — общий LLM_API_KEY.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

log = logging.getLogger(__name__)

SUPPORTED_FORMATS = {".mp3", ".mp4", ".m4a", ".wav", ".webm"}
MAX_SIZE_BYTES = 25 * 1024 * 1024  # 25 МБ — лимит Groq на один файл
# Целимся в 90% лимита, чтобы не упереться в границу из-за оверхеда контейнера.
CHUNK_TARGET_BYTES = int(MAX_SIZE_BYTES * 0.9)


def build_whisper_client() -> tuple[OpenAI, str]:
    base_url = os.environ.get("WHISPER_BASE_URL", "https://api.groq.com/openai/v1")
    # Whisper на Groq → используем ключ Groq (fallback на старый LLM_API_KEY).
    api_key = os.environ.get("GROQ_API_KEY") or os.environ["LLM_API_KEY"]
    model = os.environ.get("WHISPER_MODEL", "whisper-large-v3")
    return OpenAI(base_url=base_url, api_key=api_key), model


def _check_format(path: Path) -> None:
    if path.suffix.lower() not in SUPPORTED_FORMATS:
        raise ValueError(
            f"Неподдерживаемый формат {path.suffix!r}. "
            f"Допустимы: {', '.join(sorted(SUPPORTED_FORMATS))}"
        )


def _transcribe_one(client: OpenAI, model: str, path: Path) -> str:
    """Транскрибировать один файл, гарантированно <= лимита."""
    with path.open("rb") as fh:
        result = client.audio.transcriptions.create(
            model=model,
            file=fh,
            response_format="text",
        )
    # response_format="text" → возвращается строка; на всякий случай страхуемся.
    return result if isinstance(result, str) else getattr(result, "text", str(result))


def _split_audio(path: Path) -> list[Path]:
    """Разрезать большой аудиофайл на части < лимита. Требует pydub + ffmpeg."""
    try:
        from pydub import AudioSegment
    except ImportError as exc:  # noqa: TRY003
        raise RuntimeError(
            "Файл больше 25 МБ — нужна нарезка. Установи pydub и ffmpeg "
            "(pip install pydub; brew install ffmpeg)."
        ) from exc

    size = path.stat().st_size
    audio = AudioSegment.from_file(path)
    duration_ms = len(audio)

    # Сколько частей нужно, чтобы каждая влезла в целевой размер.
    n_chunks = max(1, -(-size // CHUNK_TARGET_BYTES))  # ceil
    chunk_ms = -(-duration_ms // n_chunks)  # ceil, равные по длительности части

    export_fmt = "mp3"
    parts: list[Path] = []
    for i in range(n_chunks):
        seg = audio[i * chunk_ms : (i + 1) * chunk_ms]
        if len(seg) == 0:
            continue
        part_path = path.with_name(f"{path.stem}.part{i}.{export_fmt}")
        seg.export(part_path, format=export_fmt)
        parts.append(part_path)
    log.info("Split %s (%.1f МБ) into %d part(s)", path.name, size / 1e6, len(parts))
    return parts


def transcribe(audio_path: str | Path) -> str:
    """Аудио → транскрипт. Большие файлы режутся и склеиваются автоматически."""
    path = Path(audio_path)
    if not path.exists():
        raise FileNotFoundError(path)
    _check_format(path)

    client, model = build_whisper_client()

    if path.stat().st_size <= MAX_SIZE_BYTES:
        log.info("Transcribing %s", path.name)
        return _transcribe_one(client, model, path).strip()

    parts = _split_audio(path)
    texts: list[str] = []
    try:
        for part in parts:
            texts.append(_transcribe_one(client, model, part).strip())
    finally:
        for part in parts:
            part.unlink(missing_ok=True)  # подчищаем временные куски
    return "\n".join(t for t in texts if t)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) != 2:
        print("Usage: python transcribe.py <audio_file>")
        raise SystemExit(1)
    print(transcribe(sys.argv[1]))
