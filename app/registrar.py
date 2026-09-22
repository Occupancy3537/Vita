"""
Регистратор лабораторных документов (Волна 3 / B2, 2026-09-18): перенос
`Sub-Agent: Registrar` + `Save_Lab_Results` из n8n. Фото/документ, присланный
доктор-боту, диспетчер детерминированно маршрутизирует сюда (dispatch.route ->
"registrar"), дальше:

  Шаг 1 — классификация содержания (vision-LLM): лабораторный документ /
          заключение врача / НЕ документ. НЕ документ (фото еды, симптома,
          мема) — честный ответ Владу, НИЧЕГО не записывается (тихая запись
          мусора — риск №1).
  Шаг 2 — извлечение (только для документов): document_date / lab_name /
          notes / markers[{name,value,unit,ref_low,ref_high}] — порт промпта
          n8n-регистратора (маркерсная схема — дословно). Значения только
          видимые на изображении, ничего не выдумывать.
  Шаг 3 — запись: ТЕ ЖЕ внутренние функции, что стоят за /visits/sync и
          /labs/result (app.main.visits_sync / labs_result_sync — вызов
          напрямую, не через HTTP-loopback; импорт ленивый из-за цикла
          main->poller->registrar). Идемпотентность наследуется:
          /labs/result — ON CONFLICT по (visit_source_ref, marker_key),
          visit_source_ref = "V<ГГГГММДД>" из даты документа (тот же ключ,
          что строил Build Visit в n8n) — повторная отправка того же фото
          дублей не плодит.
  Шаг 3b — дуал-райт (санкция ZCode 18.09): те же визит+результаты в
          health.visits/health.results (паритет со старым n8n-путём, phase C)
          — потребители health.results (PhenoAge Calc, Advisor, Watchdog,
          bioage-dashboard) продолжают видеть новые лаб-данные до перевода на
          card.lab_result в Волнах 4+. Сбой дуал-райта не рвёт ответ Владу
          (card.* уже зафиксирован), но помечается в ответе «не доехало».
  Шаг 4 — ответ Владу в тот же чат: «✅ Загрузил: визит <дата>, <N>
          показателей» + нераспознанные имена списком + при 0 распознанных —
          честное «не нашёл знакомых показателей, ничего не записал».

Маппинг имя->Marker_ID — порт ноды Map из Save_Lab_Results: словарь SYN
(202 синонима) + ABBR (латинские аббревиатуры) + имена из health.markers +
%-/абс-разрешение лейкоформулы по единице + конвертация единиц PhenoAge
(альбумин/глюкоза/креатинин/СРБ). Словари перенесены автогенератором из
jsCode (не руками) — /tmp/gen_syn.py, см. волна 3 отчёт.

Файлы живут в памяти (bytes), на диск ничего не пишется. Модели:
REGISTRAR_MODEL (изображения, дефолт — DEFAULT_MODEL из app/ai_models.py,
z-ai/glm-5.3-flash — проверена живым вызовом 18.09: кириллица лаб-бланка
читается, 1.8с, ~$0.00013/вызов) и REGISTRAR_PDF_MODEL (PDF — glm-5.3-flash
не принимает file-модальность, поэтому дефолт FOOD_MODEL, google/gemini-3.1-
flash-lite — та же модель, что и у фото-пути Food diary, но выбрана здесь
по ДРУГОЙ причине: не "это еда", а "единственная дешёвая с поддержкой PDF").
"""
import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.ai_models import DEFAULT_MODEL, FOOD_MODEL
from app.db import get_conn
from app.doctor import telegram

logger = logging.getLogger(__name__)

OWNER_CHAT_ID = "8956401"
VL = timezone(timedelta(hours=10))
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
REGISTRAR_MODEL = os.environ.get("REGISTRAR_MODEL", DEFAULT_MODEL)
REGISTRAR_PDF_MODEL = os.environ.get("REGISTRAR_PDF_MODEL", FOOD_MODEL)
PROMPT_VERSION = "registrar-vision/1"

# ─────────────────── чистая логика (без сети и БД — для тестов) ───────────────────

