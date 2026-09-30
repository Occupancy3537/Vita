
import json, re
rows = json.load(open("/home/openclaw/lab_prices/latest.json"))
print("=== Инвитро: additional_services (из listing) ===")
import httpx
city = "7c9b62af-a2a2-42ca-a84d-657ea4819aa5"
c = httpx.Client(headers={"User-Agent": "Mozilla/5.0 VL-verify"}, timeout=25)
r = c.get(f"https://www.invitro.ru/golk/tests/api/v1/tests?cityID={city}&limit=10&offset=0")
d = r.json()
for b in d.get("data", []):
    for p in b.get("products", [])[:3]:
        svc = p.get("additional_services") or []
        if svc:
            print("  ", p.get("bitrix_id"), p.get("title", "")[:40], "| доп:", svc)
            break
    break
print()
print("=== Юнилаб взятие крови ===")
for r in rows:
    if r["lab_code"] == "unilab" and re.search(r"взят|забор", r["name"], re.I):
        print("  ", r["external_code"], "|", r["name"][:60], "|", r["price"])
print()
print("=== Инвитро 240/2401/842 — названия ===")
for code in ("240", "2401", "842"):
    r = next((x for x in rows if x["lab_code"]=="invitro" and x["external_code"]==code), None)
    if r:
        print(" ", code, "|", r["name"][:75], "|", r["price"])
print()
print("=== Гемотест: гематология (для ОАК-покрытия) ===")
hits = [(r["external_code"], r["name"][:60], r["price"]) for r in rows
        if r["lab_code"] == "gemotest" and re.search(r"клинический анализ|общий анализ крови", r["name"], re.I)]
for h in hits[:5]:
    print("  ", h)
