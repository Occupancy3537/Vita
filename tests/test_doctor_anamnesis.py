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