# Автоперенесено из n8n Save_Lab_Results / нода Map (jsCode) — генератор /tmp/gen_syn.py
SYN = {"билирубин общий": "M001", "общий билирубин": "M001", "билирубин": "M001", "билирубин прямой": "M002", "прямой билирубин": "M002", "билирубин конъюгированный": "M002", "глюкоза": "M003", "глюкоза плазмы": "M003", "глюкоза венозной крови": "M003", "глюкоза натощак": "M003", "креатинин": "M004", "мочевая кислота": "M005", "мочевина": "M006", "urea": "M006", "общий белок": "M007", "белок общий": "M007", "альбумин": "M008", "триглицериды": "M009", "тг": "M009", "холестерин общий": "M010", "холестерин": "M010", "общий холестерин": "M010", "холестерин суммарный": "M010", "холестерин лпвп": "M011", "лпвп": "M011", "hdl": "M011", "хс лпвп": "M011", "холестерин hdl": "M011", "холестерин лпнп": "M012", "лпнп": "M012", "ldl": "M012", "хс лпнп": "M012", "холестерин ldl": "M012", "холестерин лпонп": "M013", "лпонп": "M013", "vldl": "M013", "холестерин не лпвп": "M014", "не hdl": "M014", "non hdl": "M014", "холестерин неквп": "M014", "аланинаминотрансфераза": "M015", "алат": "M015", "алт": "M015", "alt": "M015", "аспартатаминотрансфераза": "M016", "асат": "M016", "аст": "M016", "ast": "M016", "щелочная фосфатаза": "M017", "alp": "M017", "щф": "M017", "т4 свободный": "M018", "свободный т4": "M018", "ft4": "M018", "ттг": "M019", "tsh": "M019", "тиреотропный гормон": "M019", "тестостерон общий": "M020", "тестостерон": "M020", "пса общий": "M021", "пса": "M021", "psa": "M021", "простатический специфический антиген": "M021", "пса свободный": "M022", "свободный пса": "M022", "пса свободный пса": "M023", "соотношение пса": "M023", "с реактивный белок": "M024", "срб": "M024", "crp": "M024", "hs crp": "M024", "вчсрб": "M024", "с реактивный белок высокочувствительный": "M024", "ревматоидный фактор": "M025", "рф": "M025", "антитела сср": "M026", "аццп": "M026", "ферритин": "M027", "железо": "M028", "железо сывороточное": "M028", "сывороточное железо": "M028", "fe": "M028", "натрий": "M029", "na": "M029", "калий": "M030", "k": "M030", "гликированный гемоглобин": "M031", "hba1c": "M031", "гликированный гемоглобин hba1c": "M031", "гликогемоглобин": "M031", "инсулин": "M032", "фолиевая кислота": "M033", "фолаты": "M033", "витамин b12": "M034", "b12": "M034", "цианокобаламин": "M034", "витамин в12": "M034", "витамин d": "M035", "витамин d 25 oh": "M035", "витамин d 25 он": "M035", "25 oh витамин d": "M035", "25 он витамин d": "M035", "витамин д": "M035", "25 гидроксивитамин d": "M035", "ige общий": "M036", "ige": "M036", "иммуноглобулин e общий": "M036", "иммуноглобулин e": "M036", "гемоглобин в кале": "M037", "трансферрин в кале": "M038", "лейкоциты": "M039", "wbc": "M039", "эритроциты": "M040", "rbc": "M040", "гемоглобин": "M041", "hgb": "M041", "hb": "M041", "гематокрит": "M042", "hct": "M042", "средний объем эритроцита": "M043", "средний объем эритроцитов": "M043", "mcv": "M043", "среднее содержание hb": "M044", "среднее содержание гемоглобина": "M044", "mch": "M044", "средняя концентрация hb": "M045", "средняя концентрация гемоглобина": "M045", "mchc": "M045", "тромбоциты": "M046", "plt": "M046", "цветовой показатель": "M047", "цп": "M047", "соэ": "M048", "esr": "M048", "скорость оседания эритроцитов": "M048", "ширина распред эритроцитов": "M049", "rdw": "M049", "rdw cv": "M049", "rdw sd": "M050", "ширина распред тромбоцитов": "M051", "pdw": "M051", "средний объем тромбоцитов": "M052", "mpv": "M052", "тромбокрит": "M053", "pct": "M053", "крупные тромбоциты": "M054", "p lcr": "M054", "крупные тромбоциты абс": "M055", "p lcc": "M055", "незрелые гранулоциты абс": "M056", "незрелые гранулоциты": "M057", "незрелые гранулоциты процент": "M057", "нейтрофилы абс": "M058", "нейтрофилы абсолютное": "M058", "нейтрофилы абсолютные": "M058", "нейтрофилы": "M059", "нейтрофилы процент": "M059", "нейтрофилы отн": "M059", "neu": "M059", "лимфоциты абс": "M061", "лимфоциты абсолютное": "M061", "лимфоциты абсолютные": "M061", "лимфоциты": "M062", "лимфоциты процент": "M062", "лимфоциты отн": "M062", "lym": "M062", "моноциты абс": "M063", "моноциты абсолютное": "M063", "моноциты": "M064", "моноциты процент": "M064", "моноциты отн": "M064", "эозинофилы абс": "M065", "эозинофилы абсолютное": "M065", "эозинофилы": "M066", "эозинофилы процент": "M066", "эозинофилы отн": "M066", "базофилы абс": "M067", "базофилы абсолютное": "M067", "базофилы": "M068", "базофилы процент": "M068", "базофилы отн": "M068", "палочкоядерные нейтрофилы": "M060", "палочкоядерные": "M060", "нейтрофилы палочкоядерные": "M060", "сегментоядерные нейтрофилы": "M059", "сегментоядерные": "M059", "нейтрофилы сегментоядерные": "M059", "ачтв": "M069", "aptt": "M069", "тромбиновое время": "M070", "тв": "M070", "фибриноген": "M071", "антитромбин iii": "M072", "антитромбин 3": "M072", "мно": "M073", "inr": "M073", "протромбиновое отношение": "M074", "по": "M074", "агрескрин тест": "M075", "рфмк": "M076", "пти": "M077", "протромбиновый индекс": "M077"}

