"""Phase 5 плана нового доктора — commit.py: транзакционная запись health.*+
card.*. 2026-09-21 (#38/#47): commit.py теперь пишет в REGISTRAR_HEALTH_SCHEMA
(тот же переключатель, что и app/registrar.py), conftest.py задаёт card_test и
создаёт двойники symptom_log/doctor_notes/investigations/lab_plan — прод
health.* эти тесты больше не касаются вообще. (Раньше писали реальные строки
с префиксом test-commit- и убирали в teardown — "известные 5 падений" держались
именно из-за этого: тестовый Open_Investigation конфликтовал с настоящей
открытой записью Влада в health.investigations, инвариант "только одно
открытое" отрабатывал корректно, просто не на той базе.) card.*
(episode/fact/journal) — через conftest.py, автоматически truncate'ится."""
import pytest

from app.db import get_conn
from app.doctor.commit import _HEALTH_SCHEMA, CommitError, already_committed, apply_staged_writes
from app.doctor.contract import StagedWrite
from app.doctor.dialog import write_turn

_next_update_id = iter(range(1, 100_000))


def _turn() -> str:
    """update_id уникален по (chat_id, update_id) — счётчик, не константа: тесты
    транзакционности вызывают _turn() по несколько раз за один прогон."""
    with get_conn() as conn, conn.cursor() as cur:
        turn_id = write_turn(cur, chat_id="900", update_id=next(_next_update_id), role="user", text="тест")
        conn.commit()
    return turn_id


def test_empty_staged_writes_is_noop():
    r = apply_staged_writes([], turn_id=_turn())
    assert r == {"committed": False, "reason": "nothing_to_write"}


def test_symptom_write_creates_symptom_log_and_episode():
    turn_id = _turn()
    sw = [StagedWrite(kind="symptom", payload={
        "symptom_id": "test-commit-sid1", "symptom": "тестовая боль",
        "system": "ОДА", "severity": 3, "status": "active",
    })]
    r = apply_staged_writes(sw, turn_id=turn_id)
    assert r["committed"] is True
    assert r["applied"] == ["symptom"]

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT symptom, severity, status FROM {_HEALTH_SCHEMA}.symptom_log WHERE symptom_id = 'test-commit-sid1'")
        row = cur.fetchone()
        # severity — колонка TEXT (как и в остальной health.*, см. Phase 0), не integer.
        assert row == ("тестовая боль", "3", "active")

        cur.execute("SELECT count(*) FROM card_test.episode WHERE symptom_key = 'test-commit-sid1'")
        assert cur.fetchone()[0] == 1


def test_symptom_resolved_status_closes_episode():
    turn_id1 = _turn()
    apply_staged_writes([StagedWrite(kind="symptom", payload={
        "symptom_id": "test-commit-sid2", "symptom": "тест", "status": "active",
    })], turn_id=turn_id1)

    turn_id2 = _turn()
    apply_staged_writes([StagedWrite(kind="symptom", payload={
        "symptom_id": "test-commit-sid2", "symptom": "тест", "status": "resolved",
    })], turn_id=turn_id2)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM card_test.episode WHERE symptom_key = 'test-commit-sid2'")
        assert cur.fetchone()[0] == "resolved"


def test_note_write():
    turn_id = _turn()
    sw = [StagedWrite(kind="note", payload={"category": "test-commit-cat", "note": "тестовая заметка"})]
    r = apply_staged_writes(sw, turn_id=turn_id)
    assert r["committed"] is True

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT note FROM {_HEALTH_SCHEMA}.doctor_notes WHERE category = 'test-commit-cat'")
        assert cur.fetchone()[0] == "тестовая заметка"


def test_open_investigation_when_none_open():
    turn_id = _turn()
    sw = [StagedWrite(kind="investigation_open",
                       payload={"inv_id": "test-commit-inv1", "trigger": "тест"})]
    r = apply_staged_writes(sw, turn_id=turn_id)
    assert r["committed"] is True

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT status FROM {_HEALTH_SCHEMA}.investigations WHERE inv_id = 'test-commit-inv1'")
        assert cur.fetchone()[0] == "open"


def test_open_investigation_rejected_when_one_already_open():
    apply_staged_writes([StagedWrite(kind="investigation_open",
                                      payload={"inv_id": "test-commit-inv2", "trigger": "первое"})],
                         turn_id=_turn())

    turn_id2 = _turn()
    with pytest.raises(CommitError):
        apply_staged_writes([StagedWrite(kind="investigation_open",
                                          payload={"inv_id": "test-commit-inv3", "trigger": "второе"})],
                             turn_id=turn_id2)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {_HEALTH_SCHEMA}.investigations WHERE inv_id = 'test-commit-inv3'")
        assert cur.fetchone()[0] == 0  # откат — вторая запись не появилась


