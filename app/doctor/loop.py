"""
Агентный цикл нового доктора (план §3.4) — свой, без LangChain. Раунды
инструментов ограничены (`config.MAX_TOOL_ROUNDS`), инструменты внутри одного
раунда исполняются параллельно через `ThreadPoolExecutor` — card-service в
остальном синхронный (см. app/db.py: "объём трафика не требует asyncio"),
но раздельные короткие соединения на поток дают настоящую параллельность для
IO-bound SQL/HTTP инструментов без перехода всего сервиса на asyncio.

`run_turn()` не принимает курсор вызывающего и не участвует в его транзакции —
управляет своими соединениями сам (досье — на чтение, каждый инструмент —
своё). Единственное, что уходит "наружу" из результата — `TurnResult` с
`staged_writes` (план §3.5): commit.py (Phase 5) их ещё не применяет, но
intake.py сохраняет их в `dialog_turn.meta`, чтобы ничего не терялось молча
до появления Phase 5 (тихая потеря данных — риск №1 проекта).
"""
import concurrent.futures
import hashlib
import json
import os
import threading
import time
from typing import Optional

import httpx

from app import timeutil
from app.db import get_conn
from app.doctor import config, telegram, trace
from app.doctor.contract import StagedWrite, TurnResult
from app.doctor.context import build_dossier
from app.doctor.dialog import recent_turns
from app.doctor.prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_soft_probe, format_dossier
from app.doctor.tools import TOOLS_BY_NAME, openai_tool_schemas

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DEGRADED_REPLY = (
    "Не успел разобраться — техническая заминка на моей стороне. "
    "Если сообщение срочное — опиши ещё раз или обратись к врачу напрямую; "
    "если нет, попробуй написать чуть позже."
)

# L2 (аудит логики, 2026-09-23, КРИТИЧНО): деградированные ответы ниже
# (таймаут хода, исчерпан бюджет раундов) раньше не упоминали срочность
# вообще — "Не успел закончить разбор в срок... напиши ещё раз через минуту"
# было ЕДИНСТВЕННЫМ, что видел человек, даже если исходное сообщение было
# кризисным, а модель (effort=high, до 180с) просто не успела его оценить
# (детерминированный гейт к этому моменту УЖЕ проверил текст и пропустил его
# дальше — то есть regex ничего не нашёл, но семантику модель оценить не
# успела). Добавлен один и тот же безопасный хвост на все деградированные
# ветки — дёшево, не завязано на классификацию, и не может быть ложным:
# отправить человека к скорой/на кризисную линию "на всякий случай" не вредно,
# даже если сообщение было безобидным.
SAFETY_NET_SUFFIX = " Если это срочно или тебе плохо прямо сейчас — не жди: скорая 103, кризисная линия 8-800-2000-122."


def _today_vladivostok() -> str:
    return timeutil.today().isoformat()


def _run_tool(name: str, args: dict) -> dict:
    """Своё соединение на вызов — все read-инструменты независимы друг от друга
    и от вызывающей транзакции; write-инструменты в Phase 4 вообще не трогают
    БД (только валидация, см. tools.py)."""
    entry = TOOLS_BY_NAME.get(name)
    if entry is None:
        return {"error": f"unknown_tool: {name}"}
    try:
        with get_conn() as conn, conn.cursor() as cur:
            return entry["executor"](cur, args)
    except Exception as e:
        return {"error": "tool_failed", "detail": str(e)}


def _run_tools_parallel(tool_calls: list[dict]) -> dict[str, dict]:
    """tool_calls: [{"id", "name", "arguments"}] -> {id: result}. Каждый — со
    своим таймаутом из реестра (не общий для всего раунда)."""
    results: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(tool_calls))) as ex:
        futures = {}
        for tc in tool_calls:
            entry = TOOLS_BY_NAME.get(tc["name"])
            timeout = entry["timeout"] if entry else config.TOOL_TIMEOUT_SECONDS
            fut = ex.submit(_run_tool, tc["name"], tc["arguments"])
            futures[fut] = (tc["id"], timeout)
        for fut, (call_id, timeout) in futures.items():
            try:
                results[call_id] = fut.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                results[call_id] = {"error": "tool_timeout"}
            except Exception as e:
                results[call_id] = {"error": "tool_failed", "detail": str(e)}
    return results


