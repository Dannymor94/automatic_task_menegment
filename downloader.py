"""
Загрузка файла по ссылке (E4-доработка). Три случая:
  1) прямая http(s)-ссылка на аудио/текст → скачиваем как есть;
  2) Яндекс.Диск (disk.yandex.*, yadi.sk) → публичная ссылка → downloader API → прямой href;
  3) Google Drive (drive.google.com / docs.google.com) → export/uc-ссылка (+ confirm-token).

Скачанный файл кладём в uploads/ и отдаём в тот же пайплайн, что и локальный.
Понятные ошибки для приватных/недоступных ссылок.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

BASE_DIR = Path(__file__).parent
UPLOADS = BASE_DIR / "uploads"
UPLOADS.mkdir(exist_ok=True)

YANDEX_PUBLIC_API = "https://cloud-api.yandex.net/v1/disk/public/resources/download"
MAX_BYTES = 200 * 1024 * 1024  # подстраховка

# Content-Type → расширение (для определения, как пайплайн прочитает файл).
_CTYPE_EXT = {
    "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/webm": ".webm",
    "video/mp4": ".mp4", "text/plain": ".txt", "text/markdown": ".md",
}
_AUDIO_TEXT_EXT = {".mp3", ".mp4", ".m4a", ".wav", ".webm", ".txt", ".md"}


class DownloadError(Exception):
    """Понятная ошибка загрузки (приватная/битая ссылка и т.п.)."""


def _is_yandex(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "disk.yandex" in host or "yadi.sk" in host


def _is_gdrive(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "drive.google.com" in host or "docs.google.com" in host


def _resolve_yandex(url: str) -> str:
    """Публичная ссылка Я.Диска → прямой href для скачивания."""
    try:
        r = requests.get(YANDEX_PUBLIC_API, params={"public_key": url}, timeout=30)
    except requests.RequestException as e:
        raise DownloadError(f"Яндекс.Диск недоступен: {e}") from e
    if r.status_code == 404:
        raise DownloadError("Файл на Яндекс.Диске не найден или ссылка не публичная.")
    if r.status_code in (401, 403):
        raise DownloadError("Ссылка на Яндекс.Диск приватная — откройте публичный доступ.")
    if not r.ok:
        raise DownloadError(f"Яндекс.Диск вернул ошибку {r.status_code}.")
    href = r.json().get("href")
    if not href:
        raise DownloadError("Яндекс.Диск не дал прямую ссылку на файл.")
    return href


def _gdrive_id(url: str) -> str:
    for pat in (r"/file/d/([\w-]+)", r"[?&]id=([\w-]+)", r"/d/([\w-]+)"):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    raise DownloadError("Не удалось извлечь ID файла Google Drive из ссылки.")


def _ext_for(filename: str | None, ctype: str | None, fallback_url: str) -> str:
    if filename:
        ext = Path(filename).suffix.lower()
        if ext in _AUDIO_TEXT_EXT:
            return ext
    if ctype:
        base = ctype.split(";")[0].strip().lower()
        if base in _CTYPE_EXT:
            return _CTYPE_EXT[base]
        if base.startswith("text/"):
            return ".txt"
    url_ext = Path(urlparse(fallback_url).path).suffix.lower()
    if url_ext in _AUDIO_TEXT_EXT:
        return url_ext
    return ".txt"  # дефолт — считаем текстом-транскриптом


def _filename_from_headers(resp: requests.Response) -> str | None:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    return m.group(1) if m else None


def _stream_to_file(resp: requests.Response, dest: Path) -> None:
    total = 0
    with dest.open("wb") as fh:
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_BYTES:
                fh.close()
                dest.unlink(missing_ok=True)
                raise DownloadError("Файл слишком большой (> 200 МБ).")
            fh.write(chunk)


def download(url: str, meeting_id: int) -> Path:
    """Скачать файл по ссылке. Возвращает путь к локальному файлу в uploads/."""
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise DownloadError("Нужна http(s)-ссылка.")

    session = requests.Session()
    download_url = url
    suggested_name = None

    if _is_yandex(url):
        download_url = _resolve_yandex(url)
    elif _is_gdrive(url):
        download_url = f"https://drive.google.com/uc?export=download&id={_gdrive_id(url)}"

    try:
        resp = session.get(download_url, stream=True, timeout=60)
    except requests.RequestException as e:
        raise DownloadError(f"Не удалось скачать по ссылке: {e}") from e

    # Google Drive: страница подтверждения для больших файлов.
    if _is_gdrive(url) and "text/html" in resp.headers.get("content-type", ""):
        token = None
        for k, v in session.cookies.items():
            if k.startswith("download_warning"):
                token = v
        if not token:
            m = re.search(r"confirm=([\w-]+)", resp.text)
            token = m.group(1) if m else None
        if token:
            resp = session.get(download_url + f"&confirm={token}", stream=True, timeout=60)

    if resp.status_code in (401, 403):
        raise DownloadError("Ссылка приватная или требует входа — откройте публичный доступ.")
    if resp.status_code == 404:
        raise DownloadError("Файл по ссылке не найден (404).")
    if not resp.ok:
        raise DownloadError(f"Сервер вернул ошибку {resp.status_code}.")
    if "text/html" in resp.headers.get("content-type", "") and not _is_yandex(url):
        raise DownloadError("По ссылке отдаётся HTML-страница, а не файл. Проверьте, что это прямая ссылка на файл.")

    suggested_name = _filename_from_headers(resp)
    ext = _ext_for(suggested_name, resp.headers.get("content-type"), url)
    dest = UPLOADS / f"meeting_{meeting_id}{ext}"
    _stream_to_file(resp, dest)
    return dest