def test_update_investigation_success():
    apply_staged_writes([StagedWrite(kind="investigation_open",
                                      payload={"inv_id": "test-commit-inv4", "trigger": "тест"})],
                         turn_id=_turn())
    apply_staged_writes([StagedWrite(kind="investigation_update",
                                      payload={"inv_id": "test-commit-inv4", "findings": "новые данные"})],
                         turn_id=_turn())

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT findings FROM {_HEALTH_SCHEMA}.investigations WHERE inv_id = 'test-commit-inv4'")
        assert cur.fetchone()[0] == "новые данные"


def test_update_investigation_no_matching_open_raises():
    with pytest.raises(CommitError):
        apply_staged_writes([StagedWrite(kind="investigation_update",
                                          payload={"inv_id": "test-commit-does-not-exist"})],
                             turn_id=_turn())


def test_close_investigation_success():
    apply_staged_writes([StagedWrite(kind="investigation_open",
                                      payload={"inv_id": "test-commit-inv5", "trigger": "тест"})],
                         turn_id=_turn())
    apply_staged_writes([StagedWrite(kind="investigation_close",
                                      payload={"inv_id": "test-commit-inv5", "doctor_brief": "итог"})],
                         turn_id=_turn())

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT status, doctor_brief FROM {_HEALTH_SCHEMA}.investigations WHERE inv_id = 'test-commit-inv5'")
        assert cur.fetchone() == ("report_ready", "итог")

    # лимит освобождён — новое открытие теперь возможно
    apply_staged_writes([StagedWrite(kind="investigation_open",
                                      payload={"inv_id": "test-commit-inv6", "trigger": "тест2"})],
                         turn_id=_turn())
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT status FROM {_HEALTH_SCHEMA}.investigations WHERE inv_id = 'test-commit-inv6'")
        assert cur.fetchone()[0] == "open"


def test_close_investigation_no_matching_open_raises():
    with pytest.raises(CommitError):
        apply_staged_writes([StagedWrite(kind="investigation_close",
                                          payload={"inv_id": "test-commit-does-not-exist"})],
                             turn_id=_turn())


def test_plan_lab_with_interval_computes_next_due():
    turn_id = _turn()
    sw = [StagedWrite(kind="lab_plan", payload={"test": "test-commit-glucose", "interval_months": 3})]
    r = apply_staged_writes(sw, turn_id=turn_id)
    assert r["committed"] is True

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f'SELECT "Status", "Next_Due" FROM {_HEALTH_SCHEMA}.lab_plan WHERE "Test" = \'test-commit-glucose\'')
        status, next_due = cur.fetchone()
        assert status == "active"
        assert next_due is not None and len(next_due) == 10  # YYYY-MM-DD


def test_plan_lab_without_interval_next_due_is_null():
    sw = [StagedWrite(kind="lab_plan", payload={"test": "test-commit-onetime"})]
    apply_staged_writes(sw, turn_id=_turn())

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f'SELECT "Next_Due" FROM {_HEALTH_SCHEMA}.lab_plan WHERE "Test" = \'test-commit-onetime\'')
        assert cur.fetchone()[0] is None


def test_unknown_kind_raises_and_nothing_commits():
    turn_id = _turn()
    bad = StagedWrite.model_construct(kind="not_a_real_kind", payload={})
    with pytest.raises(CommitError):
        apply_staged_writes([StagedWrite(kind="note", payload={"category": "test-commit-x", "note": "n"}), bad],
                             turn_id=turn_id)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {_HEALTH_SCHEMA}.doctor_notes WHERE category = 'test-commit-x'")
        assert cur.fetchone()[0] == 0  # первый write отменён откатом