def _call_model(messages: list[dict], model: str, timeout: float) -> dict:
    api_key = os.environ["OPENROUTER_API_KEY"]
    resp = httpx.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": messages,
            "tools": openai_tool_schemas(),
            "tool_choice": "auto",
            # план §3.8: "проверить живым вызовом, а не поверить памяти" — уже
            # дважды ловили уход на провайдера мимо явных параметров. Порядок
            # провайдеров — тот же белый список (Crusoe/Fireworks/BaseTen), что
            # уже используют остальные воркфлоу проекта на GLM 5.3 Flash;
            # allow_fallbacks — если ни один недоступен, обычная маршрутизация,
            # не отказ хода целиком.
            "provider": {"require_parameters": True, "order": config.DOCTOR_PROVIDER_ORDER,
                         "allow_fallbacks": True},
            # 2026-09-18: effort настраиваемый (config.DOCTOR_REASONING_EFFORT,
            # по умолчанию 'high' с переходом на полный GLM 5.3) — раньше был
            # захардкожен в 'low' под Flash, см. комментарий у DOCTOR_MODEL.
            "reasoning": {"effort": config.DOCTOR_REASONING_EFFORT},
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _collect_staged_writes(tool_results: dict[str, dict], call_by_id: dict[str, dict]) -> list[StagedWrite]:
    out = []
    for call_id, result in tool_results.items():
        if result.get("staged") and "kind" in result and "payload" in result:
            out.append(StagedWrite(kind=result["kind"], payload=result["payload"]))
    return out


def _typing_keepalive(chat_id: str, stop_event: threading.Event) -> None:
    """2026-09-18: индикатор "печатает" гаснет в Telegram сам через ~5с —
    intake.py раньше слал его РОВНО ОДИН раз перед плейсхолдером, а сам ход
    теперь может идти до TURN_DEADLINE_SECONDS (подняли вместе с переходом на
    полный GLM 5.3 + effort=high, см. config.py) — молчащий индикатор дольше
    5с выглядит как "бот завис", а не "думает". Фоновый поток, не блокирует
    основной цикл; сбой send_chat_action уже проглатывается внутри telegram.py."""
    while not stop_event.wait(config.TYPING_REFRESH_SECONDS):
        telegram.send_chat_action(chat_id, "typing")


def run_turn(*, chat_id: str, person_id: str, text: str, turn_id: str,
             model: Optional[str] = None) -> TurnResult:
    stop_event = threading.Event()
    keepalive = threading.Thread(target=_typing_keepalive, args=(chat_id, stop_event), daemon=True)
    keepalive.start()
    try:
        return _run_turn_body(chat_id=chat_id, person_id=person_id, text=text, turn_id=turn_id, model=model)
    finally:
        stop_event.set()


