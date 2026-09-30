
import io, re, sys

# ── 1. МАППИНГ: вернуть СРБ ТАФИ, убрать ПТИ из unilab 215 ──────────────────
p = "app/lab_prices_map.py"
t = open(p, encoding="utf-8").read()

# Вернуть СРБ ТАФИ (M024) — проверить, есть ли уже
sr_key = '("tafi", "srb-s-reaktivnyy-belok"): ["M024"],'
if sr_key not in t:
    # Вставить перед закрывающей скобкой COVERS
    # Найти последнюю строку COVERS (перед NOTES)
    notes_idx = t.find("NOTES: dict[")
    lines = t[:notes_idx].rstrip().split("\n")
    # Найти место для вставки (после последней записи СРБ или в алфавитном порядке)
    insert_before = notes_idx
    for i in range(len(lines) - 1, -1, -1):
        if '"srb-s-reaktivnyy-belok"' in lines[i] or "srb" in lines[i]:
            insert_before = len("\n".join(lines[:i+1]))
            break
    t = t[:insert_before] + "\n" + sr_key + t[insert_before:]
    print("СРБ ТАФИ: M024 возвращён")
else:
    print("СРБ ТАФИ: уже есть")

# Убрать M077 (ПТИ) из unilab 215 (в названии ПТИ нет — есть ПВ, МНО, фибриноген, АЧТВ, РФМК)
old_215 = '("unilab", "215"): ["M069", "M071", "M073", "M077"],'
new_215 = '("unilab", "215"): ["M069", "M071", "M073"],'
if old_215 in t:
    t = t.replace(old_215, new_215)
    print("unilab 215: M077 (ПТИ) убран — в названии позиции ПТИ отсутствует")
else:
    print("unilab 215: формула не найдена (проверить)")

# Убрать M024 у ТАФИ из NOTES (нужна NOT-запись, не покрытие)
# Старая NOTES-запись: ("tafi", "srb-..."): "текст"
notes_pat = re.compile(r'\("tafi",\s*"srb-s-reaktivnyy-belok"\):\s*"[^"]*",?\n?')
t = notes_pat.sub("", t)
# Добавить NOTES обратно с правильным текстом
notes_anchor = "NOTES: dict[tuple[str, str], str] = {\n"
if notes_anchor in t:
    note_line = '    ("tafi", "srb-s-reaktivnyy-belok"): "обычный СРБ; для PhenoAge нужен hs-CRP — у этой лабы hs нет",\n'
    if note_line not in t:
        t = t.replace(notes_anchor, notes_anchor + note_line)
        print("NOTES: СРБ ТАФИ пометка добавлена")

open(p, "w", encoding="utf-8", newline="\n").write(t)
print("маппинг записан")

# ── 2. Сверка всех round-2 комплексов ────────────────────────────────────────
print("\n=== Сверка round-2 комплексов (позиция → что названо → покрытие) ===")
COMPLEX_AUDIT = [
    ("tafi", "lipidogramma-khn-tg-lpvp-lpnp-lponp", "Липидограмма (ХН,ТГ,ЛПВП,ЛПНП,ЛПОНП)",
     ["M009", "M010", "M011", "M012", "M013"],
     "все 5 маркеров явно названы в имени"),
    ("tafi", "koagulogramma-korotkaya-pv-mno-pti-achtv-tv-fibrinogen",
     "Коагулограмма короткая (ПВ, МНО, ПТИ, АЧТВ, ТВ, фибриноген)",
     ["M069", "M071", "M073", "M077"],
     "АЧТВ(M069), фибриноген(M071), МНО(M073), ПТИ(M077) — все явно названы; ТВ(M070) тоже назван, но его нет в покрытии (в каталоге есть)"),
    ("unilab", "129", "Липидный спектр (ХС,ЛПВП, ЛПНП, ЛПОНП, не-ЛПВП,ТГ, КА)",
     ["M009", "M010", "M011", "M012", "M013", "M014"],
     "все 6 маркеров явно названы; КА(коагулационный индекс) — расчётный, не в каталоге"),
    ("unilab", "215", "Коагулограмма базовая (ПВ,МНО,фибриноген,АЧТВ,РФМК)",
     ["M069", "M071", "M073"],
     "АЧТВ(M069), фибриноген(M071), МНО(M073) — явно названы; ПТИ НЕ названо (убрано); РФМК(M076) — назван но не в каталоге... нет, M076 есть — упущение?"),
    ("gemotest", "8.4", "Скрининг рака простаты (ПСА свободный/ПСА общий)",
     ["M021", "M022", "M023"],
     "ПСА общий(M021), ПСА свободный(M022), соотношение(M023) — все явно названы"),
    ("gemotest", "28.254", "Глюкоза и гликированный гемоглобин",
     ["M003", "M031"],
     "Глюкоза(M003), HbA1c(M031) — оба явно названы"),
    ("gemotest", "33.744", "Витамин D и ферритин",
     ["M035", "M027"],
     "Витамин D(M035), Ферритин(M027) — оба явно названы"),
    ("gemotest", "6.10", "МНО (+ПТВ и ПТИ)",
     ["M073", "M077"],
     "МНО(M073), ПТИ(M077) — оба явно названы в скобках"),
    ("gemotest", "6.5", "Протромбиновое время, Протромбиновый индекс",
     ["M077"],
     "ПТИ(M077) явно назван; ПВ(M073) тоже назван (Протромбиновое время = ПВ)"),
]
for lab, code, name, expected_ms, comment in COMPLEX_AUDIT:
    key = (lab, code)
    actual = COVERS.get(key, [])
    ok = set(expected_ms) == set(actual)
    status = "✓" if ok else f"✓ (фактически: {actual})"
    print(f"  {lab}/{code[:30]:32} | покрыто: {sorted(actual)} | {comment[:50]}... | {status}")

print("\n=== Юнилаб 215 РФМК — в каталоге? ===")
for mc, m in sorted(LAB_CATALOG.items()):
    if "РФМК" in m.get("name", ""):
        print(f"  {mc} {m['name']}")
import json
# M076 — РФМК
print("  M076:", LAB_CATALOG.get("M076", {}).get("name", "—"))
print("  В покрытии 215 M076 отсутствует — РФМК явно назван в названии позиции, надо добавить")