ABBR = {"mchc": "M045", "mch": "M044", "mcv": "M043", "wbc": "M039", "rbc": "M040", "hgb": "M041", "hb": "M041", "hct": "M042", "plt": "M046", "rdw": "M049", "rdwcv": "M049", "rdwsd": "M050", "mpv": "M052", "pdw": "M051", "pct": "M053", "esr": "M048", "soe": "M048", "alt": "M015", "ast": "M016", "alp": "M017", "tsh": "M019", "ft4": "M018", "psa": "M021", "crp": "M024", "hscrp": "M024", "hba1c": "M031", "inr": "M073", "pti": "M077", "aptt": "M069", "neu": "M059", "lym": "M062", "mono": "M064", "eos": "M066", "baso": "M068"}

# Пары «% <-> абс» для 5-компонентной лейкоформулы: голое имя («нейтрофилы») —
# %-вариант, абс-вариант выбирается по единице измерения.
PCT_TO_ABS = {"M059": "M058", "M062": "M061", "M064": "M063", "M066": "M065", "M068": "M067"}
ABS_SET = {"M058", "M061", "M063", "M065", "M067"}


def _vl_today() -> str:
    return datetime.now(VL).strftime("%Y-%m-%d")


def norm(s) -> str:
    """Порт norm() из ноды Map: нижний регистр, ё→е, скобки вон, только
    буквы/цифры/%, пробелы схлопнуты."""
    s = str("" if s is None else s).lower().replace("ё", "е")
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-zа-я0-9%]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Свернуть кириллические буквы-двойники в латиницу — аббревиатуры в бланках
# печатают вперемешку (МСHС, НGB и т.п.). Дословный порт fold() из ноды Map.
_FOLD_MAP = {"а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h",
             "о": "o", "р": "p", "с": "c", "т": "t", "х": "x", "у": "y", "і": "i"}


def fold(s: str) -> str:
    return "".join(_FOLD_MAP.get(ch, ch) if "а" <= ch <= "я" or ch == "і" else ch
                   for ch in str(s or ""))


