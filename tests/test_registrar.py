"""Волна 3 (B2, 2026-09-18): регистратор лаб-документов — классификация-ветвление,
маппинг маркеров (порт ноды Map), запись через внутренние функции /visits/sync +
/labs/result (идемпотентность), честные отказы. LLM/httpx/telegram мокаются;
запись идёт в card_test через настоящие app.main.visits_sync/labs_result_sync."""
from unittest import mock

import pytest

from app import registrar
from app.db import get_conn, schema

MARKER_ROWS = [
    {"Marker_ID": "M041", "Name": "Гемоглобин"},
    {"Marker_ID": "M003", "Name": "Глюкоза"},
    {"Marker_ID": "M058", "Name": "Нейтрофилы (абс)"},
    {"Marker_ID": "M059", "Name": "Нейтрофилы (%)"},
]

DOC_MARKERS = [
    {"name": "Гемоглобин", "value": "145", "unit": "г/л", "ref_low": "130", "ref_high": "160"},
    {"name": "Глюкоза", "value": "5.2", "unit": "ммоль/л", "ref_low": "4.1", "ref_high": "5.9"},
    {"name": "Какой-то экзотический маркер", "value": "999", "unit": "ед", "ref_low": "", "ref_high": ""},
]


def _photo_update(file_id="ph1", caption=None):
    msg = {"message_id": 5, "chat": {"id": 8956401}, "photo": [{"file_id": "small"}, {"file_id": file_id}]}
    if caption:
        msg["caption"] = caption
    return {"update_id": 100, "message": msg}


def _doc_update(filename="analysis.pdf"):
    return {"update_id": 101, "message": {"message_id": 6, "chat": {"id": 8956401},
            "document": {"file_id": "d1", "file_name": filename}}}


@pytest.fixture()
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(registrar.telegram, "send_message", lambda chat_id, text, **k: out.append((chat_id, text)))
    return out


@pytest.fixture()
def lab_doc(monkeypatch):
    """Классификатор — lab_report, извлечение — фиксированный документ, маркеры — фиксированный справочник."""
    monkeypatch.setattr(registrar, "classify_document", lambda content, mime: {"kind": "lab_report", "reason": ""})
    monkeypatch.setattr(
        registrar, "extract_document",
        lambda content, mime, today: {"document_date": "2026.09.15", "lab_name": "Инвитро",
                                      "notes": "биохимия", "markers": [dict(m) for m in DOC_MARKERS]})
    monkeypatch.setattr(registrar, "_fetch_marker_rows", lambda: MARKER_ROWS)
    monkeypatch.setattr(registrar.telegram, "download_file", lambda file_id, timeout=20.0: b"fake-image")


# ───────────────────────── чистая логика (порт ноды Map) ─────────────────────────

class TestParseDocumentDate:
    def test_dd_mm_yyyy_and_iso_order(self):
        assert registrar.parse_document_date("15.09.2026", "2026-09-18") == "2026-09-15"
        assert registrar.parse_document_date("2026.09.15", "2026-09-18") == "2026-09-15"

    def test_slashes_and_dashes_normalized(self):
        assert registrar.parse_document_date("15/09/2026", "2026-09-18") == "2026-09-15"
        assert registrar.parse_document_date("15-09-2026", "2026-09-18") == "2026-09-15"

    def test_future_date_rejected(self):
        # дата из будущего — мусорная экстракция, подставится сегодня (как в n8n Build Visit)
        assert registrar.parse_document_date("15.09.2027", "2026-09-18") is None

    def test_empty_and_garbage(self):
        assert registrar.parse_document_date("", "2026-09-18") is None
        assert registrar.parse_document_date("бланк №12345", "2026-09-18") is None


