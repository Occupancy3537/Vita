"""Follow-up по открытой сессии красного флага (бриф Влада, 2026-09-30) —
tests/app/redflag_followup.py::run_once.

Условия чистоты: курсор — card_test (зеркало card, чистится между тестами,
см. conftest::TABLES_TO_CLEAN), доставка — фикстура-двойник fake_deliver
(мок ТОЛЬКО через фикстуру, бриф), id сессий — синтетические rfs_TEST…;
боевые card.*/health.* по конструкции не пишутся (_isolate_real_schema_writes).
Время детерминировано: run_once принимает now, RF_FOLLOWUP_SINCE — через
monkeypatch.setenv. Требует migrations/0006, применённой на card_test
(см. заголовок миграции)."""
from datetime import datetime, timedelta, timezone

import pytest
from psycopg import sql

from app import redflag_followup as rff
from app.db import get_conn, schema

pytestmark = pytest.mark.usefixtures("_isolate_real_schema_writes")

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
SINCE = "2026-09-30T00:00:00+00:00"
SINCE_DT = datetime.fromisoformat(SINCE)


@pytest.fixture
def fake_deliver():
    """Двойник doctor/intake._deliver_emergency: тот же контракт
    (chat_id, message_id, text) -> bool, никогда не бросает."""
    state = {"ok": True}

    def _deliver(chat_id, message_id, text):
        _deliver.calls.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return state["ok"]

    _deliver.calls = []
    _deliver.set_ok = lambda ok: state.__setitem__("ok", ok)
    return _deliver


@pytest.fixture
def db_cur():
    with get_conn() as conn, conn.cursor() as cur:
        yield cur
        conn.rollback()  # _NoCommitConnection всё равно не коммитит — явно, для читаемости


def _add_session(db_cur, session_id, category="severe_pain", worst="L2", status="open",
                 opened=None, last_activity=None):
    db_cur.execute(
        sql.SQL("INSERT INTO {t} (id, category, opened_ts, last_activity, worst_level, status) "
                "VALUES (%s, %s, %s, %s, %s, %s)").format(t=sql.Identifier(schema(), "rf_session")),
        (session_id, category,
         opened or SINCE_DT + timedelta(hours=1),  # по умолчанию открыта ПОСЛЕ эпохи — каждый кейс проверяет свой фильтр
         last_activity or NOW - timedelta(days=3),
         worst, status),
    )


def _followup_row(db_cur, session_id):
    db_cur.execute(
        sql.SQL("SELECT sent_ts, attempts, last_error FROM {t} WHERE session_id = %s").format(
            t=sql.Identifier(schema(), "rf_followup")),
        (session_id,),
    )
    return db_cur.fetchone()


def test_fresh_session_not_messaged(db_cur, fake_deliver, monkeypatch):
    monkeypatch.setenv("RF_FOLLOWUP_SINCE", SINCE)
    # открыта после эпохи, но молчит всего 1 ч — окно тишины (48 ч) не набрано
    _add_session(db_cur, "rfs_TEST_fresh", opened=SINCE_DT + timedelta(hours=1),
                 last_activity=NOW - timedelta(hours=1))
    res = rff.run_once(db_cur, fake_deliver, now=NOW)
    assert fake_deliver.calls == []
    assert res["sent"] == 0 and res["considered"] == 0


def test_session_opened_before_epoch_not_messaged(db_cur, fake_deliver, monkeypatch):
    monkeypatch.setenv("RF_FOLLOWUP_SINCE", SINCE)
    # открыта ДО включения фичи (старая сессия от 15.09 — тот же класс), молчит давно
    _add_session(db_cur, "rfs_TEST_old", opened=SINCE_DT - timedelta(days=1),
                 last_activity=NOW - timedelta(days=2))
    res = rff.run_once(db_cur, fake_deliver, now=NOW)
    assert fake_deliver.calls == []
    assert res["sent"] == 0