def to_float(v) -> Optional[float]:
    """Порт numOf(): '5,2' -> 5.2; 'отрицательно' -> None."""
    if v is None or v == "":
        return None
    try:
        return float(str(v).strip().replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _unit_is_pct(u_norm: str) -> bool:
    return u_norm == "%" or "процент" in u_norm or "отн" in u_norm


def _unit_is_abs(u_norm: str) -> bool:
    """Абсолютные значения лейкоформулы: 10^9/л (= г/л, G/L), кл/мкл, «абс».
    В JS-оригинале часть паттернов была мертва (norm() выкидывает '/' и '^'),
    здесь проверяем и нормализованную форму ('109', 'клмкл'), и исходные
    написания — намеренное исправление, см. отчёт волны 3."""
    u = u_norm.replace(" ", "")
    if re.search(r"109|x10|клмкл|абс|г/л|g/l|гл|gl", u):
        return True
    return u.endswith("/л") or u.endswith("/l")


def resolve_id(name: str, unit: str, by_name: dict) -> Optional[str]:
    """Порт resolveId(): SYN -> health.markers -> fold-варианты -> ABBR ->
    префикс (ключи >= 4 симв.) + %-/абс-разрешение лейкоформулы."""
    n = norm(name)
    nf = fold(n)
    u = norm(unit)
    mid = SYN.get(n) or by_name.get(n) or SYN.get(nf) or by_name.get(nf)
    if not mid:
        flat = re.sub(r"[\s-]", "", nf)
        mid = ABBR.get(flat)
    if not mid:
        for k in SYN:
            if len(k) >= 4 and n.startswith(k):
                mid = SYN[k]
                break
    if not mid:
        return None
    # Лейкоформула: имя может нести «абс»/«%», иначе решаем по единице.
    # (В JS здесь был \bабс — мёртвый для кириллицы; здесь простое вхождение,
    # правило 6 playbook: кириллица в regex без \w/\b.)
    if "абс" in n:
        mid = PCT_TO_ABS.get(mid, mid)
    elif "отн" not in n and "%" not in n and mid in PCT_TO_ABS:
        if _unit_is_abs(u) and not _unit_is_pct(u):
            mid = PCT_TO_ABS[mid]
    if mid in ABS_SET and _unit_is_pct(u):
        for p, a in PCT_TO_ABS.items():
            if a == mid:
                mid = p
                break
    return mid


def convert_unit(marker_id: str, value: float, unit: str):
    """Порт convUnit(): PhenoAge-формула чувствительна к единицам — приводим 4
    «опасных» маркера к стандартным. Возвращает (value, unit, note) | None."""
    u = norm(unit).replace(" ", "")
    is_gdl = "gdl" in u or "гдл" in u
    is_mgdl = "mgdl" in u or "мгдл" in u
    if marker_id == "M008" and is_gdl:
        return value * 10, "г/л", "из г/дл"
    if marker_id == "M003" and is_mgdl:
        return value / 18.0, "ммоль/л", "из мг/дл"
    if marker_id == "M004" and is_mgdl:
        return value * 88.4, "мкмоль/л", "из мг/дл"
    if marker_id == "M024" and is_mgdl:
        return value * 10, "мг/л", "из мг/дл"
    return None


def parse_document_date(raw, today_iso: str) -> Optional[str]:
    """Порт pd() + проверка будущего из Build Visit: DD.MM.YYYY / YYYY.MM.DD
    (и через '/-') -> ГГГГ-ММ-ДД; нет даты или дата из будущего -> None
    (система подставит сегодняшнюю VL — то же поведение, что n8n)."""
    s = str("" if raw is None else raw).strip().replace("/", ".").replace("-", ".")
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        iso = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    else:
        m = re.match(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})", s)
        if not m:
            return None
        iso = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return iso if iso <= today_iso else None


