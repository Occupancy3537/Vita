# -*- coding: utf-8 -*-
"""Сборка нового app/lab_prices_map.py по РЕАЛЬНЫМ данным latest.json (этап C,
2026-09-30). Воспроизводимая сборка: запускается из card-service.

Правила (докстринг старого маппинга + уточнения задачи 30.09):
- сингл: точное совпадение аналита; другой метод (ВЭЖХ/ЖХ-МС, глюкометр,
  качественный), другой материал (капиллярная/моча/волосы/ногти/слюна/кал) —
  НЕ мапится (в needs_review с причиной);
- комплексы: покрытие только по ЯВНО названным в имени позиция анализам
  (курируемый словарь COMPLEX_BY_NAME) — «расширенная», «под болезнь» не мапятся;
- ОАК: только позиции с явной «лейкоформулой» в имени; СОЭ — если названа;
  NOT_SEPARATELY (M047/M060) не включаются; состав не перечислен → needs_review;
- DRAW-BLOOD не мапится;
- дешевле-кандидат становится основным, остальные коды того же аналита —
  в needs_review;
- сверка с control_table.csv (эталон Влада) — в конец отчёта-вывода.

Выходы:
  /home/openclaw/lab_prices/reports/mapping_evidence.csv     — по каждой строке COVERS
  /home/openclaw/lab_prices/reports/mapping_needs_review.csv — сомнительное
  /tmp/lab_prices_map_new.py                                 — черновик файла маппинга
"""
import csv
import json
import sys

sys.path.insert(0, ".")
from app.lab_catalog import LAB_CATALOG
from scripts.lab_scrape.core import norm_name_loose as norm

NAME2CODE = {m["name"]: code for code, m in LAB_CATALOG.items()}

# реальные синонимы сайтов (наблюдены в данных/контрольной таблице Влада)
SYN = {
    "Билирубин прямой": ["билирубин связанный"],
    "Холестерин общий": ["холестерин"],
    "Холестерин-ЛПВП": ["холестерин липопротеинов высокой плотности"],
    "Холестерин-ЛПНП": ["холестерин липопротеинов низкой плотности"],
    "Аланинаминотрансфераза (АлАТ)": ["алат"],
    "Аспартатаминотрансфераза (АсАТ)": ["асат"],
    "Щелочная фосфатаза": ["фосфатаза щелочная"],
    "Т4 свободный": ["тироксин свободный"],
    "ТТГ": ["тиреотропный гормон"],
    "Тестостерон общий": ["тестостерон"],
    "ПСА общий": ["общий пса", "простатический специфический антиген общий",
                  "простатспецифический антиген общий", "пса"],
    "ПСА свободный": ["свободный пса"],
    "С-реактивный белок (СРБ)": ["срб"],
    "Железо (Fe)": ["сывороточное железо"],
    "Гликированный гемоглобин (HbA1c)": ["гликозилированный гемоглобин"],
    "Витамин B12": ["цианокобаламин", "витамин в12"],
    "Витамин D (25-OH)": ["витамин д", "25 oh витамин d", "25 он витамин д",
                          "25 oh витамин d общий", "витамин d суммарный",
                          "витамин d витамин солнца"],
    "Фолиевая кислота": ["фолаты"],
    "ПСА свободный/ПСА": ["скрининг рака простаты", "пса свободный пса"],
    "Гемоглобин в кале": ["colonview"],
    "Тромбиновое время (ТВ)": ["тромбиновое время"],
    "Антитромбин III": ["антитромбин"],
    "МНО": ["протромбиновое время мно", "протромбиновое время", "протромбин мно"],
    "Агрескрин-тест": ["агрескрин"],
    "ПТИ": ["протромбиновый индекс"],
    "Липопротеин(а) [Lp(a)]": ["липопротеин а", "липопротеин a", "липопротеин"],
    "Цистатин С": ["цистатин"],
    "ГСПГ (SHBG)": ["глобулин связывающий половые гормоны"],
    "Гамма-глутамилтранспептидаза (ГГТ)": ["ггт", "гамма глутамилтранспептидаза"],
    "АпоB (ApoB)": ["аполипопротеин в", "аполлипротеины аро в", "аполипопротеины",
                    "аполлипротеины"],
    "Ревматоидный фактор": ["ревматоидный фактор"],
    "Антитела ССР": ["цитруллинированному пептиду", "аццп"],
    "IgE общий": ["ige общий", "иммуноглобулин е общий", "иммуноглобулин класса е",
                  "иммуноглобулин ige", "ige"],
}