class TestMapMarkers:
    def test_recognized_unrecognized_and_qualitative(self):
        res = registrar.map_markers(DOC_MARKERS, MARKER_ROWS)
        assert [r["marker_id"] for r in res["rows"]] == ["M041", "M003"]
        assert res["rows"][0]["value_num"] == 145.0 and res["rows"][0]["ref_min"] == 130.0
        assert res["unmatched"] == ["Какой-то экзотический маркер"]

    def test_qualitative_value_skipped(self):
        items = [{"name": "Гемоглобин", "value": "отрицательно", "unit": ""}]
        assert registrar.map_markers(items, MARKER_ROWS)["rows"] == []

    def test_unit_conversion_glucose_mgdl(self):
        items = [{"name": "Глюкоза", "value": "94", "unit": "мг/дл"}]
        res = registrar.map_markers(items, MARKER_ROWS)
        assert res["rows"][0]["value_num"] == pytest.approx(94 / 18.0, abs=0.01)
        assert "ммоль/л" in res["rows"][0]["unit"]

    def test_unit_conversion_creatinine_mgdl(self):
        items = [{"name": "Креатинин", "value": "1.0", "unit": "mg/dl"}]
        res = registrar.map_markers(items, MARKER_ROWS)
        assert res["rows"][0]["value_num"] == pytest.approx(88.4, abs=0.01)

    def test_leukocyte_pct_vs_abs_by_unit(self):
        pct = registrar.map_markers([{"name": "Нейтрофилы", "value": "45", "unit": "%"}], MARKER_ROWS)
        assert pct["rows"][0]["marker_id"] == "M059"
        ab = registrar.map_markers([{"name": "Нейтрофилы", "value": "2.5", "unit": "10^9/л"}], MARKER_ROWS)
        assert ab["rows"][0]["marker_id"] == "M058"

    def test_cyrillic_latin_abbr_mix(self):
        # НGB кириллической Н + латиница — как в реальных бланках
        res = registrar.map_markers([{"name": "НGB", "value": "150", "unit": "g/l"}], MARKER_ROWS)
        assert res["rows"][0]["marker_id"] == "M041"

    def test_duplicate_marker_deduped(self):
        items = DOC_MARKERS[:1] + DOC_MARKERS[:1]
        assert len(registrar.map_markers(items, MARKER_ROWS)["rows"]) == 1


# ───────────────────────── handle_update: ветвления ─────────────────────────

def test_not_document_nothing_written(sent, monkeypatch):
    monkeypatch.setattr(registrar.telegram, "download_file", lambda file_id, timeout=20.0: b"photo-bytes")
    monkeypatch.setattr(registrar, "classify_document", lambda content, mime: {"kind": "not_document", "reason": "еда"})
    monkeypatch.setattr(registrar, "extract_document",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("не должен извлекать")))
    monkeypatch.setattr(registrar, "persist_document",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("не должен писать")))

    registrar.handle_update(_photo_update())

    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == "8956401"
    assert "не похоже на документ" in text and "еда" in text
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.visit")
        assert cur.fetchone()[0] == 0  # НИЧЕГО не записано


def test_lab_document_writes_visit_and_results(lab_doc, sent):
    registrar.handle_update(_photo_update())

    assert len(sent) == 1
    chat_id, text = sent[0]
    assert "✅ Загрузил: визит 2026-09-15" in text
    assert "2 показателя" in text
    assert "Какой-то экзотический маркер" in text  # нераспознанные перечислены Владу

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.visit WHERE provenance->>'source_ref' = 'V20260915'")
        assert cur.fetchone()[0] == 1
        cur.execute(f"SELECT marker_key, value_num FROM {schema()}.lab_result ORDER BY marker_key")
        rows = cur.fetchall()
        assert [r[0] for r in rows] == ["M003", "M041"]
        cur.execute(f"SELECT count(*) FROM {schema()}.fact WHERE metric_key LIKE 'lab:V20260915%' OR metric_key LIKE 'lab:M%'")
        assert cur.fetchone()[0] == 2  # факт на каждый показатель