def _run_turn_body(*, chat_id: str, person_id: str, text: str, turn_id: str,
                    model: Optional[str] = None) -> TurnResult:
    model = model or config.DOCTOR_MODEL
    deadline = time.monotonic() + config.TURN_DEADLINE_SECONDS

    try:
        with get_conn() as conn, conn.cursor() as cur:
            dossier = build_dossier(cur, text)
            history = recent_turns(cur, chat_id)
    except Exception as e:
        return TurnResult(turn_id=turn_id, reply_text=DEGRADED_REPLY,
                           staged_writes=[], wrote_anything=False)

    # Гейт красных флагов уже отработал в intake.py до вызова этой функции
    # (L3 туда вообще не доходит) — здесь hard_flag_hit всегда False, ту же
    # семантику имел старый доктор (!redFlag.hit), просто проверка теперь
    # раньше по конвейеру, не внутри промпта.
    soft_probe = build_soft_probe(text, hard_flag_hit=False)
    dossier_text = format_dossier(dossier, _today_vladivostok())
    user_block = dossier_text
    if soft_probe:
        user_block += "\n\n" + soft_probe
    user_block += f"\n\n## 🗣 СООБЩЕНИЕ ПАЦИЕНТА:\n{text}"

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for t in history:
        role = "assistant" if t["role"] == "assistant" else "user"
        messages.append({"role": role, "content": t["text"]})
    messages.append({"role": "user", "content": user_block})

    staged_writes: list[StagedWrite] = []
    step_no = 0

    for round_no in range(config.MAX_TOOL_ROUNDS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return TurnResult(
                turn_id=turn_id,
                reply_text="Не успел закончить разбор в срок — вот что уже понятно: "
                            "напиши ещё раз через минуту, договорим." + SAFETY_NET_SUFFIX,
                staged_writes=staged_writes, wrote_anything=bool(staged_writes),
            )

        t0 = time.monotonic()
        try:
            data = _call_model(messages, model, timeout=min(remaining, config.MODEL_CALL_TIMEOUT_SECONDS))
        except Exception:
            try:
                with get_conn() as conn, conn.cursor() as cur:
                    trace.write_step(cur, turn_id=turn_id, step_no=step_no, role="model",
                                      model=model, latency_ms=int((time.monotonic() - t0) * 1000))
                    conn.commit()
            except Exception:
                pass  # трассировка — диагностика, не должна маскировать реальный сбой ниже
            return TurnResult(turn_id=turn_id, reply_text=DEGRADED_REPLY,
                               staged_writes=staged_writes, wrote_anything=bool(staged_writes))
        latency_ms = int((time.monotonic() - t0) * 1000)

        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        usage = data.get("usage", {})

        with get_conn() as conn, conn.cursor() as cur:
            trace.write_step(
                cur, turn_id=turn_id, step_no=step_no, role="model", model=model,
                tool_args={"prompt_version": PROMPT_VERSION},
                latency_ms=latency_ms,
                tokens_prompt=usage.get("prompt_tokens"),
                tokens_completion=usage.get("completion_tokens"),
                tokens_reasoning=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                cost_usd=usage.get("cost"),
            )
            conn.commit()
        step_no += 1

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            reply_text = (msg.get("content") or "").strip() or DEGRADED_REPLY
            return TurnResult(turn_id=turn_id, reply_text=reply_text,
                               staged_writes=staged_writes, wrote_anything=bool(staged_writes))

        if round_no >= config.MAX_TOOL_ROUNDS:
            # Бюджет раундов исчерпан, а модель всё ещё просит инструменты —
            # честная деградация (план §3.4), не бесконечный цикл.
            return TurnResult(
                turn_id=turn_id,
                reply_text="Не успел собрать всё нужное для точного ответа — "
                            "спроси ещё раз чуть конкретнее, договорим." + SAFETY_NET_SUFFIX,
                staged_writes=staged_writes, wrote_anything=bool(staged_writes),
            )

        parsed_calls = []
        for tc in tool_calls:
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except (json.JSONDecodeError, KeyError):
                args = {}
            parsed_calls.append({"id": tc["id"], "name": tc["function"]["name"], "arguments": args})

        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": tool_calls})

        results = _run_tools_parallel(parsed_calls)
        staged_writes.extend(_collect_staged_writes(results, {c["id"]: c for c in parsed_calls}))

        with get_conn() as conn, conn.cursor() as cur:
            for call in parsed_calls:
                result = results.get(call["id"], {"error": "no_result"})
                result_hash = hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()[:16]
                trace.write_step(
                    cur, turn_id=turn_id, step_no=step_no, role="tool",
                    tool_name=call["name"], tool_args=call["arguments"],
                    tool_result_hash=result_hash,
                )
                messages.append({
                    "role": "tool", "tool_call_id": call["id"],
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })
            conn.commit()
        step_no += 1

    return TurnResult(turn_id=turn_id, reply_text=DEGRADED_REPLY,
                       staged_writes=staged_writes, wrote_anything=bool(staged_writes))
