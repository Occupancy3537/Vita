"""
card.agent_step — трассировка агентного цикла (план §3.3, §3.9): "сегодня
стоимость и латентность доктора не видны нигде". Один вызов на шаг цикла
(модель или инструмент), та же схема, что migrate_doctor_tables.sql.
"""
import json
from typing import Optional

from psycopg import sql
from ulid import ULID

from app.db import schema


def write_step(
    cur, *, turn_id: str, step_no: int, role: str,
    model: Optional[str] = None, tool_name: Optional[str] = None,
    tool_args: Optional[dict] = None, tool_result_hash: Optional[str] = None,
    latency_ms: Optional[int] = None, tokens_prompt: Optional[int] = None,
    tokens_completion: Optional[int] = None, tokens_reasoning: Optional[int] = None,
    cost_usd: Optional[float] = None,
) -> str:
    step_id = f"as_{ULID()}"
    cur.execute(
        sql.SQL(
            "INSERT INTO {t} (id, turn_id, step_no, role, model, tool_name, tool_args, "
            "tool_result_hash, latency_ms, tokens_prompt, tokens_completion, tokens_reasoning, cost_usd) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        ).format(t=sql.Identifier(schema(), "agent_step")),
        (step_id, turn_id, step_no, role, model, tool_name,
         json.dumps(tool_args) if tool_args is not None else None, tool_result_hash,
         latency_ms, tokens_prompt, tokens_completion, tokens_reasoning, cost_usd),
    )
    return step_id