def test_reupload_same_document_no_duplicates(lab_doc, sent):
    """Идемпотентность: повторная отправка того же фото — те же ключи
    (visit_source_ref, marker_key), ON CONFLICT не плодит дубли."""
    registrar.handle_update(_photo_update())
    registrar.handle_update(_photo_update())

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.visit WHERE provenance->>'source_ref' = 'V20260915'")
        assert cur.fetchone()[0] == 1
        cur.execute(f"SELECT count(*) FROM {schema()}.lab_result")
        assert cur.fetchone()[0] == 2
        cur.execute(f"SELECT count(*) FROM {schema()}.fact WHERE metric_key LIKE 'lab:M%'")
        assert cur.fetchone()[0] == 2


def test_zero_recognized_markers_honest_nothing_written(monkeypatch, sent):
    monkeypatch.setattr(registrar.telegram, "download_file", lambda file_id, timeout=20.0: b"photo-bytes")
    monkeypatch.setattr(registrar, "classify_document", lambda content, mime: {"kind": "lab_report", "reason": ""})
    monkeypatch.setattr(
        registrar, "extract_document",
        lambda content, mime, today: {"document_date": "2026.09.15", "lab_name": "X",
                                      "notes": "", "markers": [{"name": "Незнакомка", "value": "1", "unit": ""}]})
    monkeypatch.setattr(registrar, "_fetch_marker_rows", lambda: MARKER_ROWS)

    registrar.handle_update(_photo_update())

    assert "е нашёл знакомых показателей" in sent[0][1]  # «Не нашёл...ничего не записал»
    assert "Незнакомка" in sent[0][1]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.visit")
        assert cur.fetchone()[0] == 0  # мусорный визит не создаём


def test_llm_error_visible_failure_not_silent(monkeypatch, sent):
    monkeypatch.setattr(registrar.telegram, "download_file", lambda file_id, timeout=20.0: b"photo-bytes")
    monkeypatch.setattr(registrar, "classify_document",
                        lambda content, mime: (_ for _ in ()).throw(RuntimeError("openrouter 502")))
    monkeypatch.setattr(registrar, "persist_document",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("не должен писать при сбое LLM")))

    registrar.handle_update(_photo_update())

    assert len(sent) == 1
    assert "Не смог разобрать документ" in sent[0][1]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.visit")
        assert cur.fetchone()[0] == 0


def test_unsupported_file_type_no_llm_call(monkeypatch, sent):
    monkeypatch.setattr(registrar.telegram, "download_file", lambda file_id, timeout=20.0: b"bytes")
    monkeypatch.setattr(registrar, "classify_document",
                        lambda content, mime: (_ for _ in ()).throw(AssertionError("не должен звать LLM для .txt")))
    registrar.handle_update(_doc_update(filename="notes.txt"))
    assert "Не смог открыть файл" in sent[0][1]


def test_document_without_date_uses_today_vl(monkeypatch, sent):
    monkeypatch.setattr(registrar.telegram, "download_file", lambda file_id, timeout=20.0: b"photo-bytes")
    monkeypatch.setattr(registrar, "classify_document", lambda content, mime: {"kind": "lab_report", "reason": ""})
    monkeypatch.setattr(
        registrar, "extract_document",
        lambda content, mime, today: {"document_date": "", "lab_name": "", "notes": "",
                                      "markers": [DOC_MARKERS[0]]})
    monkeypatch.setattr(registrar, "_fetch_marker_rows", lambda: MARKER_ROWS)
    monkeypatch.setattr(registrar, "_vl_today", lambda: "2026-09-18")

    registrar.handle_update(_photo_update())
    assert "визит 2026-09-18" in sent[0][1]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.visit WHERE provenance->>'source_ref' = 'V20260918'")
        assert cur.fetchone()[0] == 1


def test_mime_detection():
    assert registrar._mime_for("x.jpg", True) == "image/jpeg"          # фото — всегда jpeg
    assert registrar._mime_for("analysis.PDF", False) == "application/pdf"
    assert registrar._mime_for("img.heic", False) == "image/heic"
    assert registrar._mime_for("file.docx", False) is None


def test_content_part_shapes():
    part = registrar._content_part(b"x", "image/jpeg")
    assert part["type"] == "image_url" and part["image_url"]["url"].startswith("data:image/jpeg;base64,")
    part = registrar._content_part(b"x", "application/pdf")
    assert part["type"] == "file" and part["file"]["filename"] == "document.pdf"


