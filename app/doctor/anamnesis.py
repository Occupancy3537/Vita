"""
Анамнез-коллектор (Волна 2 / B1, 2026-09-17): перенос `Anamnesis Collector` из n8n.
Данные — `health.anamnesis` (лист Health_DB!Anamnesis остаётся витриной для Влада).

Две обязанности:
1. Планировщик: ежедневно 11:00 Владивосток — выбрать вопрос (приоритет категорий
   + кап 3 попыток — порт `anam_pick_next_v2.js` из n8n дословно), спросить
   force_reply с тегом #A##, пометить asked/skipped.
2. Приём ответов: реплай на сообщение с тегом #A## диспетчер маршрутизирует сюда
   (детерминированно, без LLM) — запись ответа и подтверждение «Записал ✅».
   До этого пересылка в выключенный Capitan молча теряла ответы (риск №1).
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from app.db import get_conn, schema
from app.doctor import telegram
from app import run_log, timeutil
from app.scheduler_alert import alert_on_failure

logger = logging.getLogger(__name__)

CHAT_ID = "8956401"
VL = timezone(timedelta(hours=10))
ASK_HOUR_VL = 11
_ANAM_TAG = re.compile(r"#(A\d{2})\b")


def _vl_today() -> str:
    return datetime.now(VL).strftime("%Y-%m-%d")


def _days_between(a: str, b: str) -> int:
    """Порт daysBetween из anam_pick_next_v2.js: без даты — 999 (『давно』)."""
    if not a or not b:
        return 999
    try:
        da = datetime.strptime(a[:10], "%Y-%m-%d")
        db_ = datetime.strptime(b[:10], "%Y-%m-%d")
        return round((db_ - da).days)
    except ValueError:
        return 999


def _cat_rank(cat: str) -> int:
    """Порт catRank: препараты(0) → аллергии(1) → наследственность(2) → личный
    анамнез(3) → образ жизни(4) → профилактика(5) → прочее(6) → происхождение(9)."""
    c = str(cat or "").lower().strip()
    if c.startswith("препарат") or "лекарств" in c or "добавк" in c:
        return 0
    if c.startswith("аллерг"):
        return 1
    if c.startswith("наследственн"):
        return 2
    if c.startswith("личный анамнез") or c.startswith("личн"):
        return 3
    if c.startswith("образ жизни"):
        return 4
    if c.startswith("профилактика") or c.startswith("скрин"):
        return 5
    if c.startswith("происхожд"):
        return 9
    return 6


def pick_next(rows: list[dict], today: str) -> dict:
    """Чистая функция выбора действия — порт pick_next из n8n (для тестов)."""
    rows = [r for r in rows if r.get("Q_ID")]
    rows.sort(key=lambda r: (_cat_rank(r.get("Category", "")), str(r.get("Q_ID", ""))))
    total = len(rows)
    answered_real = sum(1 for r in rows if str(r.get("Status")) == "answered")

    asked_row = next((r for r in rows if str(r.get("Status")) == "asked"), None)
    pick, attempt, skip_q_id = None, 1, ""
    if asked_row:
        cur_att = int(asked_row.get("Attempts") or 1)
        since = _days_between(str(asked_row.get("Asked_Date") or ""), today)
        if since < 2:
            return {"action": "wait", "reason": f"ждём ответа на {asked_row.get('Q_ID')} ({since}д, попытка {cur_att}/3)",
                    "total": total, "answered": answered_real}
        if cur_att >= 3:
            skip_q_id = asked_row.get("Q_ID")
            pick = next((r for r in rows if str(r.get("Status")) == "pending"), None)
            attempt = 1
        else:
            pick = asked_row
            attempt = cur_att + 1
    else:
        pick = next((r for r in rows if str(r.get("Status")) == "pending"), None)
        attempt = 1

    if not pick:
        if skip_q_id:
            return {"action": "skip_only", "skip_q_id": skip_q_id, "reason": "вопрос пропущен, пул закрыт",
                    "total": total, "answered": answered_real}
        return {"action": "done", "reason": f"все вопросы закрыты ({answered_real + 0}/{total})",
                "total": total, "answered": answered_real}

    num = answered_real + 1
    text = (f"🧬 Анамнез {num}/{total}\n\n{pick.get('Question')}"
            "\n\n↩️ Ответь на это сообщение (reply / свайп влево). Или просто напиши ответ — я пойму по тегу "
            f"#{pick.get('Q_ID')} ниже."
            + (f"\n\n(повторяю — не увидел ответа, попытка {attempt}/3)" if attempt > 1 else "")
            + f"\n\n#{pick.get('Q_ID')}")
    return {"action": "ask", "q_id": pick.get("Q_ID"), "category": pick.get("Category"), "text": text,
            "attempt": attempt, "num": num, "total": total, "answered": answered_real,
            "skip_q_id": skip_q_id}


def _fetch_rows(cur) -> list[dict]:
    cur.execute(
        f'SELECT "Q_ID","Category","Question","Status","Asked_Date","Answer","Answered_Date","Attempts" '
        f'FROM {schema()}.anamnesis'
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def ask_daily() -> dict:
    """Один цикл опроса: выбрать, спросить (если пора), пометить. Возвращает результат pick_next."""
    with get_conn() as conn, conn.cursor() as cur:
        res = pick_next(_fetch_rows(cur), _vl_today())
        if res["action"] == "ask":
            telegram.send_message(CHAT_ID, res["text"], force_reply=True)
            cur.execute(
                f'UPDATE {schema()}.anamnesis SET "Status"=\'asked\', "Asked_Date"=%s, "Attempts"=%s '
                f'WHERE "Q_ID"=%s',
                (_vl_today(), res["attempt"], res["q_id"]),
            )
        if res.get("skip_q_id"):
            cur.execute(
                f'UPDATE {schema()}.anamnesis SET "Status"=\'skipped\' WHERE "Q_ID"=%s AND "Status"=\'asked\'',
                (res["skip_q_id"],),
            )
        conn.commit()
    logger.info("anamnesis ask_daily: %s", {k: v for k, v in res.items() if k != "text"})
    return res


def handle_reply(update: dict) -> None:
    """Реплай на сообщение с тегом #A##: записать ответ, подтвердить. Идемпотентно:
    повторный ответ на уже закрытый вопрос НЕ перезаписывает его (ответ видно в логе).

    F10 (внешний аудит логики, 2026-09-22): текст вопроса обещает «или просто
    напиши ответ — я пойму по тегу #A05», но тег искался ТОЛЬКО в тексте
    сообщения бота (реплай), и обычный ответ без reply уезжал в диспетчер к
    доктору. Теперь тег принимается и в самом тексте ответа (диспетчер
    маршрутизирует такие сообщения сюда, см. dispatch.route)."""
    msg = update.get("message") or {}
    reply_text = ((msg.get("reply_to_message") or {}).get("text")) or ""
    own_text = (msg.get("text") or "").strip()
    m = _ANAM_TAG.search(reply_text) or _ANAM_TAG.search(own_text)
    if not m:
        logger.warning("anamnesis handle_reply без тега — проигнорировано")
        return
    q_id = m.group(1)
    # F10: если тег был в самом тексте ответа (не реплай) — в карту он не попадёт
    text = _ANAM_TAG.sub("", own_text).strip()
    if not text:
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f'UPDATE {schema()}.anamnesis SET "Answer"=%s, "Answered_Date"=%s, "Status"=\'answered\' '
            f'WHERE "Q_ID"=%s AND "Status" != \'answered\' RETURNING "Q_ID"',
            (text, _vl_today(), q_id),
        )
        row = cur.fetchone()
        conn.commit()
    if row:
        telegram.send_message(CHAT_ID, f"Записал ✅ ({q_id})")
        logger.info("anamnesis: записан ответ %s", q_id)
    else:
        telegram.send_message(CHAT_ID, f"Вопрос {q_id} уже закрыт или неизвестен — ответ не записан. "
                                       f"Если хочешь дополнить — скажи доктору обычным сообщением.")
        logger.info("anamnesis: ответ %s не записан (закрыт/неизвестен)", q_id)


def run_scheduler() -> None:
    """Поток-планировщик: спит до 11:00 VL, зовёт ask_daily, повторяет. Ошибка цикла
    не убивает поток (тот же принцип, что у poller._safe_process)."""
    logger.info("anamnesis scheduler: старт")
    while True:
        try:
            timeutil.sleep_until_local(ASK_HOUR_VL)  # Фаза 3: час — по поясу человека
            ask_daily()
            run_log.mark_run("anamnesis")
        except Exception as e:
            logger.exception("anamnesis ask_daily упал — повтор завтра")
            alert_on_failure("anamnesis", e)
            time.sleep(3600)
