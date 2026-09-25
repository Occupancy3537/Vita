"""
П5 §1-§7, план §3.1/§3.7 — детерминированный гейт красных флагов, вызываемый на
каждом ходе доктора ДО модели. Две фазы РАЗНЕСЕНЫ ПО ВРЕМЕНИ, не по логике:

  fast_gate()          — A (regex) + bracelet-cross (F7). Ноль сети, <200мс
                         (Phase-2 приёмка). Единственное, что может решить
                         fast_gate — это L3: слой A и bracelet-cross в союзе
                         (app/redflag_union.py) всегда дают L3 либо ничего,
                         L1/L2 физически не выходят из этой пары источников
                         (level_for() их не порождает) — только слой B умеет.
  slow_gate_followup() — слой B (LLM). Вызывается ПОСЛЕ того, как ответ (или
                         эмердженси-короткое замыкание) уже ушёл пациенту —
                         "никогда не задерживает эмердженси" (план §3.7)
                         означает буквально это, не "считается реже". Пишет
                         в ТУ ЖЕ сессию отдельным rf_event (F8), не дублирует
                         то, что уже записал fast_gate (передаёт в union
                         пустые layer_a_hits/bracelet_hits — они уже учтены).
                         2026-09-23 (аудит логики, L3): если сам находит L3 —
                         не только пишет rf_event, но и досылает пациенту
                         активное предупреждение (гарантированной доставкой) —
                         второй независимый слой больше не молчит на находке,
                         которую первый пропустил.

Порог эскалации (план §3.7, предложение принято Владом с оговоркой "калибровать
во время тестирования", не как жёсткая константа с первого дня):
  L3 -> короткое замыкание здесь же, модель/агентный цикл не вызывается вообще
  L2 -> доктор отвечает как обычно, но первой строкой — явная рекомендация
        показаться врачу сегодня (добавляет вызывающий, loop.py — Phase 4;
        здесь, в Phase 2, L2 только детектируется и пишется в rf_event)
  L1 -> только заметка в rf_event, разговор идёт как обычно
"""
import logging
from typing import Optional

from app.db import get_conn, schema
from app.doctor.dialog import write_turn
from app.journal import write_journal
from app.redflag_b import classify as classify_layer_b
from app.redflag_union import evaluate_and_record, evaluate_union, record_rf_event
from app.write_path import new_id, prov

logger = logging.getLogger(__name__)

EMERGENCY_REPLY = (
    "⚠️ То, что вы описали, похоже на состояние, которое нельзя ждать. "
    "Пожалуйста, вызовите скорую (103 / 112) или обратитесь в приёмный покой прямо сейчас.\n\n"
    "Я записал это в карту. Когда будет безопасно — вернитесь и расскажите, чем всё закончилось."
)


def fast_gate(cur, text: str, source_id: Optional[str] = None) -> dict:
    """A + bracelet, детерминированно, без сети — см. докстринг модуля."""
    return evaluate_and_record(cur, text, source_id)


def record_emergency_episode(cur, category: str, text: str, source_id: Optional[str]) -> str:
    """Минимальная запись эпизода на emergency-пути (план §3.1: rf_event +
    episode + dialog_turn — LLM не вызывается, значит структурного извлечения
    тоже нет; symptom_key берём из категории флага, а не из drafts, как в
    write_path.apply_draft. Не через apply_draft() намеренно: тот путь ждёт
    Draft от extraction.py, которого здесь по определению нет и не будет."""
    ep_id = new_id("ep")
    cur.execute(
        f"INSERT INTO {schema()}.episode "
        f"(id, ts_event, provenance, verification, symptom_key, onset_ts, status, context) "
        f"VALUES (%s, now(), %s, 'auto', %s, now(), 'open', %s)",
        (ep_id, prov("redflag_emergency", {"source_id": source_id}), f"emergency:{category}", text[:500]),
    )
    write_journal(cur, "episode", ep_id, "create",
                  diff={"symptom_key": f"emergency:{category}", "status": "open", "context": text[:500]},
                  reason=f"source={source_id}", actor="redflag_gate", link_back=True)
    return ep_id


def handle_emergency(cur, chat_id: str, gate_result: dict, text: str, source_id: Optional[str]) -> str:
    """L3 короткое замыкание целиком: episode + assistant dialog_turn. Вызывающий
    (intake.py) держит открытую транзакцию для user-хода — дописывает в неё же,
    коммитит сам; Telegram-отправку тоже делает вызывающий (эта функция — только
    БД-часть, без сети, чтобы остаться в бюджете <200мс)."""
    category = gate_result["result"]["category"]
    ep_id = record_emergency_episode(cur, category, text, source_id)
    write_turn(cur, chat_id=chat_id, role="assistant", text=EMERGENCY_REPLY,
               rf_level="L3", wrote_anything=True, meta={"episode_id": ep_id, "category": category})
    return EMERGENCY_REPLY


FOLLOWUP_L3_PREFIX = "⚠️ Пересмотрел твоё предыдущее сообщение внимательнее — оно похоже на неотложное состояние"


def slow_gate_followup(chat_id: str, text: str, source_id: Optional[str] = None,
                        prior_replies: Optional[list[str]] = None) -> None:
    """Слой B — вызывать ПОСЛЕ отправки ответа пациенту (не на пути к нему).
    Деградация (сеть легла, невалидный JSON) уже обрабатывается внутри
    redflag_b.classify как degraded=True, не исключением — здесь ловим только
    неожиданное, чтобы сбой B не ронял фоновую задачу целиком.

    L3 (аудит логики, 2026-09-23, КРИТИЧНО): раньше ЛЮБОЙ уровень от B —
    включая L3 — только писал rf_event и молчал. Для L1 это нормально
    (заметка на будущее), но L3 от B означает: A (regex) пропустил, семан-
    тический слой нашёл, а пациенту УЖЕ ушёл обычный (не эмердженси) ответ —
    "второй независимый слой безопасности" (план §1.3) констатировал угрозу
    в карточку и молчал перед пациентом. Теперь L3 от B досылает активное
    предупреждение той же гарантированной доставкой (ретраи + фолбэк на
    сервисный бот), что и детерминированный гейт — не задерживает исходный ответ
    (уже ушёл), но и не оставляет находку немой."""
    try:
        layer_b = classify_layer_b(text, prior_replies or [])
    except Exception:
        logger.exception("doctor.gate: layer B classify failed")
        return
    if not layer_b.hit or layer_b.degraded:
        return
    result = evaluate_union([], [], layer_b)  # только вклад B — A/bracelet уже учёл fast_gate
    if not result.get("level"):
        return
    with get_conn() as conn, conn.cursor() as cur:
        record_rf_event(cur, result, source_id)
        conn.commit()

    if result["level"] == "L3":
        from app.doctor.intake import _deliver_emergency  # ленивый импорт — избегаем цикла gate<->intake
        note = result.get("context_note") or "см. карту"
        followup = (f"{FOLLOWUP_L3_PREFIX} ({note}). Если это всё ещё актуально — "
                    "вызови скорую (103 / 112) или обратись в приёмный покой прямо сейчас.")
        _deliver_emergency(chat_id, None, followup)