def test_reply_send_failure_does_not_raise(monkeypatch, sent):
    """Падение отправки ответа не должно ронять обработку (guard поллера и так
    поймал бы, но здесь дешевле — сам registrar не бросает)."""
    monkeypatch.setattr(registrar.telegram, "send_message",
                        lambda chat_id, text, **k: (_ for _ in ()).throw(RuntimeError("tg down")))
    monkeypatch.setattr(registrar.telegram, "download_file", lambda file_id, timeout=20.0: b"b")
    monkeypatch.setattr(registrar, "classify_document", lambda content, mime: {"kind": "not_document", "reason": "мем"})
    registrar.handle_update(_photo_update())  # не бросает


# ─────────────── дуал-райт в health.visits/results (санкция ZCode 18.09) ───────────────
# conftest перенаправляет цель дуал-райта в card_test.visits/results (двойники
# health.* той же формы) — тесты реально гоняют SQL upsert'ы, прод не трогая.

def _health_visits(**vals):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO {schema()}.visits ("Visit_ID","Date","Age_at_Visit","Lab_Name","Notes") '
            'VALUES (%s,%s,%s,%s,%s) ON CONFLICT ("Visit_ID") DO NOTHING',
            (vals.get("Visit_ID"), vals.get("Date"), vals.get("Age_at_Visit"),
             vals.get("Lab_Name"), vals.get("Notes")))
        conn.commit()


def test_dual_write_creates_visit_and_results(lab_doc, sent):
    registrar.handle_update(_photo_update())

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f'SELECT "Visit_ID","Date","Age_at_Visit","Lab_Name","Notes" FROM {schema()}.visits')
        v = cur.fetchall()
        assert v == [("V20260915", "15.09.2026", "44", "Инвитро", "биохимия")]
        cur.execute(f'SELECT "Marker_ID","Value","Original_Unit","Lab_Min","Lab_Max" FROM {schema()}.results ORDER BY "Marker_ID"')
        rows = cur.fetchall()
        assert [r[0] for r in rows] == ["M003", "M041"]
        assert rows[1] == ("M041", "145", "г/л", "130", "160")  # Value с запятой/без .0 — формат старого пути
        assert rows[0][1] == "5,2"  # запятая, как в прод-витрине


def test_dual_write_idempotent_both_targets(lab_doc, sent):
    """Повторная отправка того же фото: 0 новых строк в ОБОИХ хранилищах."""
    registrar.handle_update(_photo_update())
    registrar.handle_update(_photo_update())
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.visits")
        assert cur.fetchone()[0] == 1
        cur.execute(f"SELECT count(*) FROM {schema()}.results")
        assert cur.fetchone()[0] == 2
        cur.execute(f"SELECT count(*) FROM {schema()}.visit WHERE provenance->>'source_ref' = 'V20260915'")
        assert cur.fetchone()[0] == 1
        cur.execute(f"SELECT count(*) FROM {schema()}.lab_result")
        assert cur.fetchone()[0] == 2


def test_dual_write_reuses_existing_visit_by_date(lab_doc, sent):
    """Визит с той же датой уже есть (старый формат Visit_ID) — реюз его
    Visit_ID, поля не перезаписаны, дубль-визит не создаётся (порт Build Visit)."""
    _health_visits(Visit_ID="V09", Date="15.09.2026", Age_at_Visit="44",
                   Lab_Name="КДЦ", Notes="старая заметка")
    registrar.handle_update(_photo_update())

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f'SELECT "Visit_ID","Lab_Name","Notes" FROM {schema()}.visits')
        v = cur.fetchall()
        assert v == [("V09", "КДЦ", "старая заметка")]  # старые поля сохранены
        cur.execute(f'SELECT DISTINCT "Visit_ID" FROM {schema()}.results')
        assert cur.fetchall() == [("V09",)]  # результаты под реюзнутым визитом


