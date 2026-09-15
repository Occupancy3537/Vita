"""
Phase 6 плана нового доктора (NEW_DOCTOR_PLAN_2026-09-15.md §4, "Проверка на
корпусе") — оффлайн head-to-head замер моделей на 26 реальных жалобах из
health.symptom_log (tests/fixtures/golden_dialogs/symptom_corpus.json).

НЕ pytest — разовый инструмент замера (делает настоящие платные вызовы
OpenRouter), не гейт CI. Запуск:
    cd card-service && source .venv/bin/activate
    CARD_PG_HOST=127.0.0.1 CARD_PG_PORT=5432 CARD_PG_USER=card_service \
    CARD_PG_PASSWORD=... CARD_PG_DATABASE=health OPENROUTER_API_KEY=... \
    python3 scripts/eval_doctor_corpus.py

Важно: НЕ вызывает commit.py — только loop.run_turn(), запись остаётся
StagedWrite (не долетает до health.*). Каждый прогон создаёт свой изолированный
card.dialog_turn с чат-id "eval-<model>-<symptom_id>" — не пересекается с
реальным чатом Влада (8956401) и не требует отдельной уборки health.*.
"""
import concurrent.futures
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_conn  # noqa: E402
from app.doctor.dialog import write_turn  # noqa: E402
from app.doctor import loop  # noqa: E402

CORPUS_PATH = Path(__file__).resolve().parent.parent / "tests/fixtures/golden_dialogs/symptom_corpus.json"
MODELS = ["google/gemini-3.8-flash", "anthropic/claude-haiku-4.5"]
MAX_WORKERS = 6


def load_corpus() -> list[dict]:
    return json.loads(CORPUS_PATH.read_text())


def make_eval_turn(model: str, symptom_id: str, update_id: int) -> tuple[str, str]:
    chat_id = f"eval-{model.replace('/', '_')}"
    with get_conn() as conn, conn.cursor() as cur:
        turn_id = write_turn(cur, chat_id=chat_id, update_id=update_id, role="user", text=symptom_id)
        conn.commit()
    return chat_id, turn_id


def run_one(model: str, item: dict, update_id: int) -> dict:
    chat_id, turn_id = make_eval_turn(model, item["symptom_id"], update_id)
    t0 = time.monotonic()
    try:
        result = loop.run_turn(chat_id=chat_id, person_id="self", text=item["symptom"],
                                turn_id=turn_id, model=model)
        error = None
    except Exception as e:  # честная деградация — но замер должен знать, что цикл упал
        result = None
        error = repr(e)
    elapsed = time.monotonic() - t0

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT sum(latency_ms), sum(tokens_prompt), sum(tokens_completion), sum(cost_usd), "
            "count(*) FILTER (WHERE role='tool') "
            "FROM card.agent_step WHERE turn_id = %s", (turn_id,),
        )
        latency_ms, tok_p, tok_c, cost, tool_calls = cur.fetchone()

    return {
        "symptom_id": item["symptom_id"], "model": model, "turn_id": turn_id,
        "input": item["symptom"], "old_hypothesis": item.get("hypothesis"), "old_notes": item.get("notes"),
        "reply": result.reply_text if result else None,
        "staged_writes": [w.model_dump() for w in result.staged_writes] if result else [],
        "error": error,
        "elapsed_s": round(elapsed, 1),
        "latency_ms_sum": latency_ms, "tokens_prompt": tok_p, "tokens_completion": tok_c,
        "cost_usd": float(cost) if cost is not None else None, "tool_calls": tool_calls,
        "reply_len": len(result.reply_text) if result else 0,
    }


def compliance_flags(row: dict) -> list[str]:
    flags = []
    reply = row["reply"] or ""
    if row["error"]:
        flags.append("ERROR")
        return flags
    is_question = "?" in reply and len(reply) < 600
    limit = 500 if is_question else 1400
    if len(reply) > limit:
        flags.append(f"over_limit({len(reply)}>{limit})")
    if is_question and row["staged_writes"]:
        flags.append("wrote_on_question_turn")  # железное правило: вопросы -> без записи
    for bad_word in ["мг", "мл ", "таблетк", "мкг"]:
        if bad_word in reply.lower():
            flags.append(f"possible_dose_mention:{bad_word.strip()}")
            break
    open_tags = reply.count("<b>") + reply.count("<i>")
    close_tags = reply.count("</b>") + reply.count("</i>")
    if open_tags != close_tags:
        flags.append("unbalanced_html")
    return flags


def main():
    corpus = load_corpus()
    print(f"corpus: {len(corpus)} items x {len(MODELS)} models = {len(corpus) * len(MODELS)} calls")

    jobs = []
    update_id = 1
    for model in MODELS:
        for item in corpus:
            jobs.append((model, item, update_id))
            update_id += 1

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(run_one, model, item, uid): (model, item["symptom_id"]) for model, item, uid in jobs}
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            model, sid = futures[fut]
            try:
                row = fut.result()
            except Exception as e:
                row = {"symptom_id": sid, "model": model, "error": repr(e), "reply": None,
                       "staged_writes": [], "elapsed_s": None, "reply_len": 0, "tool_calls": None,
                       "cost_usd": None, "tokens_prompt": None, "tokens_completion": None,
                       "latency_ms_sum": None, "input": "", "old_hypothesis": None, "old_notes": None,
                       "turn_id": None}
            row["flags"] = compliance_flags(row)
            results.append(row)
            print(f"[{i}/{len(jobs)}] {model} / {sid} — {row['elapsed_s']}s, flags={row['flags']}")

    out_path = Path(__file__).resolve().parent.parent / "eval_results.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\nwrote {len(results)} rows to {out_path}")

    for model in MODELS:
        rows = [r for r in results if r["model"] == model]
        errors = sum(1 for r in rows if r.get("error"))
        clean = sum(1 for r in rows if not r.get("flags"))
        avg_elapsed = sum(r["elapsed_s"] for r in rows if r.get("elapsed_s")) / max(1, len(rows) - errors)
        total_cost = sum(r["cost_usd"] for r in rows if r.get("cost_usd"))
        print(f"\n=== {model} ===")
        print(f"  errors: {errors}/{len(rows)}, clean (no flags): {clean}/{len(rows)}")
        print(f"  avg elapsed: {avg_elapsed:.1f}s, total cost: ${total_cost:.4f}")


if __name__ == "__main__":
    main()
