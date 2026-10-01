"""Замер высоты экранов и шторок Vita (бюджет компактности, CLAUDE.md «Подача»).

Запуск (одной командой, из корня card-service, с CARD_PG_* в окружении):
    .venv/bin/python tools/ui/measure.py [--w 390] [--h 844] [--write tools/ui/heights.md]

Поднимает харнесс на порту 9199, открывает headless Chrome, жмёт нужные элементы и меряет высоту содержимого
шторки (#ovScreen.scrollHeight) или вкладки в CSS-пикселях. Порог для шторки — 1,3 экрана.
Нужен /usr/bin/google-chrome и пакет websockets."""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

import websockets

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PORT, CDP = 9199, 9344
TABS = ["today", "cases", "plan", "me"]


def tab(i):
    return f"document.querySelectorAll('#tabbar button')[{i}].click()"


def click(sel):
    return f"(function(){{var e=document.querySelector({json.dumps(sel)});if(e)e.click();return !!e}})()"


# (имя, шаги до измерения, что мерить: 'sheet' | селектор вкладки)
SCREENS = [
    ("Прогноз дня", [click("[data-a=index]")], "sheet"),
    ("Фокус дня", [click("[data-a=verdict]")], "sheet"),
    ("Заряд", [click("[data-a=chip][data-k=recovery]")], "sheet"),
    ("Сон", [click("[data-a=chip][data-k=sleep]")], "sheet"),
    ("Движение", [click("[data-a=chip][data-k=move]")], "sheet"),
    ("Питание сегодня", [click("[data-a=chip][data-k=food]")], "sheet"),
    ("Питание · вчера", [click("[data-a=chip][data-k=food]"), click("[data-a=foodyest]")], "sheet"),
    ("Серии", [click("#streaks")], "sheet"),
    ("Сдача анализов", [tab(2), click("[data-a=draw]")], "sheet"),
    ("Консилиум (итог)", [tab(2), click("[data-a=consrep]")], "sheet"),
    ("Заключение врача", [tab(2), click("[data-a=notes]")], "sheet"),
    ("Медпаспорт", [tab(3), click("[data-a=medpass]")], "sheet"),
    ("Вчера (шторка)", [click("[data-a=dayback]"), click("[data-a=chip][data-k=sleep]")], "sheet"),
    ("Вкладка «Проверки»", [tab(1)], "#s-cases"),
    ("Вкладка «Врач»", [tab(2)], "#s-plan"),
    ("Вкладка «Я»", [tab(3)], "#s-me"),
]


async def run(w, h):
    chrome = subprocess.Popen(["/usr/bin/google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
                               f"--remote-debugging-port={CDP}", f"--user-data-dir=/tmp/ui_measure_{int(time.time())}",
                               "--force-prefers-reduced-motion", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rows = []
    try:
        for _ in range(40):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json")); break
            except Exception:
                time.sleep(.25)
        ws_url = [t for t in tabs if t["type"] == "page"][0]["webSocketDebuggerUrl"]
        async with websockets.connect(ws_url, max_size=50_000_000) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1; i = n[0]
                await ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == i:
                        return m

            async def ev(expr):
                r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
                return r["result"]["result"].get("value")

            await call("Page.enable"); await call("Runtime.enable")
            for name, steps, what in SCREENS:
                await call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": True})
                await call("Page.navigate", {"url": f"http://127.0.0.1:{PORT}/harness"})
                await asyncio.sleep(3.2)
                for s in steps:
                    await ev(s); await asyncio.sleep(1.1)
                sel = "#ovScreen" if what == "sheet" else what
                # высота именно содержимого (scrollHeight не меньше высоты экрана и всё «округляет» до неё)
                val = await ev("(function(){var e=document.querySelector(%s),l=e&&e.lastElementChild;"
                               "if(!l)return 0;return Math.round(l.getBoundingClientRect().bottom-e.getBoundingClientRect().top+e.scrollTop)})()" % json.dumps(sel))
                rows.append((name, val, what == "sheet"))
    finally:
        chrome.terminate()
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--w", type=int, default=390)
    ap.add_argument("--h", type=int, default=844)
    ap.add_argument("--write", default="")
    a = ap.parse_args()
    env = dict(os.environ, PYTHONPATH=ROOT, VITA_PASSWORD=os.environ.get("VITA_PASSWORD", "x"),
               VITA_SESSION_SECRET=os.environ.get("VITA_SESSION_SECRET", "harness-secret"), DASHBOARD_TOKEN=os.environ.get("DASHBOARD_TOKEN", "x"))
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "tools.ui.harness:app", "--port", str(PORT), "--log-level", "warning"],
                           cwd=ROOT, env=env)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/harness", timeout=1); break
            except Exception:
                time.sleep(.5)
        rows = asyncio.run(run(a.w, a.h))
    finally:
        srv.terminate()
    budget = int(a.h * 1.3)
    lines = [f"Замер высоты Vita, экран {a.w}×{a.h}, бюджет шторки {budget} px", "",
             "| Экран | Высота, px | Экранов | Бюджет |", "|---|---|---|---|"]
    bad = 0
    for name, val, is_sheet in rows:
        screens = f"{val / a.h:.1f}" if val else "—"
        ok = "" if not is_sheet or not val else ("ок" if val <= budget else "ПРЕВЫШЕН")
        bad += ok == "ПРЕВЫШЕН"
        lines.append(f"| {name} | {val} | {screens} | {ok} |")
    out = "\n".join(lines)
    print(out)
    if a.write:
        open(a.write, "w", encoding="utf-8").write(out + "\n")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