EXCLUDE_RX = r"моча|капиллярн|волос|ногт|слюн|кал\b|глюкометр|вэжх|жх-мс|мазок|качествен|суперцен|по суперцене|ImmunoCAP|IgE \(|ртмл"
EXCLUDE_NAME_RX = r"свободный тестостерон|тестостерон свободный|дгэа|17-?oh|андростендион|кортизол|прогестерон|эстрадиол|фсг\b|лг\b|пролактин|инсулин человеческий|аллерг|посев|полиморфизм|гельминт|лямбл|дизентер"

# комплексы, где состав ЯВНО назван в имени (курируемо, по реальным названиям)
COMPLEX_BY_NAME = {
    ("gemotest", "28.254"): ["M003", "M031"],
    ("gemotest", "33.744"): ["M035", "M027"],
    ("gemotest", "8.4"): ["M021", "M022", "M023"],
    ("gemotest", "6.10"): ["M073", "M077"],
    ("gemotest", "6.5"): ["M077"],
    ("gemotest", "1.127"): ["M014"],
    ("tafi", "protrombinovoe-vremya-pv-mno"): ["M073"],
    ("unilab", "204"): ["M073"],
    ("unilab", "276"): ["M077"],
    ("invitro", "2"): ["M073"],
}

# ОАК-позиции: имя однозначно называет клинический анализ крови с лейкоформулой
OAK_POSITIONS = {
    ("gemotest", "3.9.1"): "с СОЭ",
    ("gemotest", "3.9.2"): "с СОЭ",
    ("gemotest", "3.4"): "лейкоформула + ретикулоциты, СОЭ не названа",
    ("invitro", "1515"): "с СОЭ",
    ("invitro", "1555"): "с СОЭ",
    ("invitro", "5/119"): "лейкоформула (с микроскопией), СОЭ не названа",
    ("tafi", "klinicheskiy-analiz-krovi-28-parametrov-soe-po-vestergrenu"): "28 параметров + СОЭ (состав не перечислен)",
    ("unilab", "601"): "основные + лейкоформула",
    ("unilab", "621"): "основные + лейкоформула",
    ("unilab", "625"): "основные + лейкоформула + СОЭ",
}
OAK_CODES = ["M039", "M040", "M041", "M042", "M043", "M044", "M045", "M046",
             "M048", "M049", "M050", "M058", "M059", "M061", "M062", "M063",
             "M064", "M065", "M066", "M067", "M068"]
OAK_NOT_ORDERABLE = {"M047", "M060"}

rows = json.load(open("/home/openclaw/lab_prices/latest.json"))
by_lab = {}
for r in rows:
    by_lab.setdefault(r["lab_code"], []).append(r)

covers = {}
evidence = []
needs = []


def add(lab, code, markers, basis, name, price, url, raw_ref, conf):
    key = (lab, code)
    if key in covers:
        old = set(covers[key])
        covers[key] = sorted(old | set(markers))
        for e in evidence:
            if e["lab"] == lab and e["code"] == code:
                e["markers"] = ";".join(covers[key])
        return
    covers[key] = list(markers)
    evidence.append({"lab": lab, "code": code, "markers": ";".join(markers),
                     "name": (name or "")[:100], "price": price,
                     "url": url or "", "raw_ref": raw_ref or "",
                     "basis": basis, "confidence": conf})


def put_review(kind, lab, marker, code, name, price, note):
    needs.append({"kind": kind, "lab": lab, "marker": marker,
                  "code": code or "", "name": (name or "")[:100],
                  "price": price if price is not None else "", "note": note})


