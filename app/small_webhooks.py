"""Мелкие самостоятельные n8n-вебхуки без расписания и без LLM — порт
2026-09-20 (группа малых утилит). Каждая функция — 1:1 с отдельным бывшим
n8n-воркфлоу, сгруппированы в одном файле только потому, что по отдельности
каждая слишком мала для своего модуля (тот же принцип, что уже применялся
негласно к app/gate_watch.py — маленький сфокусированный модуль на функцию,
здесь просто несколько функций такого размера в одном файле)."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import get_conn

# --- «Был ли завтрак?» (webhook check-breakfast) -----------------------------
#
# Исходный SQL уже нормализует дату в 'YYYY-MM-DD' (порт 11.09, см. STATE.md
# B2-1) — многоформатная проверка дат в оригинальном Check Logic (DD.MM.YYYY,
# DD/MM/YYYY) с тех пор мертва (входные строки всегда 'YYYY-MM-DD'), поэтому
# здесь только прямое сравнение, не три формата.


def check_breakfast(cur) -> dict:
    cur.execute(
        "SELECT to_char(\"Date\" AT TIME ZONE 'Asia/Vladivostok', 'YYYY-MM-DD') AS d "
        'FROM health.meals '
        "WHERE (\"Date\" AT TIME ZONE 'Asia/Vladivostok')::date >= (now() AT TIME ZONE 'Asia/Vladivostok')::date - 2 "
        'ORDER BY "Date" DESC'
    )
    dates = {r[0] for r in cur.fetchall()}
    today_vl = (datetime.now(timezone.utc) + timedelta(hours=10)).strftime("%Y-%m-%d")
    return {"breakfast_ready": today_vl in dates}


# --- Отметка выполнения действия (webhook action-ack) ------------------------
#
# 2026-09-20: раньше писал ТОЛЬКО в Sheets Action_Log — health.action_log
# (Волна #24, today-dashboard) синкается оттуда лишь раз в сутки ночным
# sheets_to_pg_mirror.js, так что отметка "сделал" могла не доехать до
# дашборда почти сутки. Теперь пишет напрямую в Postgres — тот же canonical-
# источник, что читает get_today_dashboard(). Sheets больше не участвует.
# 2026-09-22 (внешний аудит, K3 — КРИТИЧНО): значение раньше было литералом
# здесь — вынесено в env (ACTION_ACK_TOKEN, run.sh) и ротировано (засветилось
# внешнему аудиту). Синхронизировано со значением, зашитым client-side в
# /var/www/d-.../{index,v4}.html (ACK_TOKEN) — оба места правятся вместе.
_ACTION_ACK_TOKEN = os.environ.get("ACTION_ACK_TOKEN", "")


def action_ack(token: str, action_id: str, done) -> dict:
    if not _ACTION_ACK_TOKEN or token != _ACTION_ACK_TOKEN:
        return {"ok": False, "error": "forbidden"}
    action_id = str(action_id or "").strip()
    if not action_id:
        return {"ok": False, "error": "no_id"}
    done_bool = done if isinstance(done, bool) else str(done) == "true"
    parts = action_id.split("|", 1)
    date_issued, title = parts[0], (parts[1] if len(parts) > 1 else "")
    done_at = (datetime.now(timezone.utc) + timedelta(hours=10)).strftime("%Y-%m-%d %H:%M")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            'INSERT INTO health.action_log ("Action_ID", "Date_Issued", "Title", "Done", "Done_At") '
            "VALUES (%s, %s, %s, %s, %s) "
            'ON CONFLICT ("Action_ID") DO UPDATE SET "Done" = EXCLUDED."Done", "Done_At" = EXCLUDED."Done_At"',
            (action_id, date_issued, title, "да" if done_bool else "нет", done_at),
        )
        conn.commit()
    return {"ok": True, "error": None}


# --- Статический виджет «Дневник питания» (webhook dashboard) ---------------
#
# Чистая статика — сам HTML не меняется, зовёт /dashboard/today-nutrition
# клиентским JS. Единственная реальная правка при переносе: этот fetch() всё
# ещё указывал на СТАРЫЙ n8n-вебхук today-nutrition, отключённый в #25 (у
# самого виджета последнее реальное открытие — 07.09, поэтому поломка не
# всплыла раньше) — обновлён на card-service, заодно и токен (виджетный токен
# `wFSIRB6...` никогда не совпадал с токеном today-nutrition — похоже, виджет
# не работал ещё до этой сессии, не только с момента переноса).
_WIDGET_PATH = Path(__file__).parent / "static" / "nutrition_widget.html"
_widget_html_cache: str | None = None


def get_nutrition_widget_html() -> str:
    global _widget_html_cache
    if _widget_html_cache is None:
        _widget_html_cache = _WIDGET_PATH.read_text(encoding="utf-8")
    return _widget_html_cache
