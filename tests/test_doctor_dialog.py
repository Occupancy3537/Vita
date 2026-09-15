"""Phase 1 плана нового доктора — card.dialog_turn: идемпотентность, окно сессии,
turn_index. Против card_test (conftest.py), не прод."""
from app.db import get_conn
from app.doctor.dialog import already_processed, next_turn_index, recent_turns, write_turn


def test_write_and_read_turn():
    with get_conn() as conn, conn.cursor() as cur:
        turn_id = write_turn(cur, chat_id="123", update_id=1, role="user", text="болит голова")
        conn.commit()
    assert turn_id.startswith("dt_")

    with get_conn() as conn, conn.cursor() as cur:
        turns = recent_turns(cur, "123")
    assert len(turns) == 1
    assert turns[0]["text"] == "болит голова"
    assert turns[0]["role"] == "user"
    assert turns[0]["turn_index"] == 0


def test_turn_index_increments_per_chat():
    with get_conn() as conn, conn.cursor() as cur:
        write_turn(cur, chat_id="123", update_id=1, role="user", text="A")
        write_turn(cur, chat_id="123", update_id=None, role="assistant", text="B")
        idx = next_turn_index(cur, "123")
        conn.commit()
    assert idx == 2  # два хода уже есть, следующий — 2


def test_turn_index_independent_per_chat():
    with get_conn() as conn, conn.cursor() as cur:
        write_turn(cur, chat_id="111", update_id=1, role="user", text="A")
        write_turn(cur, chat_id="111", update_id=2, role="user", text="B")
        idx_other_chat = next_turn_index(cur, "222")
        conn.commit()
    assert idx_other_chat == 0  # другой chat_id — своя нумерация с нуля


def test_already_processed_true_after_write():
    with get_conn() as conn, conn.cursor() as cur:
        assert already_processed(cur, "123", 42) is False
        write_turn(cur, chat_id="123", update_id=42, role="user", text="дубль-тест")
        conn.commit()

    with get_conn() as conn, conn.cursor() as cur:
        assert already_processed(cur, "123", 42) is True


def test_already_processed_none_update_id_always_false():
    """Ходы ассистента (update_id=None) не участвуют в идемпотентности Телеграма —
    их может быть сколько угодно, дубль-проверка тут бессмысленна по определению."""
    with get_conn() as conn, conn.cursor() as cur:
        assert already_processed(cur, "123", None) is False


def test_duplicate_update_id_same_chat_rejected_by_unique_index():
    """Страховка на уровне БД (dialog_turn_update_idx), а не только явная проверка
    already_processed — план §3.3, идемпотентность по update_id."""
    import psycopg

    with get_conn() as conn, conn.cursor() as cur:
        write_turn(cur, chat_id="999", update_id=7, role="user", text="первый")
        conn.commit()

    with get_conn() as conn, conn.cursor() as cur:
        try:
            write_turn(cur, chat_id="999", update_id=7, role="user", text="дубль")
            conn.commit()
            assert False, "ожидалась ошибка уникального индекса"
        except psycopg.errors.UniqueViolation:
            conn.rollback()


def test_recent_turns_chronological_order():
    with get_conn() as conn, conn.cursor() as cur:
        write_turn(cur, chat_id="555", update_id=1, role="user", text="первое")
        write_turn(cur, chat_id="555", update_id=None, role="assistant", text="ответ")
        write_turn(cur, chat_id="555", update_id=2, role="user", text="второе")
        conn.commit()

    with get_conn() as conn, conn.cursor() as cur:
        turns = recent_turns(cur, "555")
    assert [t["text"] for t in turns] == ["первое", "ответ", "второе"]


def test_recent_turns_respects_limit():
    with get_conn() as conn, conn.cursor() as cur:
        for i in range(5):
            write_turn(cur, chat_id="777", update_id=i, role="user", text=f"msg{i}")
        conn.commit()

    with get_conn() as conn, conn.cursor() as cur:
        turns = recent_turns(cur, "777", limit=2)
    assert len(turns) == 2
    assert [t["text"] for t in turns] == ["msg3", "msg4"]  # последние по времени, в хронологии