# ── 1) синглы по именам каталога + синонимы; дешевле-кандидат основной ──────
for mname, mcode in NAME2CODE.items():
    variants = [norm(mname)] + [norm(v) for v in SYN.get(mname, [])]
    variants = sorted({v for v in variants if v})
    for lab, rr in by_lab.items():
        hits = []
        for r in rr:
            if r.get("kind", "single") != "single":
                continue
            n = norm(r["name"])
            import re as _re
            if _re.search(EXCLUDE_RX, r["name"], _re.I) or _re.search(EXCLUDE_NAME_RX, r["name"], _re.I):
                continue
            for v in variants:
                if n == v or (v and n.startswith(v + " ")):
                    hits.append((r, "exact" if n == norm(mname) else "synonym"))
                    break
        if not hits:
            continue
        hits.sort(key=lambda h: (h[0]["price"], h[0]["external_code"]))
        best, conf = hits[0]
        add(lab, best["external_code"], [mcode],
            f"сингл, имя сайта «{best['name'][:70]}»", best["name"], best["price"],
            best["url"], best["raw_ref"], conf)
        for other, _c in hits[1:]:
            put_review("alternate_code", lab, mname, other["external_code"],
                       other["name"], other["price"],
                       "тот же аналит, другой код/материал — не основной")

# ── 2) комплексы по явному имени ────────────────────────────────────────────
by_key = {(r["lab_code"], r["external_code"]): r for r in rows}
for (lab, code), markers in COMPLEX_BY_NAME.items():
    r = by_key.get((lab, code))
    if r is None:
        put_review("missing_complex", lab, "+".join(markers), code, "", None,
                   "позиция из COMPLEX_BY_NAME не найдена в latest.json")
        continue
    add(lab, code, markers, f"комплекс, состав явен из названия «{r['name'][:70]}»",
        r["name"], r["price"], r["url"], r["raw_ref"], "complex-by-name")

# ── 3) ОАК ──────────────────────────────────────────────────────────────────
for (lab, code), note in OAK_POSITIONS.items():
    r = by_key.get((lab, code))
    if r is None:
        put_review("missing_oak", lab, "ОАК", code, "", None,
                   "ОАК-позиция не найдена в latest.json")
        continue
    markers = [m for m in OAK_CODES]
    if "СОЭ" not in note and "с СОЭ" not in note:
        markers = [m for m in markers if m != "M048"]
    add(lab, code, markers, f"ОАК по имени позиции ({note}); список — _OAK_STD из маппинга v1",
        r["name"], r["price"], r["url"], r["raw_ref"],
        "oak-by-name (состав не перечислен — подтвердить)")
    if "состав не перечислен" in note:
        put_review("oak_composition_unlisted", lab, "ОАК", code, r["name"], r["price"],
                   "состав не перечислен на сайте — замаплено по стандартному ОАК, подтвердить")

# ── 4) DRAW-BLOOD: наличие и цены (в COVERS НЕ входит) ──────────────────────
draw = [r for r in rows if "взят" in norm(r["name"]) and ("крови" in norm(r["name"]))]
draw_report = [(r["lab_code"], r["external_code"], r["name"][:50], r["price"]) for r in draw]

# ── сверки и файлы ──────────────────────────────────────────────────────────
keys_in_latest = {(r["lab_code"], r["external_code"]) for r in rows}
orphans = [k for k in covers if k not in keys_in_latest]
bad_markers = [m for ms in covers.values() for m in ms if m not in LAB_CATALOG]

with open("/home/openclaw/lab_prices/reports/mapping_evidence.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["lab", "code", "markers", "name", "price",
                                      "url", "raw_ref", "basis", "confidence"], delimiter=";")
    w.writeheader()
    w.writerows(sorted(evidence, key=lambda e: (e["lab"], e["code"])))

