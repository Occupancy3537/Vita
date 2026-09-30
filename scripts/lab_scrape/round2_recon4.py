# -*- coding: utf-8 -*-
"""Разведка 4: взятие крови Инвитро/Юнилаб (по API/страницам); Гемотест 230."""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from vps import client  # noqa: E402

INNER = r'''
import json, re
rows = json.load(open("/home/openclaw/lab_prices/latest.json"))
print("=== Инвитро: additional_services в комплексе (detail) ===")
# additional_services есть в listing: price=280 «Взятие крови из вены»
# проверим на живом API
import httpx
city = "7c9b62af-a2a2-42ca-a84d-657ea4819aa5"
c = httpx.Client(headers={"User-Agent": "Mozilla/5.0 VL-verify"}, timeout=25)
r = c.get(f"https://www.invitro.ru/golk/tests/api/v1/tests?cityID={city}&limit=3&offset=0")
d = r.json()
for b in d.get("data", []):
    for p in b.get("products", [])[:2]:
        svc = p.get("additional_services") or []
        if svc:
            print("  продукт:", p.get("bitrix_id"), p.get("title", "")[:40], "| доп. услуги:", svc)
            break
print()
print("=== Юнилаб: взятие крови в latest ===")
for r in rows:
    if r["lab_code"] == "unilab" and re.search(r"взят|забор|крови из вены", r["name"], re.I):
        print("  ", r["external_code"], "|", r["name"][:60], "|", r["price"])
print()
print("=== Инвитро 240/2401/842: метод из названия (уже видели) ===")
for code in ("240", "2401", "842"):
    r = next((x for x in rows if x["lab_code"]=="invitro" and x["external_code"]==code), None)
    if r: print(" ", code, "|", r["name"][:75], "|", r["price"])
'''
c = client()
try:
    sftp = c.open_sftp()
    with sftp.file('/home/openclaw/longevity-project/card-service/scripts/lab_scrape/round2_recon4.py', 'w') as f:
        f.write(INNER.encode('utf-8'))
    sftp.close()
    si, so, se = c.exec_command(
        f"cd {CARD} && /home/openclaw/lab-scraper/.venv/bin/python scripts/lab_scrape/round2_recon4.py",
        timeout=300)
    sys.stdout.write(so.read().decode('utf-8', 'replace'))
finally:
    c.close()