def test_transactionality_third_write_fails_nothing_persists():
    """Приёмка Phase 5: конверт с несколькими записями, падение на одной из
    них — ничего не остаётся, не только последняя. Третий элемент батча —
    заведомо конфликтующее Open_Investigation (invariant), symptom/note до
    него должны откатиться вместе с ним."""
    turn_id = _turn()
    apply_staged_writes([StagedWrite(kind="investigation_open",
                                      payload={"inv_id": "test-commit-blocker", "trigger": "уже открыто"})],
                         turn_id=_turn())

    batch = [
        StagedWrite(kind="symptom", payload={"symptom_id": "test-commit-tx-sid", "symptom": "тест"}),
        StagedWrite(kind="note", payload={"category": "test-commit-tx-note", "note": "тест"}),
        StagedWrite(kind="investigation_open", payload={"inv_id": "test-commit-tx-inv", "trigger": "тест"}),
    ]
    with pytest.raises(CommitError):
        apply_staged_writes(batch, turn_id=turn_id)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {_HEALTH_SCHEMA}.symptom_log WHERE symptom_id = 'test-commit-tx-sid'")
        assert cur.fetchone()[0] == 0
        cur.execute(f"SELECT count(*) FROM {_HEALTH_SCHEMA}.doctor_notes WHERE category = 'test-commit-tx-note'")
        assert cur.fetchone()[0] == 0
        cur.execute(f"SELECT count(*) FROM {_HEALTH_SCHEMA}.investigations WHERE inv_id = 'test-commit-tx-inv'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM card_test.episode WHERE symptom_key = 'test-commit-tx-sid'")
        assert cur.fetchone()[0] == 0

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM {_HEALTH_SCHEMA}.investigations WHERE inv_id = 'test-commit-blocker'")
        conn.commit()


def test_idempotent_by_turn_id_second_call_is_noop():
    turn_id = _turn()
    sw = [StagedWrite(kind="note", payload={"category": "test-commit-idem", "note": "первый раз"})]

    r1 = apply_staged_writes(sw, turn_id=turn_id)
    assert r1["committed"] is True

    r2 = apply_staged_writes(sw, turn_id=turn_id)  # тот же turn_id — повтор
    assert r2 == {"committed": False, "reason": "already_committed"}

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {_HEALTH_SCHEMA}.doctor_notes WHERE category = 'test-commit-idem'")
        assert cur.fetchone()[0] == 1  # не задвоилось


def test_already_committed_true_after_note_only_batch():
    """Маркер идемпотентности не должен зависеть от наличия symptom-записи в
    батче — note/lab_plan-only коммит тоже обязан быть проверяемым."""
    turn_id = _turn()
    apply_staged_writes([StagedWrite(kind="note", payload={"category": "test-commit-marker", "note": "n"})],
                         turn_id=turn_id)

    with get_conn() as conn, conn.cursor() as cur:
        assert already_committed(cur, turn_id) is True


def test_note_dates_are_vladivostok_not_utc():
    """T1 (внешний аудит логики, 2026-09-22): note_date — VL-день момента записи,
    а не CURRENT_DATE (UTC): до 10:00 по Владивостоку UTC-дата ещё вчерашняя
    (живое доказательство: doctor_notes id 135/73, AGENT_SYNC #57)."""
    turn_id = _turn()
    apply_staged_writes([StagedWrite(kind="note", payload={"category": "test-commit-t1", "note": "дата"})],
                         turn_id=turn_id)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT note_date, (created_at AT TIME ZONE 'Asia/Vladivostok')::date "
            f"FROM {_HEALTH_SCHEMA}.doctor_notes WHERE category = 'test-commit-t1'"
        )
        note_date, vl_created = cur.fetchone()
    assert note_date == vl_created


def test_investigation_and_lab_plan_dates_are_vladivostok():
    """T1 продолжение: opened/updated расследования и Next_Due плана — тоже VL."""
    apply_staged_writes([StagedWrite(kind="investigation_open",
                                      payload={"inv_id": "test-commit-t1-inv", "trigger": "тест"})],
                         turn_id=_turn())
    apply_staged_writes([StagedWrite(kind="lab_plan",
                                      payload={"test": "test-commit-t1-lab", "interval_months": 3})],
                         turn_id=_turn())

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT opened, (created_at AT TIME ZONE 'Asia/Vladivostok')::date "
            f"FROM {_HEALTH_SCHEMA}.investigations WHERE inv_id = 'test-commit-t1-inv'"
        )
        opened, vl_created = cur.fetchone()
        assert opened == vl_created

        cur.execute(f'SELECT "Next_Due" FROM {_HEALTH_SCHEMA}.lab_plan WHERE "Test" = \'test-commit-t1-lab\'')
        next_due = cur.fetchone()[0]
        # Next_Due = VL-сегодня + 3 месяца; проверяем диапазон, чтобы тест не
        # зависел от календарной даты прогона
        assert next_due is not None and len(next_due) == 10