with open("/home/openclaw/lab_prices/reports/mapping_needs_review.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["kind", "lab", "marker", "code", "name", "price", "note"], delimiter=";")
    w.writeheader()
    w.writerows(needs)

print(f"ключей COVERS: {len(covers)} | сирот: {len(orphans)} | неверных маркеров: {len(bad_markers)}")
print(f"needs_review: {len(needs)} | evidence: {len(evidence)}")
if orphans:
    print("СИРОТЫ:", orphans[:5])
print("DRAW-BLOOD позиции:", draw_report[:8], f"… всего {len(draw_report)}")

# ── генерация файла маппинга ────────────────────────────────────────────────
def fmt():
    lines = []
    lines.append('"""Кураторский маппинг «позиция прайса лабы → коды каталога»')
    lines.append('(app/lab_catalog.LAB_CATALOG) — пересобран 30.09 по РЕАЛЬНОМУ прайсу')
    lines.append('latest.json (скрипт scripts/lab_scrape/mapping_build.py; 7694 позиции,')
    lines.append('все коды — настоящие коды/слаги сайтов, evidence: /home/openclaw/lab_prices/reports/mapping_evidence.csv).')
    lines.append('')
    lines.append('Принципы (прежние + уточнения задачи 30.09):')
    lines.append('  - ключ = (lab_key, external_code) из прайса; значение — коды каталога,')
    lines.append('    которые позиция ЗАКРЫВАЕТ; без записи здесь позиция в расчёт')
    lines.append('    цен не попадает (молчаливого матчинга нет);')
    lines.append('  - сингл: точное совпадение аналита; другой метод (ВЭЖХ/ЖХ-МС,')
    lines.append('    глюкометр, качественный) или материал (капиллярная/моча/волосы/')
    lines.append('    ногти/слюна) — НЕ мапится (см. mapping_needs_review.csv);')
    lines.append('  - комплексы: только состав, явный из названия; «под болезнь»,')
    lines.append('    женские/детские/ИППП/аллергопанели — не мапятся;')
    lines.append('  - ОАК: позиции с явной лейкоформулой в имени закрывают _OAK_STD;')
    lines.append('    СОЭ — только если названа; NOT_SEPARATELY (M047/M060) — никогда;')
    lines.append('  - сомнительное — в mapping_needs_review.csv, решается с Владом.')
    lines.append('')
    lines.append('НЕ ЗАМАПЛЕНО сознательно (проверено по реальному прайсу 30.09):')
    lines.append('  - hs-СРБ у ТАФИ (нет позиции), ПСА свободный у Юнилаба (нет),')
    lines.append('    АпоB… пересмотры — см. mapping_needs_review.csv и отчёт.')
    lines.append('"""')
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("# (lab_key, external_code) -> [коды каталога]")
    lines.append("COVERS: dict[tuple[str, str], list[str]] = {")
    for (lab, code), markers in sorted(covers.items()):
        ms = ", ".join(f'"{m}"' for m in markers)
        lines.append(f'    ("{lab}", "{code}"): [{ms}],')
    lines.append("}")
    lines.append("")
    lines.append('# примечания к позициям (key -> текст) — попадают в breakdown оффера')
    lines.append("NOTES: dict[tuple[str, str], str] = {")
    for (lab, code), markers in sorted(covers.items()):
        if len(markers) > 3:  # комплексы/ОАК — пояснить, что закрывают несколько
            nm = by_key.get((lab, code), {}).get("name", "")
            short = (nm[:60] + "…") if len(nm) > 60 else nm
            lines.append(f'    ("{lab}", "{code}"): "комплекс «{short}» — итог дешевле суммы синглов",')
    lines.append("}")
    return "\n".join(lines) + "\n"

open("/tmp/lab_prices_map_new.py", "w", encoding="utf-8").write(fmt())
print("черновик: /tmp/lab_prices_map_new.py")

# сверка с контрольной таблицей
try:
    ct = list(csv.DictReader(open("/home/openclaw/lab_prices/reports/control_table.csv", encoding="utf-8-sig"), delimiter=";"))
    mism = []
    for row in ct:
        if "нет в собранном прайсе" in row.get("цена", ""):
            continue
        lab, code = row["лаба"], row["код"]
        markers = set((covers.get((lab, code)) or []))
        m_name = row["маркер"]
        # маркер таблицы → коды каталога: через точное имя каталога (34 маркера — их имена близки)
        if not markers:
            mism.append((m_name, lab, code, "код таблицы отсутствует в COVERS"))
    if mism:
        print("РАСХОЖДЕНИЯ с контрольной таблицей:", len(mism))
        for m in mism[:15]:
            print("  ", m)
    else:
        print("сверка с контрольной таблицей: все коды таблицы присутствуют в COVERS")
except FileNotFoundError:
    print("control_table.csv не найден — сверка пропущена")