def map_markers(items: list, marker_rows: list) -> dict:
    """Порт ноды Map: сырой markers[] от LLM -> распознанные строки для записи
    + нераспознанные имена (НЕ записываются) + конвертации единиц."""
    by_name = {}
    labels = {}
    for m in marker_rows or []:
        if not m.get("Marker_ID"):
            continue
        by_name[norm(m.get("Name"))] = m["Marker_ID"]
        labels[m["Marker_ID"]] = m.get("Name")

    seen = set()
    rows, unmatched, converted = [], [], []
    for it in items or []:
        if not it:
            continue
        name = str(it.get("name") or it.get("Name") or "").strip()
        val_raw = it.get("value", it.get("Value"))
        if not name or val_raw is None or str(val_raw).strip() == "":
            continue
        value = to_float(val_raw)
        if value is None:  # «отрицательно», «не обнаружено» — не числовой
            continue
        unit = str(it.get("unit") or it.get("Unit") or "").strip()
        mid = resolve_id(name, unit, by_name)
        if not mid:
            unmatched.append(name)
            continue
        if mid in seen:
            continue
        seen.add(mid)

        out_unit = unit
        c = convert_unit(mid, value, unit)
        if c:
            value, out_unit, note = c
            value = round(value, 3)
            converted.append(f"{name} {note}")
            out_unit = f"{out_unit} ({unit or note})"
        ref_low = to_float(it.get("ref_low", it.get("refLow", it.get("min"))))
        ref_high = to_float(it.get("ref_high", it.get("refHigh", it.get("max"))))
        rows.append({
            "marker_id": mid, "label": labels.get(mid, name), "value_num": value,
            "unit": out_unit, "ref_min": ref_low, "ref_max": ref_high,
        })
    return {"rows": rows, "unmatched": unmatched, "converted": converted}


def _plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "показатель"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "показателя"
    return "показателей"


def build_reply(date_iso: str, mapped: dict, doc_kind: str) -> str:
    """Шаг 4: текст подтверждения Владу. 0 распознанных — честный отказ."""
    if not mapped["rows"]:
        parts = ["Не нашёл знакомых показателей — ничего не записал."]
        if mapped["unmatched"]:
            parts.append("Видел, но не распознал: " + "; ".join(mapped["unmatched"]) + ".")
        if doc_kind == "doctor_conclusion":
            parts.append("Это похоже на заключение без числовых показателей — перескажи суть доктору сообщением, он зафиксирует в карте.")
        return " ".join(parts)
    n = len(mapped["rows"])
    text = f"✅ Загрузил: визит {date_iso}, {n} {_plural(n)}"
    if mapped["converted"]:
        text += "\nПриведены к стандартным единицам: " + "; ".join(mapped["converted"]) + "."
    if mapped["unmatched"]:
        text += "\nНе распознаны и НЕ записаны: " + "; ".join(mapped["unmatched"]) + "."
    return text


# ───────────────────────────── LLM (vision) I/O ─────────────────────────────

CLASSIFY_SYSTEM_PROMPT = """Ты — классификатор вложений медицинского бота. Перед тобой изображение или PDF.
Верни JSON строго по схеме:
{
  "kind": "lab_report" | "doctor_conclusion" | "not_document",
  "reason": "кратко по-русски, до 10 слов"
}
kind:
- lab_report — бланк/фото/файл лабораторного или диагностического исследования с числовыми показателями (кровь, моча, биохимия, гормоны, витамины, ЭКГ-расшифровка с цифрами и т.п.);
- doctor_conclusion — заключение врача, выписка, описание обследования (УЗИ, МРТ, справка) БЕЗ таблицы числовых показателей;
- not_document — всё остальное: еда, симптомы (сыпь/отёк на коже), скриншоты, мемы, пейзажи, фото людей.
При сомнении между lab_report и doctor_conclusion выбирай lab_report. Только JSON, без пояснений."""

