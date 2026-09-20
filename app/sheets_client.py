"""Прямые вызовы Google Sheets/Calendar API v4/v3 из card-service — без
n8n посередине (2026-09-20, группа 3). Тот же принцип REST+OAuth-refresh,
что уже использует `sheets_to_pg_mirror.js` (Node, вне card-service) —
здесь то же самое на httpx, чтобы card-service мог читать/писать Sheets
сам, когда таблица недоступна как Postgres-зеркало 1:1 (MicroClimate нужен
построчно за конкретные часы сна — дневной агрегат health.microclimate
для этого не годится; Metric_Config/Digest_Log/Anomalies_Log — низкочастотные
чтения/записи, заводить для них отдельный Postgres-путь не по бюджету
сложности).

Два разных OAuth-credential (оба Google, но разный refresh_token —
consent давался раздельно на Sheets и на Calendar):
- CARD_GOOGLE_SHEETS_REFRESH_TOKEN — тот же клиент/сервисный аккаунт, что
  `sheets_to_pg_mirror.js` (credential LLwuPzJuom1ek7BZ в n8n).
- CARD_GOOGLE_CALENDAR_REFRESH_TOKEN — credential PqacoYjKhCXLSGf8, отдельно
  авторизован Владом на перенос 2026-09-20 (календарь — более
  чувствительные данные, чем ячейки таблицы, не считал это автоматически
  покрытым прошлым разрешением на Sheets).
CARD_GOOGLE_CLIENT_ID/SECRET общие для обоих (один OAuth-клиент в Google
Cloud Console, независимо от того, на что именно давался consent)."""
import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

HEALTH_DB_SHEET_ID = "1M8focgZBHCbhLEQb4GoyxTYxedA-FcdjQ_XakX5SG2w"

_token_cache: dict[str, tuple[str, float]] = {}


def _get_access_token(kind: str) -> str:
    """kind: 'sheets' | 'calendar' — какой refresh_token использовать.
    Кэш в памяти процесса на время жизни токена (обычно 3600с), с запасом
    в 60с до истечения."""
    cached = _token_cache.get(kind)
    if cached and cached[1] > time.time() + 60:
        return cached[0]

    client_id = os.environ.get("CARD_GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("CARD_GOOGLE_CLIENT_SECRET")
    refresh_token = os.environ.get(f"CARD_GOOGLE_{kind.upper()}_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        raise RuntimeError(f"sheets_client: нет credentials для kind={kind!r} (проверь env)")

    resp = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={"client_id": client_id, "client_secret": client_secret,
              "refresh_token": refresh_token, "grant_type": "refresh_token"},
        timeout=15.0,
    )
    resp.raise_for_status()
    j = resp.json()
    token = j["access_token"]
    _token_cache[kind] = (token, time.time() + int(j.get("expires_in", 3600)))
    return token


def get_values(spreadsheet_id: str, sheet_range: str, kind: str = "sheets") -> list[list]:
    """GET spreadsheets.values.get — sheet_range это A1-нотация ('Metric_Config'
    целиком, или 'Daily_Trends!A1:BZ2000' и т.п.)."""
    token = _get_access_token(kind)
    resp = httpx.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{sheet_range}",
        headers={"Authorization": f"Bearer {token}"}, timeout=20.0,
    )
    resp.raise_for_status()
    return resp.json().get("values", [])


def append_row(spreadsheet_id: str, sheet_title: str, values: list, kind: str = "sheets") -> None:
    """POST spreadsheets.values.append (operation: append, без поиска
    существующей строки — порт "Digest_log" node)."""
    token = _get_access_token(kind)
    resp = httpx.post(
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{sheet_title}:append",
        params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
        headers={"Authorization": f"Bearer {token}"},
        json={"values": [values]}, timeout=20.0,
    )
    resp.raise_for_status()


def append_or_update_row(spreadsheet_id: str, sheet_title: str, key_col: str, row: dict, kind: str = "sheets") -> None:
    """Порт n8n Google Sheets "appendOrUpdate" (matchingColumns=[key_col],
    mappingMode=autoMapInputData): читает шапку + существующие строки,
    находит строку по key_col — если нашёл, обновляет только те колонки,
    ключи которых ЕСТЬ в `row` (autoMapInputData так и работает — маппит
    только присутствующие в item поля); колонки, которых в `row` вообще НЕТ
    как ключа, остаются НЕТРОНУТЫМИ (сохраняют то, что уже было в ячейке).
    Если ключ в `row` есть, но значение None/'' — ячейка ЗАТИРАЕТСЯ пустой
    строкой (это и есть задокументированный «Garmin null-clobber»: вызывающий
    обязан сам выкинуть пустые ключи ДО вызова, если хочет их сохранить —
    Code in JavaScript в оригинале делает это явно, biohacking_ingest.py
    делает то же самое). Если строка не найдена — добавляется новая, поля не
    указанные в `row` остаются пустыми (для новой строки "сохранять" нечего).
    Колонки, которых нет в шапке вообще, молча пропускаются — таблицы уже
    содержат нужные заголовки, добавление новых колонок сюда — ручная задача,
    как и было до переноса."""
    token = _get_access_token(kind)
    all_values = get_values(spreadsheet_id, sheet_title, kind=kind)
    if not all_values:
        raise RuntimeError(f"sheets_client: лист {sheet_title!r} пуст, нет шапки — не пишу")
    header = all_values[0]
    try:
        key_idx = header.index(key_col)
    except ValueError:
        raise RuntimeError(f"sheets_client: колонки {key_col!r} нет в шапке {sheet_title!r}")

    key_val = str(row.get(key_col, ""))
    match_row_num: Optional[int] = None  # 1-based, включая шапку (строка 2 = первая данных)
    existing: list = []
    for i, r in enumerate(all_values[1:], start=2):
        if len(r) > key_idx and str(r[key_idx]) == key_val:
            match_row_num, existing = i, r
            break

    out_row = []
    for j, h in enumerate(header):
        if h in row:
            v = row[h]
            out_row.append("" if v is None else str(v))
        else:
            out_row.append(existing[j] if j < len(existing) else "")
    end_col = _col_letter(len(header))
    if match_row_num:
        rng = f"{sheet_title}!A{match_row_num}:{end_col}{match_row_num}"
        resp = httpx.put(
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{rng}",
            params={"valueInputOption": "USER_ENTERED"},
            headers={"Authorization": f"Bearer {token}"},
            json={"values": [out_row]}, timeout=20.0,
        )
    else:
        resp = httpx.post(
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{sheet_title}:append",
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            headers={"Authorization": f"Bearer {token}"},
            json={"values": [out_row]}, timeout=20.0,
        )
    resp.raise_for_status()


def _col_letter(n: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA, ..."""
    s = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def get_calendar_events(calendar_id: str, time_min: str, time_max: str) -> list[dict]:
    """GET calendarList events.list — порт "Get Calendar Events"."""
    token = _get_access_token("calendar")
    resp = httpx.get(
        f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
        params={"timeMin": time_min, "timeMax": time_max, "singleEvents": "true"},
        headers={"Authorization": f"Bearer {token}"}, timeout=20.0,
    )
    resp.raise_for_status()
    return resp.json().get("items", [])
