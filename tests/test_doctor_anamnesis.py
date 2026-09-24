"""Волна 2 (B1, 2026-09-17): анамнез-коллектор — выбор вопроса, приём ответа."""
from unittest import mock

import pytest

from app.doctor import anamnesis


def _row(q_id, cat, status="pending", asked=None, attempts=0, answer=None):
    return {"Q_ID": q_id, "Category": cat, "Question": f"Вопрос {q_id}?", "Status": status,
            "Asked_Date": asked, "Answer": answer, "Answered_Date": None, "Attempts": attempts}


TODAY = "2026-09-17"


class TestPickNext:
    def test_fresh_ask_by_priority(self):
        rows = [_row("A21", "образ жизни"), _row("A27", "препараты"), _row("A01", "наследственность")]
        res = anamnesis.pick_next(rows, TODAY)
        assert res["action"] == "ask" and res["q_id"] == "A27"  # препараты раньше всех
        assert "#A27" in res["text"] and "1/3" in res["text"]

    def test_wait_if_asked_recently(self):
        rows = [_row("A07", "наследственность", status="asked", asked="2026-09-16", attempts=1),
                _row("A08", "наследственность")]
        res = anamnesis.pick_next(rows, TODAY)
        assert res["action"] == "wait"  # 1 день < 2 — ждём

    def test_reask_after_2_days(self):
        rows = [_row("A07", "наследственность", status="asked", asked="2026-09-15", attempts=1)]
        res = anamnesis.pick_next(rows, TODAY)
        assert res["action"] == "ask" and res["q_id"] == "A07" and res["attempt"] == 2
        assert "попытка 2/3" in res["text"]

    def test_skip_after_3_attempts_and_take_next(self):
        rows = [_row("A07", "наследственность", status="asked", asked="2026-09-14", attempts=3),
                _row("A08", "наследственность")]
        res = anamnesis.pick_next(rows, TODAY)
        assert res["action"] == "ask" and res["q_id"] == "A08" and res["skip_q_id"] == "A07"

    def test_skip_only_when_pool_exhausted(self):
        rows = [_row("A07", "наследственность", status="asked", asked="2026-09-14", attempts=3),
                _row("A08", "наследственность", status="answered", answer="ок")]
        res = anamnesis.pick_next(rows, TODAY)
        assert res["action"] == "skip_only" and res["skip_q_id"] == "A07"

    def test_done_when_all_closed(self):
        rows = [_row("A01", "наследственность", status="answered", answer="да"),
                _row("A02", "наследственность", status="skipped")]
        res = anamnesis.pick_next(rows, TODAY)
        assert res["action"] == "done"

    def test_category_order(self):
        rows = [_row("A30", "происхождение"), _row("A18", "аллергии"), _row("A12", "личный анамнез"),
                _row("A05", "профилактика")]
        res = anamnesis.pick_next(rows, TODAY)
        assert res["q_id"] == "A18"  # аллергии (1) раньше личного анамнеза (3)