# Порт системного промпта n8n-регистратора (Sub-Agent: Registrar) — правила
# document_date и markers перенесены дословно, инструментарий Add_Medical_Note
# (Google Sheets, уходит в B3) заменён на поля ответа.
EXTRACT_SYSTEM_PROMPT = """СЕГОДНЯ: {today} (Владивосток).

Ты — Регистратор анализов (ИИ-ассистент врача). Внимательно изучи прикреплённый документ (фото или PDF) и извлеки из него структуру.

Верни JSON строго по схеме:
{{
  "document_date": "ГГГГ.ММ.ДД" | "",
  "lab_name": string,
  "notes": string,
  "markers": [{{"name": string, "value": string, "unit": string, "ref_low": string, "ref_high": string}}]
}}

ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:
1. document_date — дата СТРОГО из шапки документа (ищи «дата исследования», «дата забора», «дата приёма», дату рядом с названием лаборатории или ФИО). Формат ГГГГ.ММ.ДД. Если в документе только день и месяц — год бери текущий из строки СЕГОДНЯ выше. Если явной даты в документе НЕТ — верни document_date пустым, система подставит сегодняшнюю. НИКОГДА не выдумывай дату, не ставь наугад и не бери числа из тела текста (референсы, номера бланков).
2. lab_name — название лаборатории/учреждения из шапки (нет — пустая строка).
3. notes — короткая пометка о типе анализа: ОАК, биохимия, гормоны, липидный профиль и т.п.
4. markers — ВСЕ числовые показатели документа. name пиши строго как в документе. Включай только строки с числовым значением; качественные («отрицательно», «не обнаружено») НЕ включай. Ничего не выдумывай: значения только видимые в документе, никаких типовых цифр из памяти. ref_low/ref_high — референсные границы из документа, если напечатаны; иначе пустые строки.
Только JSON, без пояснений."""


def _model_for(mime: str) -> str:
    return REGISTRAR_PDF_MODEL if mime == "application/pdf" else REGISTRAR_MODEL


def _content_part(content: bytes, mime: str) -> dict:
    b64 = base64.b64encode(content).decode()
    if mime == "application/pdf":
        return {"type": "file", "file": {"filename": "document.pdf",
                                         "file_data": f"data:application/pdf;base64,{b64}"}}
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def _vision_json(system_prompt: str, content: bytes, mime: str, timeout: float = 60.0) -> dict:
    """Один vision-вызов в OpenRouter -> распарсенный JSON. Паттерн тот же, что
    app/extraction.py (structured output, temperature 0), контент — parts."""
    api_key = os.environ["OPENROUTER_API_KEY"]
    resp = httpx.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": _model_for(mime),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [_content_part(content, mime)]},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return json.loads(resp.json()["choices"][0]["message"]["content"])


def classify_document(content: bytes, mime: str) -> dict:
    """Шаг 1. kind, reason. Невалидный ответ модели — исключение (видимый сбой)."""
    parsed = _vision_json(CLASSIFY_SYSTEM_PROMPT, content, mime)
    kind = parsed.get("kind")
    if kind not in ("lab_report", "doctor_conclusion", "not_document"):
        raise ValueError(f"classify_document: неожиданный kind={kind!r}")
    return {"kind": kind, "reason": str(parsed.get("reason") or "")}


def extract_document(content: bytes, mime: str, today_vl: str) -> dict:
    """Шаг 2. document_date, lab_name, notes, markers[]."""
    parsed = _vision_json(
        EXTRACT_SYSTEM_PROMPT.format(today=today_vl), content, mime)
    markers = parsed.get("markers")
    if not isinstance(markers, list):
        raise ValueError("extract_document: markers не список")
    return {
        "document_date": str(parsed.get("document_date") or ""),
        "lab_name": str(parsed.get("lab_name") or ""),
        "notes": str(parsed.get("notes") or ""),
        "markers": markers,
    }


# ───────────────────────────── запись (шаг 3) ─────────────────────────────

_HEALTH_SCHEMA = os.environ.get("REGISTRAR_HEALTH_SCHEMA", "health")
BIRTH_YEAR = 1982  # порт Build Visit: Age_at_Visit = год документа - 1982


def _to_comma(v) -> str:
    """Порт toComma() из ноды Map: число -> строка с запятой (формат старого
    пути Results; PhenoAge num() парсит и запятую, и точку — но не меняем
    формат витрины). 145.0 -> '145', 5.222 -> '5,222', None -> ''."""
    if v is None or v == "":
        return ""
    if isinstance(v, float) and v.is_integer():
        s = str(int(v))
    else:
        s = str(v).strip().replace(" ", "")
    return s.replace(".", ",")


def _fetch_marker_rows() -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT "Marker_ID", "Name" FROM health.markers')
        return [{"Marker_ID": r[0], "Name": r[1]} for r in cur.fetchall()]


