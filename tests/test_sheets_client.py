"""app/sheets_client.py — юниты на find_row_by_column/delete_row (2026-09-21,
добавлены для Food diary_v5 — порт "find row"/"del" nodes). Остальное в этом
модуле — прямые HTTP-вызовы к Google API, уже проверенные живыми вызовами
при переносе группы 3/1 (не мокается здесь, нечего мокать без реального
ответа сервера)."""
from app import sheets_client as sc


def test_find_row_by_column_returns_zero_based_data_index(monkeypatch):
    values = [
        ["Entry_ID", "Meal_description"],
        ["111", "Овсянка"],
        ["222", "Омлет"],
        ["333", "Салат"],
    ]
    monkeypatch.setattr(sc, "get_values", lambda *a, **kw: values)
    assert sc.find_row_by_column("sid", "Meals", "Entry_ID", "222") == 1


def test_find_row_by_column_none_when_not_found(monkeypatch):
    values = [["Entry_ID"], ["111"]]
    monkeypatch.setattr(sc, "get_values", lambda *a, **kw: values)
    assert sc.find_row_by_column("sid", "Meals", "Entry_ID", "999") is None


def test_find_row_by_column_none_when_column_missing(monkeypatch):
    values = [["Other_Col"], ["x"]]
    monkeypatch.setattr(sc, "get_values", lambda *a, **kw: values)
    assert sc.find_row_by_column("sid", "Meals", "Entry_ID", "1") is None


def test_find_row_by_column_none_when_sheet_empty(monkeypatch):
    monkeypatch.setattr(sc, "get_values", lambda *a, **kw: [])
    assert sc.find_row_by_column("sid", "Meals", "Entry_ID", "1") is None


def test_delete_row_sends_correct_delete_dimension_request(monkeypatch):
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResp()

    monkeypatch.setattr(sc, "_get_access_token", lambda kind: "tok")
    monkeypatch.setattr(sc.httpx, "post", fake_post)

    sc.delete_row("sid123", 403788598, data_row_index=1)

    assert "sid123:batchUpdate" in captured["url"]
    req = captured["json"]["requests"][0]["deleteDimension"]["range"]
    assert req["sheetId"] == 403788598
    assert req["dimension"] == "ROWS"
    assert req["startIndex"] == 2  # data_row_index(1) + 1 за шапку
    assert req["endIndex"] == 3