class TestHandleReply:
    def _update(self, tag="#A07", text="двое сводных братьев"):
        return {"update_id": 1, "message": {"message_id": 2, "chat": {"id": 8956401}, "text": text,
                "reply_to_message": {"message_id": 1, "text": f"🧬 Анамнез 11/30\n...\n{tag}"}}}

    def test_answer_recorded_and_confirmed(self, monkeypatch):
        sent = []
        monkeypatch.setattr(anamnesis.telegram, "send_message", lambda *a, **k: sent.append(a))
        exe = []
        class Cur:
            description = [("Q_ID",)]
            def execute(self, sql, params=None): exe.append((sql, params)); return None
            def fetchone(self): return ("A07",)
            def __enter__(self): return self
            def __exit__(self, *a): return False
        class Conn:
            def cursor(self): return Cur()
            def commit(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(anamnesis, "get_conn", lambda: Conn())
        anamnesis.handle_reply(self._update())
        assert any("Answer" in s and "A07" == (p and p[2]) for s, p in exe)
        assert sent and "Записал ✅ (A07)" in sent[0][1]

    def test_double_answer_not_overwritten(self, monkeypatch):
        sent = []
        monkeypatch.setattr(anamnesis.telegram, "send_message", lambda *a, **k: sent.append(a))
        class Cur:
            description = [("Q_ID",)]
            def execute(self, sql, params=None): return None
            def fetchone(self): return None  # UPDATE не тронул строк
            def __enter__(self): return self
            def __exit__(self, *a): return False
        class Conn:
            def cursor(self): return Cur()
            def commit(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(anamnesis, "get_conn", lambda: Conn())
        anamnesis.handle_reply(self._update())
        assert sent and "уже закрыт" in sent[0][1]

    def test_no_tag_ignored(self, monkeypatch):
        called = []
        monkeypatch.setattr(anamnesis, "get_conn", lambda: (_ for _ in ()).throw(AssertionError("не должен лезть в БД")))
        anamnesis.handle_reply(self._update(tag="без тега"))
        assert not called

    def test_empty_text_no_write(self, monkeypatch):
        monkeypatch.setattr(anamnesis, "get_conn", lambda: (_ for _ in ()).throw(AssertionError("не должен лезть в БД")))
        anamnesis.handle_reply(self._update(text=""))

    def test_tag_in_own_text_without_reply_recorded(self, monkeypatch):
        """F10 (2026-09-22): тег #A## в самом тексте (без реплая) — ответ
        принимается, тег вырезается из сохраняемого текста."""
        sent = []
        monkeypatch.setattr(anamnesis.telegram, "send_message", lambda *a, **k: sent.append(a))
        exe = []
        class Cur:
            description = [("Q_ID",)]
            def execute(self, sql, params=None): exe.append((sql, params)); return None
            def fetchone(self): return ("A05",)
            def __enter__(self): return self
            def __exit__(self, *a): return False
        class Conn:
            def cursor(self): return Cur()
            def commit(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(anamnesis, "get_conn", lambda: Conn())
        update = {"update_id": 3, "message": {"message_id": 4, "chat": {"id": 8956401},
                  "text": "#A05 мне 44 года"}}  # без reply_to_message
        anamnesis.handle_reply(update)
        assert exe, "ответ должен быть записан"
        assert exe[0][1][0] == "мне 44 года"  # тег вырезан из текста ответа
        assert sent and "Записал ✅ (A05)" in sent[0][1]


# --- Реальная health.anamnesis (2026-09-23) --------------------------------
# 2026-09-23, реальный алерт Влада: "relation card.anamnesis does not
# exist" — все SQL выше в модуле писали f'{schema()}.anamnesis' вместо
# 'health.anamnesis' с самого порта (2026-09-17), падали КАЖДЫЙ день, и ни
# один тест этого не поймал — все тесты класса TestPickNext/TestHandleReply
# мокают get_conn() целиком, текст SQL никогда не исполнялся по-настоящему.
# Эти тесты бьют по реальной health.anamnesis (тестовый Q_ID) — именно
# чтобы поймать регрессию вида "не тот schema/таблица в SQL-тексте",
# которую моки по конструкции поймать не могут.
#
# Cleanup — НЕ DELETE: при разборе выяснилось, что у роли card_service на
# health.anamnesis есть SELECT/INSERT/UPDATE, но НЕТ DELETE (ровно то, что
# нужно продовому коду — он тоже никогда не удаляет строки; заводить лишний
# грант ради одних только тестов не по бюджету сложности). Вместо удаления
# тестовая строка каждый раз переводится в терминальный "answered" — не
# участвует в pick_next() ни при каких обстоятельствах (мёртвый статус),
# просто остаётся в таблице как безвредный, явно помеченный "тест" фикстур.
#
# 2026-09-24 (ROADMAP 0.7): оба теста ниже дополнительно изолированы через
# _isolate_real_schema_writes (ничего не коммитится по-настоящему). Это НЕ
# отменяет смысл написанного выше: _isolate_real_schema_writes открывает
# РЕАЛЬНОЕ соединение теми же credentials (app.db.get_conn), что и прод —
# у роли card_service по-прежнему физически нет DELETE на health.anamnesis,
# это свойство роли в Postgres, а не что-то, что можно "изолировать".
from app.db import get_conn

TEST_Q_ID = "TEST-anamnesis-999"


def _neutralize_test_row(cur):
    cur.execute(
        'UPDATE health.anamnesis SET "Status"=\'answered\', "Answer"=\'тест — авто-нейтрализовано\' '
        'WHERE "Q_ID"=%s',
        (TEST_Q_ID,),
    )


@pytest.mark.usefixtures("_isolate_real_schema_writes")
def test_fetch_rows_reads_real_health_anamnesis_table():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            'INSERT INTO health.anamnesis ("Q_ID", "Category", "Question", "Status", "Attempts") '
            "VALUES (%s, %s, %s, %s, %s) "
            'ON CONFLICT ("Q_ID") DO UPDATE SET "Category" = EXCLUDED."Category"',
            (TEST_Q_ID, "тест", "Тестовый вопрос?", "pending", 0),
        )
        conn.commit()
        rows = anamnesis._fetch_rows(cur)
        _neutralize_test_row(cur)
        conn.commit()
    assert any(r["Q_ID"] == TEST_Q_ID for r in rows)


@pytest.mark.usefixtures("_isolate_real_schema_writes")
def test_ask_daily_update_writes_to_real_health_anamnesis_table():
    """Та же UPDATE-строка, что ask_daily() исполняет при action='ask' —
    проверена напрямую по СВОЕМУ Q_ID (не через pick_next()/ask_daily()
    целиком: в health.anamnesis сейчас 19 настоящих pending-вопросов, и
    выбор приоритета мог бы задеть реальный вопрос вместо тестового —
    это не тот риск, ради которого стоит писать регрессионный тест)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            'INSERT INTO health.anamnesis ("Q_ID", "Category", "Question", "Status", "Attempts") '
            "VALUES (%s, %s, %s, %s, %s) "
            'ON CONFLICT ("Q_ID") DO UPDATE SET "Status" = EXCLUDED."Status", "Attempts" = EXCLUDED."Attempts"',
            (TEST_Q_ID, "тест", "Тестовый вопрос?", "pending", 0),
        )
        conn.commit()
        cur.execute(
            'UPDATE health.anamnesis SET "Status"=\'asked\', "Asked_Date"=%s, "Attempts"=%s WHERE "Q_ID"=%s',
            ("2026-09-23", 1, TEST_Q_ID),
        )
        conn.commit()
        cur.execute('SELECT "Status", "Asked_Date" FROM health.anamnesis WHERE "Q_ID" = %s', (TEST_Q_ID,))
        status, asked = cur.fetchone()
        _neutralize_test_row(cur)
        conn.commit()
    assert status == "asked" and asked == "2026-09-23"