def persist_document(doc: dict, mapped: dict, date_iso: str) -> str:
    """Визит + результаты через внутренние функции /visits/sync и /labs/result.
    Импорт ленивый: app.main импортирует poller -> сюда, обычный import дал бы
    цикл на этапе загрузки. visit_source_ref = V<ГГГГММДД> — тот же ключ, что
    строил нода Build Visit (дублей при перезагрузке не будет: ON CONFLICT)."""
    from app.main import LabResultSyncRequest, VisitSyncRequest, labs_result_sync, visits_sync  # noqa: E501
    visit_ref = "V" + date_iso.replace("-", "")
    ts = date_iso + "T00:00:00Z"  # тот же формат, что Sync Visit to Card в n8n
    visits_sync(VisitSyncRequest(source_ref=visit_ref, title=doc.get("lab_name") or None,
                                 raw_text=doc.get("notes") or None, ts_event=ts))
    for r in mapped["rows"]:
        labs_result_sync(LabResultSyncRequest(
            visit_source_ref=visit_ref, visit_ts_event=ts, marker_key=r["marker_id"],
            marker_label=r["label"], value_num=r["value_num"], unit=r["unit"] or None,
            ref_min=r["ref_min"], ref_max=r["ref_max"]))
    return visit_ref


def persist_health(doc: dict, mapped: dict, date_iso: str) -> str:
    """Шаг 3b — дуал-райт (санкция ZCode 18.09): те же визит+результаты в
    health.visits / health.results, паритет со старым n8n-путём (Write Visit PG /
    Write Results PG, phase C: upsert по PK "Visit_ID" и ("Visit_ID","Marker_ID"),
    колонки/форматы один-в-один — Value с запятой, Date DD.MM.YYYY). Порт
    семантики Build Visit: существующий визит с той же датой переиспользуется,
    его поля НЕ перезаписываются; нового — Visit_ID = V<ГГГГММДД>,
    Age_at_Visit = год - 1982. Сбой здесь не должен рвать ответ Владу —
    вызывающий оборачивает в try/except и помечает ответ."""
    ru_date = f"{date_iso[8:10]}.{date_iso[5:7]}.{date_iso[0:4]}"
    age = str(int(date_iso[:4]) - BIRTH_YEAR)
    lab_name = (doc.get("lab_name") or "").strip()
    notes = (doc.get("notes") or "").strip()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f'SELECT "Visit_ID" FROM {_HEALTH_SCHEMA}.visits WHERE "Date" = %s LIMIT 1',
                    (ru_date,))
        row = cur.fetchone()
        if row:
            visit_id = row[0]  # визит с этой датой уже есть — реюз, старые поля сохраняем
        else:
            visit_id = "V" + date_iso.replace("-", "")
            cur.execute(
                f'INSERT INTO {_HEALTH_SCHEMA}.visits ("Visit_ID","Date","Age_at_Visit","Lab_Name","Notes") '
                f'VALUES (%s,%s,%s,%s,%s) ON CONFLICT ("Visit_ID") DO NOTHING',
                (visit_id, ru_date, age, lab_name, notes))
        for r in mapped["rows"]:
            cur.execute(
                f'INSERT INTO {_HEALTH_SCHEMA}.results ("Visit_ID","Marker_ID","Value","Original_Unit","Lab_Min","Lab_Max") '
                f'VALUES (%s,%s,%s,%s,%s,%s) '
                f'ON CONFLICT ("Visit_ID","Marker_ID") DO UPDATE SET '
                f'"Value"=EXCLUDED."Value","Original_Unit"=EXCLUDED."Original_Unit",'
                f'"Lab_Min"=EXCLUDED."Lab_Min","Lab_Max"=EXCLUDED."Lab_Max",_synced_at=now()',
                (visit_id, r["marker_id"], _to_comma(r["value_num"]), r["unit"] or "",
                 _to_comma(r["ref_min"]), _to_comma(r["ref_max"])))
        conn.commit()
    return visit_id


# ───────────────────────────── точка входа ─────────────────────────────