def test_dual_write_failure_visible_card_still_written(lab_doc, sent, monkeypatch):
    """Сбой дуал-райта: card.* записан, ответ Владу с ⚠️-пометкой — не тихий."""
    def boom(*a, **k):
        raise RuntimeError("health schema unreachable")
    monkeypatch.setattr(registrar, "persist_health", boom)

    registrar.handle_update(_photo_update())

    assert "не доехали до старой базы" in sent[0][1]
    assert "✅ Загрузил" in sent[0][1]  # основная запись состоялась
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.visit WHERE provenance->>'source_ref' = 'V20260915'")
        assert cur.fetchone()[0] == 1
        cur.execute(f"SELECT count(*) FROM {schema()}.lab_result")
        assert cur.fetchone()[0] == 2
        cur.execute(f"SELECT count(*) FROM {schema()}.results")
        assert cur.fetchone()[0] == 0  # health-цель не тронута упавшим вызовом


def test_to_comma_formats():
    assert registrar._to_comma(145.0) == "145"
    assert registrar._to_comma(5.222) == "5,222"
    assert registrar._to_comma(None) == ""
    assert registrar._to_comma("4.1") == "4,1"


# ─────────── F6 (внешний аудит логики, 2026-09-22): частичная запись ───────────

_ROWS_2 = [
    {"marker_id": "M041", "label": "Гемоглобин", "value_num": 145.0,
     "unit": "г/л", "ref_min": 130.0, "ref_max": 160.0},
    {"marker_id": "M003", "label": "Глюкоза", "value_num": 5.2,
     "unit": "ммоль/л", "ref_min": 4.1, "ref_max": 5.9},
]


def test_partial_persist_retries_once_and_succeeds(lab_doc, monkeypatch):
    """Один маркер падает на первой попытке — повтор дозальёт его (идемпотентно)."""
    import app.main as main_mod
    real = main_mod.labs_result_sync
    seen = {"M041": 0}

    def flaky(req):
        if req.marker_key == "M041":
            seen["M041"] += 1
            if seen["M041"] == 1:
                raise RuntimeError("сеть мигнула")
        return real(req)

    monkeypatch.setattr(main_mod, "labs_result_sync", flaky)
    ref = registrar.persist_document({"lab_name": "Инвитро", "notes": ""}, {"rows": _ROWS_2}, "2026-09-15")

    assert ref == "V20260915"
    assert seen["M041"] == 2  # была повторная попытка
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {schema()}.lab_result")
        assert cur.fetchone()[0] == 2  # оба записаны, дубля нет


def test_partial_persist_raises_with_counts(lab_doc, monkeypatch):
    """Постоянный сбой одного маркера: PartialPersistError со счётчиками,
    успевшие показатели остаются в базе (раньше наружу уходило «ничего»)."""
    import app.main as main_mod
    real = main_mod.labs_result_sync

    def boom_m003(req):
        if req.marker_key == "M003":
            raise RuntimeError("база недоступна")
        return real(req)

    monkeypatch.setattr(main_mod, "labs_result_sync", boom_m003)
    with pytest.raises(registrar.PartialPersistError) as ei:
        registrar.persist_document({"lab_name": "Инвитро", "notes": ""}, {"rows": _ROWS_2}, "2026-09-15")

    assert ei.value.written == 1 and ei.value.total == 2
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT marker_key FROM {schema()}.lab_result ORDER BY marker_key")
        assert [r[0] for r in cur.fetchall()] == ["M041"]


def test_handle_update_partial_write_honest_reply(lab_doc, sent, monkeypatch):
    """Ответ Владу при частичной записи честный: сколько записано, что делать."""
    def partial(*a, **k):
        raise registrar.PartialPersistError(1, 2, RuntimeError("сеть"))

    monkeypatch.setattr(registrar, "persist_document", partial)
    monkeypatch.setattr(registrar, "persist_health", lambda *a, **k: "V20260915")

    registrar.handle_update(_photo_update())

    assert len(sent) == 1
    _, text = sent[0]
    assert "частично" in text and "1 из 2" in text and "дозапишутся" in text