def test_l1_and_closed_not_messaged(db_cur, fake_deliver, monkeypatch):
    monkeypatch.setenv("RF_FOLLOWUP_SINCE", SINCE)
    _add_session(db_cur, "rfs_TEST_l1", worst="L1", last_activity=NOW - timedelta(days=3))
    _add_session(db_cur, "rfs_TEST_closed", status="closed", last_activity=NOW - timedelta(days=3))
    res = rff.run_once(db_cur, fake_deliver, now=NOW)
    assert fake_deliver.calls == []
    assert res["sent"] == 0


def test_eligible_sent_once_then_never(db_cur, fake_deliver, monkeypatch):
    monkeypatch.setenv("RF_FOLLOWUP_SINCE", SINCE)
    _add_session(db_cur, "rfs_TEST_ok", category="cardiac_acute", worst="L2",
                 last_activity=NOW - timedelta(days=2))
    res = rff.run_once(db_cur, fake_deliver, now=NOW)
    assert res["sent"] == 1 and len(fake_deliver.calls) == 1
    call = fake_deliver.calls[0]
    assert call["chat_id"] == rff.CHAT_ID and call["message_id"] is None
    # человеческая категория, не сырой ключ
    assert "боль в груди" in call["text"] and "cardiac_acute" not in call["text"]
    sent_ts, attempts, last_error = _followup_row(db_cur, "rfs_TEST_ok")
    assert sent_ts is not None and attempts == 1 and last_error is None

    # второй прогон: follow-up уже отправлен — тишина
    res2 = rff.run_once(db_cur, fake_deliver, now=NOW + timedelta(minutes=5))
    assert res2["sent"] == 0 and res2["considered"] == 0
    assert len(fake_deliver.calls) == 1


def test_failed_delivery_attempts_capped(db_cur, fake_deliver, monkeypatch):
    monkeypatch.setenv("RF_FOLLOWUP_SINCE", SINCE)
    _add_session(db_cur, "rfs_TEST_fail", last_activity=NOW - timedelta(days=2))
    fake_deliver.set_ok(False)

    for i in range(1, rff.MAX_ATTEMPTS + 1):
        res = rff.run_once(db_cur, fake_deliver, now=NOW + timedelta(minutes=i))
        assert res["failed"] == 1 and res["sent"] == 0
        sent_ts, attempts, last_error = _followup_row(db_cur, "rfs_TEST_fail")
        assert sent_ts is None and attempts == i and last_error is not None
    assert len(fake_deliver.calls) == rff.MAX_ATTEMPTS

    # после MAX_ATTEMPTS сессия больше не рассматривается — попыток больше нет
    res = rff.run_once(db_cur, fake_deliver, now=NOW + timedelta(minutes=99))
    assert res["sent"] == 0 and res["considered"] == 0
    assert len(fake_deliver.calls) == rff.MAX_ATTEMPTS


def test_disabled_without_since(db_cur, fake_deliver, monkeypatch):
    monkeypatch.delenv("RF_FOLLOWUP_SINCE", raising=False)
    _add_session(db_cur, "rfs_TEST_disabled", last_activity=NOW - timedelta(days=2))
    res = rff.run_once(db_cur, fake_deliver, now=NOW)
    assert "skipped" in res and res.get("sent", 0) == 0
    assert fake_deliver.calls == []


def test_message_never_contains_raw_category_key(db_cur, fake_deliver, monkeypatch):
    monkeypatch.setenv("RF_FOLLOWUP_SINCE", SINCE)
    # неизвестная словарю категория — общая формулировка, не сырой ключ
    _add_session(db_cur, "rfs_TEST_unknown", category="mystery_category",
                 last_activity=NOW - timedelta(days=2))
    res = rff.run_once(db_cur, fake_deliver, now=NOW)
    assert res["sent"] == 1
    text = fake_deliver.calls[0]["text"]
    assert "mystery_category" not in text
    assert rff.CATEGORY_DEFAULT in text
    # и известная категория — тоже без сырого ключа
    assert "Два дня назад ты писал про" in text
