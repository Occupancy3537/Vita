# -*- coding: utf-8 -*-
"""Журнал предсказаний и сверка точности (2026-10-01, фаза 6 плана; просьба Влада: «когда база накопится — сравним, насколько
точны были предсказания, и поправим, если получится совсем неточно»).

kind='day_index': в 15:00 по Владивостоку записываем прогноз индекса дня («Прогноз дня» на главной), а когда день закрыт и
вечерний снимок (vita_day_snapshot) записан — дописываем фактический итог и ошибку. Точность = средняя абсолютная ошибка (MAE)
и смещение (bias: прогноз − факт) по последним дням. Мало наблюдений — честно «копится»."""
import logging
import statistics
import time
from datetime import date
from typing import Optional

from psycopg import sql

from app import run_log, timeutil
from app.db import get_conn, schema
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)
KIND_DAY_INDEX = "day_index"
RECORD_HOUR_VL, RECORD_MINUTE_VL = 15, 0
MIN_N_FOR_STATS = 7            # меньше наблюдений — показываем только «копится»
MIN_N_FOR_ALERT = 14
MAE_ALERT_POINTS = 10          # средняя ошибка больше 10 баллов из 100 — предсказания неточны
WINDOW_DAYS = 30


def record_prediction(cur, kind: str, target_day: date, predicted: float, meta: Optional[dict] = None) -> bool:
    """Первая запись за (kind, день) остаётся — повторный запуск её не меняет (идемпотентно)."""
    import json
    cur.execute(sql.SQL("INSERT INTO {t} (kind, target_date, predicted, meta) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING")
                .format(t=sql.Identifier(schema(), "prediction_log")),
                (kind, target_day, predicted, json.dumps(meta, ensure_ascii=False) if meta else None))
    return cur.rowcount == 1


def fill_observed(cur) -> int:
    """Дописывает факт для прогнозов индекса дня, у которых день уже закрыт (есть снимок). Возвращает число заполненных."""
    cur.execute(
        sql.SQL("UPDATE {p} p SET observed = (s.ring->>'score')::numeric, observed_ts = now() FROM {s} s "
                "WHERE p.kind = %s AND p.observed IS NULL AND s.date = p.target_date AND s.ring->>'score' IS NOT NULL")
        .format(p=sql.Identifier(schema(), "prediction_log"), s=sql.Identifier(schema(), "vita_day_snapshot")),
        (KIND_DAY_INDEX,))
    return cur.rowcount


def accuracy(cur, kind: str = KIND_DAY_INDEX, window_days: int = WINDOW_DAYS) -> dict:
    """{n, mae, bias, max_err, enough, alert}: по закрытым дням последних window_days."""
    cur.execute(
        sql.SQL("SELECT predicted, observed FROM {t} WHERE kind = %s AND observed IS NOT NULL "
                "AND target_date >= (now() - make_interval(days => %s))::date ORDER BY target_date")
        .format(t=sql.Identifier(schema(), "prediction_log")), (kind, window_days))
    pairs = [(float(p), float(o)) for p, o in cur.fetchall()]
    n = len(pairs)
    if not n:
        return {"n": 0, "mae": None, "bias": None, "max_err": None, "enough": False, "alert": False}
    errs = [p - o for p, o in pairs]
    mae = round(statistics.mean(abs(e) for e in errs), 1)
    return {"n": n, "mae": mae, "bias": round(statistics.mean(errs), 1), "max_err": round(max(abs(e) for e in errs), 1),
            "enough": n >= MIN_N_FOR_STATS, "alert": n >= MIN_N_FOR_ALERT and mae > MAE_ALERT_POINTS}


def run_once() -> dict:
    """Записать сегодняшний прогноз и дополнить фактами закрытые дни. Падение одного шага не роняет другой."""
    from app import vita
    out = {"recorded": False, "filled": 0}
    with get_conn() as conn, conn.cursor() as cur:
        score = (vita.build_today(cur).get("ring") or {}).get("score")
        if score is not None:
            out["recorded"] = record_prediction(cur, KIND_DAY_INDEX, timeutil.today(), score, {"hour": RECORD_HOUR_VL})
        conn.commit()
    with get_conn() as conn, conn.cursor() as cur:
        out["filled"] = fill_observed(cur)
        conn.commit()
    return out


def run_scheduler() -> None:
    logger.info("predictions: старт (запись прогноза в %02d:%02d ВЛ)", RECORD_HOUR_VL, RECORD_MINUTE_VL)
    while True:
        try:
            timeutil.sleep_until_local(RECORD_HOUR_VL, RECORD_MINUTE_VL)
            run_once()
            run_log.mark_run("predictions")
        except Exception as e:
            logger.exception("predictions run_once упал — повтор через час")
            alert_on_failure("predictions", e)
            time.sleep(3600)
