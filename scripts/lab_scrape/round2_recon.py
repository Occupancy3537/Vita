
import csv, json, sys
sys.path.insert(0, ".")
from scripts.lab_scrape.core import norm_name_loose as norm

gaps = list(csv.DictReader(open("/home/openclaw/lab_prices/reports/claude_review_gaps.csv",
                                encoding="utf-8-sig"), delimiter=";"))
print(f"gaps: {len(gaps)} строк")
rows = json.load(open("/home/openclaw/lab_prices/latest.json"))
by = {}
for r in rows:
    by.setdefault((r["lab_code"], r["external_code"]), r)

print("\n=== по каждой строке: реальная позиция ===")
for g in gaps:
    lab, code = g.get("лаба") or g.get("lab"), g.get("код") or g.get("code")
    markers = g.get("маркеры") or g.get("markers")
    action = g.get("что сделать") or g.get("action") or ""
    r = by.get((lab, code))
    if r:
        print(f"  [{lab}] {code} | {r['name'][:64]} | {r['price']} | маркеры: {markers} | {action[:40]}")
    else:
        print(f"  [{lab}] {code} | НЕТ В LATEST | маркеры: {markers} | {action[:40]}")

print("\n=== поиск похожих позиций (для синонимов) ===")
for pat, lab in [("АЛТ|АлАТ|аланин", "tafi"), ("АСТ|АсАТ|аспартат", "tafi"),
                 ("ГГТ|гамма", "tafi"), ("Белок общий|белк", "unilab"),
                 ("Витамин D|25-?ОН|25-?OH", "unilab"),
                 ("АЧТВ", "unilab"), ("скрыт", "unilab"), ("скрыт", "tafi"),
                 ("МНО|протромбин", "tafi"), ("фибриноген", "tafi"),
                 ("Гемоглобин в кале|скрыт.*кров|ColonView", "gemotest")]:
    hits = [(r["external_code"], r["name"][:60], r["price"], r["kind"])
            for r in rows if (not lab or r["lab_code"] == lab) and
            __import__("re").search(pat, r["name"], __import__("re").I)]
    print(f"  {label if (label:=pat) else ''} @{lab or 'все'}: {len(hits)}")
    for h in hits[:5]:
        print("     ", h)

print("\n=== покрытие маркеров по лабам (было 68/66/63/62 из 81) ===")
from app.lab_prices_map import COVERS
from collections import Counter
cnt = Counter()
for (lab, code), ms in COVERS.items():
    for m in ms:
        cnt[lab] += 1
print(dict(cnt))
print("маркеров в каталоге:", 81)
