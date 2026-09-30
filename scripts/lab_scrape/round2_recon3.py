
import json, re
rows = json.load(open("/home/openclaw/lab_prices/latest.json"))
by = {(r["lab_code"], r["external_code"]): r for r in rows}

print("=== Гемотест 21.3 / 21.21 / 21.10 — состав/метод (composition) ===")
for code in ("21.3", "21.21", "21.10"):
    r = by.get(("gemotest", code))
    if r:
        print(f"  {code} | {r['name'][:50]} | comp: {str(r.get('composition'))[:200]}")

print("\n=== Юнилаб 2623 — метод (ИХА?) ===")
r = by.get(("unilab", "2623"))
if r:
    print("  comp:", str(r.get("composition"))[:300])

print("\n=== Инвитро 842 — Hb+гаптоглобин (это человеческий Hb = M037?) ===")
r = by.get(("invitro", "842"))
if r:
    print("  ", r["name"][:70], "|", r["price"])
    print("  comp:", str(r.get("composition"))[:200])

print("\n=== Инвитро 2401 — количественный (ИХА?) ===")
r = by.get(("invitro", "2401"))
if r:
    print("  ", r["name"][:70], "|", r["price"])

print("\n=== ТАФИ липидограмма — состав ===")
r = by.get(("tafi", "lipidogramma-khn-tg-lpvp-lpnp-lponp"))
if r:
    print("  ", r["name"][:60], "|", r["price"])
    print("  comp:", str(r.get("composition"))[:300])

print("\n=== Юнилаб 129 липидный спектр — состав ===")
r = by.get(("unilab", "129"))
if r:
    print("  ", r["name"][:60], "|", r["price"])
    print("  comp:", str(r.get("composition"))[:300])

print("\n=== Гемотест ColonView 2.6 — есть ли в latest? ===")
r = by.get(("gemotest", "2.6"))
print("  ", (r["name"][:60], r["price"]) if r else "НЕТ (сбор не нашёл — проверить)" )

print("\n=== Инвитро взятие крови (для DRAW fee) ===")
for r in rows:
    if r["lab_code"] == "invitro" and re.search(r"взят|забор", r["name"], re.I):
        print("  ", r["external_code"], "|", r["name"][:60], "|", r["price"])

print("\n=== Гемотест взятие крови ===")
for r in rows:
    if r["lab_code"] == "gemotest" and re.search(r"взят|забор", r["name"], re.I):
        print("  ", r["external_code"], "|", r["name"][:60], "|", r["price"])

print("\n=== ТАФИ взятие крови ===")
for r in rows:
    if r["lab_code"] == "tafi" and re.search(r"взят", r["name"], re.I):
        print("  ", r["external_code"], "|", r["name"][:60], "|", r["price"])

print("\n=== Юнилаб взятие крови ===")
for r in rows:
    if r["lab_code"] == "unilab" and re.search(r"взят|забор", r["name"], re.I):
        print("  ", r["external_code"], "|", r["name"][:60], "|", r["price"])
