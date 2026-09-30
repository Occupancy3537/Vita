"""Общий каркас: HTTP, нормализация, raw-доказательства, ворота качества."""
from __future__ import annotations

import gzip
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from . import DELAY_SECONDS, HTTP_TIMEOUT, RETRIES, UA, VERIFY_NOTE_TMPL, CITY, LAB_NAMES

# ---------------------------------------------------------------- HTTP -----

_last_request_at: dict[str, float] = {}


def new_session() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"},
                        timeout=HTTP_TIMEOUT, follow_redirects=False)


def get(s: httpx.Client, url: str, *, binary: bool = False):
    """GET с задержкой по хосту, повторами и честным кодом возврата.
    Возвращает (status_code, content:str|bytes) или (status, None) при
    транспортной ошибке/не-200 — вызывающий решает, это reject."""
    host = urlparse(url).netloc
    wait = DELAY_SECONDS - (time.monotonic() - _last_request_at.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    err = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = s.get(url)
            _last_request_at[host] = time.monotonic()
            if r.status_code == 200:
                return 200, (r.content if binary else r.text)
            err = f"HTTP {r.status_code}"
            if r.status_code in (404, 410):
                return r.status_code, None
        except httpx.HTTPError as e:
            err = f"{type(e).__name__}: {e}"[:200]
        if attempt < RETRIES:
            time.sleep(2 * attempt)
    return (-1 if err is None else 0), None


def robots_allows(s: httpx.Client, base: str, path: str) -> tuple[bool, str]:
    """Прочитать robots.txt один раз на хост и спросить про путь. Ошибка
    чтения robots = False («не смогли проверить — не ходим»)."""
    rp = RobotFileParser()
    try:
        r = s.get(base + "/robots.txt")
        rp.parse(r.text.splitlines())
        return rp.can_fetch(UA.split()[0], path), ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:160]


# ------------------------------------------------------------ нормализация --

def norm_name(s: str) -> str:
    """То же правило, что app.lab_prices_ingest._norm: lower, ё→е, только
    буквы/цифры/пробелы. Используется для сверки и отчётов, НЕ для данных."""
    s = (s or "").lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", " ", s).strip()


def norm_name_loose(s: str) -> str:
    """Для СОПОСТАВЛЕНИЯ названий в отчётах (не для данных): срезать
    пояснения в скобках («Глюкоза (Glucose)» → «глюкоза») и латинские
    хвосты после «/». Сайты пишут одно и то же исследование по-разному;
    без этого честный точный матчинг даёт ложные «нет»."""
    s = (s or "").split("/")[0]
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\[[^\]]*\]", " ", s)
    return norm_name(s)


def parse_price(raw) -> float | None:
    """«2 200 ₽» / «790» / «1 175 руб.» → float; не смогли — None (не 0!)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).replace("\u00a0", " ").replace("\u202f", " ")
    m = re.search(r"(\d[\d\s]*)(?:[.,](\d{1,2}))?", s)
    if not m:
        return None
    whole = m.group(1).replace(" ", "")
    frac = m.group(2) or "0"
    try:
        return float(f"{whole}.{frac}")
    except ValueError:
        return None


# ------------------------------------------------------- raw-доказательства --

def save_raw(run_dir: str, lab: str, name: str, content) -> str:
    """Сжать сырой ответ в raw/ и вернуть относительный путь (raw_ref)."""
    raw_dir = os.path.join(run_dir, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", name)[:80]
    fname = f"{lab}_{safe}.gz"
    path = os.path.join(raw_dir, fname)
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    with gzip.GzipFile(path, "wb", mtime=0) as f:
        f.write(data)
    return f"raw/{fname}"


# ----------------------------------------------------------------- строки --

def make_row(*, lab_code: str, ext_code: str, name: str, price, category,
             term, composition, url, source_url: str, raw_ref: str,
             city_confirmed: bool, collected_at: str) -> dict:
    """Строка итогового JSON. Строгий формат задачи; существующие поля
    lab_prices_ingest.parse_rows не менять, новые ingest игнорирует."""
    return {
        "lab_code": lab_code,
        "lab_name": LAB_NAMES.get(lab_code, lab_code),
        "city": CITY if city_confirmed else "",
        "external_code": ext_code,
        "name": (name or "").strip(),
        "category": (category or "").strip() or None,
        "price": price,
        "currency": "RUB",
        "turnaround_time": (term or "").strip() or None,
        "composition": (composition or "").strip() or None,
            "url": url,
            "scraped_at": collected_at,
            # --- новые поля (задача, формат выхода) ---
            "collected_at": collected_at,
            "source_url": source_url,
        "raw_ref": raw_ref,
        "city_confirmed": bool(city_confirmed),
        "verified": False,
        "verify_note": VERIFY_NOTE_TMPL.format(date=collected_at[:10]),
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------- ворота качества --

GATE_REQUIRED_FIELDS = ("source_url", "raw_ref", "city_confirmed", "collected_at")


def validate_rows(rows: list[dict]) -> list[str]:
    """Проверка обязательных полей каждой строки (задача, ворота). Возвращает
    список причин; пустой список = всё чисто. Строки с нарушениями НЕ должны
    были пройти validate_rows коллекционера — здесь страховка."""
    problems = []
    seen = set()
    for r in rows:
        lab, ext = r.get("lab_code"), r.get("external_code")
        tag = f"{lab}/{ext}"
        price = r.get("price")
        if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0:
            problems.append(f"{tag}: цена не число или <=0: {price!r}")
        for f in GATE_REQUIRED_FIELDS:
            if not r.get(f):
                problems.append(f"{tag}: пусто {f}")
        if not r.get("city_confirmed"):
            problems.append(f"{tag}: город не подтверждён")
        key = (lab, ext)
        if key in seen:
            problems.append(f"{tag}: дубль (lab_code, external_code)")
        seen.add(key)
    return problems


def compare_with_previous(rows: list[dict], prev_rows: list[dict]) -> list[str]:
    """Ворота против предыдущего прошедшего запуска (задача): падение позиций
    >20% у лабы; изменение цены >50% у >15% позиций. Обе выборки — уже
    валидные строки."""
    reasons = []
    cur_by_lab: dict[str, dict[tuple, float]] = {}
    prev_by_lab: dict[str, dict[tuple, float]] = {}
    for r in rows:
        cur_by_lab.setdefault(r["lab_code"], {})[(r["lab_code"], r["external_code"])] = r["price"]
    for r in prev_rows:
        prev_by_lab.setdefault(r["lab_code"], {})[(r["lab_code"], r["external_code"])] = r["price"]
    for lab in sorted(set(prev_by_lab) | set(cur_by_lab)):
        prev_n, cur_n = len(prev_by_lab.get(lab, {})), len(cur_by_lab.get(lab, {}))
        if prev_n and cur_n < prev_n * 0.8:
            reasons.append(f"[{lab}] позиций {cur_n} против {prev_n} — падение >20%")
        prev = prev_by_lab.get(lab, {})
        cur = cur_by_lab.get(lab, {})
        if not prev:
            continue
        changed = [
            1 for k, p in cur.items()
            if k in prev and prev[k] and p
            and (abs(p - prev[k]) / prev[k]) > 0.5
        ]
        if cur and len(changed) > 0.15 * len(cur):
            reasons.append(f"[{lab}] цена >50% изменилась у {len(changed)} из {len(cur)} позиций (>15%)")
    return reasons
