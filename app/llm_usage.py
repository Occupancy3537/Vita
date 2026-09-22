"""Учёт стоимости LLM-вызовов ВНЕ доктора (2026-09-22, «полные расходы на ИИ»
на странице «Настройки»; запрос Влада «давай всё»).

Доктор уже пишет структурный трейс в card.agent_step (там же latency и cost
каждого шага) — его сюда НЕ дублируем. Этот модуль — для остальных вызовов
OpenRouter: извлечение симптома, red-flag слой B, классификатор диспетчера,
дневник еды (фото/текст), регистратор, отчёты о питании, watchdog, память L2.
Страница (app/system_status.py) суммирует оба источника.

Fail-safe: учёт денег никогда не роняет сам вызов — сбой записи только в лог
(тот же принцип, что у run_log).
"""
import logging

from app.db import get_conn, schema

logger = logging.getLogger(__name__)


def record(module: str, model: str | None, usage: dict | None) -> None:
    """Записать один вызов. usage — сырой usage-блок OpenRouter; часть
    провайдеров/моделей его не возвращает — тогда писать нечего и не пишем."""
    if not usage:
        return
    cost = usage.get("cost")
    tokens_prompt = usage.get("prompt_tokens")
    tokens_completion = usage.get("completion_tokens")
    if cost is None and tokens_prompt is None and tokens_completion is None:
        return
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO {t} (module, model, tokens_prompt, tokens_completion, cost_usd) "
                "VALUES (%s, %s, %s, %s, %s)".format(t=schema() + ".llm_usage"),
                (module, model, tokens_prompt, tokens_completion, cost),
            )
            conn.commit()
    except Exception:
        logger.warning("llm_usage: не удалось записать стоимость (%s/%s)",
                       module, model, exc_info=True)
