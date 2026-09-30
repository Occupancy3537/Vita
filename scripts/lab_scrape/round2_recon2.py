
import json, re
rows = json.load(open("/home/openclaw/lab_prices/latest.json"))
for label, pat, lab in [
    ("ТАФИ АЛТ", r"аланинаминотрансфераз|алт\b", "tafi"),
    ("ТАФИ АСТ", r"аспартатаминотрансфераз|аст\b", "tafi"),
    ("ТАФИ ГГТП", r"гамма-?глютамил|ггт", "tafi"),
    ("Юнилаб Белок общий", r"белок общий|общий белок", "unilab"),
    ("Юнилаб вит D", r"25.*ОН.*витамин|витамин.*25", "unilab"),
    ("Юнилаб АЧТВ", r"ачтв", "unilab"),
    ("Юнилаб/ТАФИ скрытая кровь", r"скрыт.*кров", None),
    ("ТАФИ коагулограмма/МНО", r"коагулограмма|протромбиновое.*мно", "tafi"),
    ("Гемотест скрытая кровь — метод?", r"скрыт.*кров", "gemotest"),
    ("Юнилаб 2623 метод?", r"скрыт.*кров", "unilab"),
    ("Гемотест ColonView", r"colonview", "gemotest"),
    ("Инвитро 240/2401", r"скрыт.*кров|гемоглобин.*кале", "invitro"),
    ("Гемотест Гамма-ГТ капиллярный", r"гамма-?гт", "gemotest"),
]:
    hits = [(r["external_code"], r["name"][:68], r["price"])
            for r in rows if (not lab or r["lab_code"] == lab) and re.search(pat, r["name"], re.I)]
    print(f"--- {label}: {len(hits)}")
    for h in hits[:6]:
        print("     ", h)