_MIME_BY_EXT = {
    ".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif",
}
UNSUPPORTED_REPLY = (
    "Не смог открыть файл этого типа — пришли, пожалуйста, фото документа сообщением "
    "(или файлом jpg/png/pdf). Ничего не записал."
)
NOT_DOCUMENT_REPLY = (
    "Это не похоже на документ анализов ({reason}). Ничего не записал. "
    "Фото симптомов пока опиши словами — передам доктору."
)
FAILURE_REPLY = (
    "⚠️ Не смог разобрать документ (ошибка обработки). Ничего не записал — "
    "попробуй прислать ещё раз, фото целиком при хорошем свете."
)


def _attachment(update: dict):
    """(file_id, filename, is_photo) для крупнейшей фото-версии или документа."""
    msg = update.get("message") or {}
    if msg.get("photo"):
        largest = msg["photo"][-1]  # Telegram отдаёт по возрастанию размера
        return largest["file_id"], f"{largest['file_id']}.jpg", True
    if msg.get("document"):
        doc = msg["document"]
        return doc["file_id"], doc.get("file_name") or doc["file_id"], False
    return None, None, False


def _mime_for(filename: str, is_photo: bool) -> Optional[str]:
    if is_photo:
        return "image/jpeg"  # Telegram всегда отдаёт фото как JPEG
    ext = os.path.splitext(filename)[1].lower()
    return _MIME_BY_EXT.get(ext)


def _reply(chat_id: str, text: str) -> None:
    try:
        telegram.send_message(chat_id, text)
    except Exception:  # отправка никогда не должна ронять обработку
        logger.exception("registrar: не удалось отправить ответ Владу")


def handle_update(update: dict) -> None:
    """Ветка "registrar" диспетчера: фото/документ -> разбор -> запись ->
    подтверждение. Любой сбой — видимый ответ Владу, не тихий (guard поллера
    продолжает приём в любом случае)."""
    msg = update.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id") or OWNER_CHAT_ID)
    file_id, filename, is_photo = _attachment(update)
    if not file_id:
        logger.warning("registrar: апдейт %s без вложения — пропущен", update.get("update_id"))
        return
    update_id = update.get("update_id")
    try:
        content = telegram.download_file(file_id)
        mime = _mime_for(filename, is_photo)
        if mime is None:
            logger.warning("registrar: неподдерживаемый тип файла %r (update %s)", filename, update_id)
            _reply(chat_id, UNSUPPORTED_REPLY)
            return

        cls = classify_document(content, mime)  # шаг 1
        if cls["kind"] == "not_document":
            logger.info("registrar: update %s — не документ (%s), ничего не записано",
                        update_id, cls["reason"])
            _reply(chat_id, NOT_DOCUMENT_REPLY.format(reason=cls["reason"] or "похоже на обычное фото"))
            return

        today = _vl_today()
        doc = extract_document(content, mime, today)  # шаг 2
        mapped = map_markers(doc["markers"], _fetch_marker_rows())
        date_iso = parse_document_date(doc["document_date"], today) or today

        if not mapped["rows"]:  # честный отказ: ничего не пишем
            logger.info("registrar: update %s — 0 распознанных показателей (kind=%s)",
                        update_id, cls["kind"])
            _reply(chat_id, build_reply(date_iso, mapped, cls["kind"]))
            return

        visit_ref = persist_document(doc, mapped, date_iso)  # шаг 3: card.* (канон)
        health_note = ""
        try:
            health_visit_id = persist_health(doc, mapped, date_iso)  # шаг 3b: дуал-райт
            logger.info("registrar: update %s -> card:%s / health:%s, %d показателей, нераспознано: %s",
                        update_id, visit_ref, health_visit_id, len(mapped["rows"]),
                        "; ".join(mapped["unmatched"]) or "нет")
        except Exception:
            # card.* уже зафиксирован — сбой старой витрины не рвёт ответ, но и не молчит
            logger.exception("registrar: дуал-райт в health.* не удался (card.%s записан), update %s",
                             visit_ref, update_id)
            health_note = "\n⚠️ Записал в карту, но данные не доехали до старой базы (панель PhenoAge их не увидит)."
        _reply(chat_id, build_reply(date_iso, mapped, cls["kind"]) + health_note)  # шаг 4
    except Exception:
        logger.exception("registrar: сбой обработки update %s", update_id)
        _reply(chat_id, FAILURE_REPLY)
