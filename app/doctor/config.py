"""
Конфигурация нового доктора (NEW_DOCTOR_PLAN_2026-09-15.md §3.9) — всё из env,
ничего не захардкожено в коде цикла/промпта. Мягкие дефолты здесь нужны только
чтобы модуль спокойно импортировался в тестах без полного набора переменных —
реальные секреты (TELEGRAM_BOT_TOKEN) проверяются на использовании, в
app.doctor.telegram, не здесь (см. её докстринг).
"""
import os

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Модель решается замером на золотом корпусе (план §3.8, шаг 6) — полноценный
# замер (Phase 6) прерван инцидентом с бюджетом OpenRouter 2026-09-15 (workspace
# daily budget), не завершён. 2026-09-16: Влад поднял бюджет до $1.5/день и
# прямо попросил перейти на GLM 5.3 Flash — тот же провайдер-белый список
# (Crusoe/Fireworks/BaseTen), что уже используют остальные воркфлоу n8n этого
# проекта, самый дешёвый вариант. Замена модели — через переменную окружения,
# без правки кода.
DOCTOR_MODEL = os.environ.get("DOCTOR_MODEL", "z-ai/glm-5.3-flash")
DOCTOR_PROVIDER_ORDER = os.environ.get("DOCTOR_PROVIDER_ORDER", "Crusoe,Fireworks,BaseTen").split(",")

# §3.4 — бюджет агентного цикла.
TURN_DEADLINE_SECONDS = float(os.environ.get("DOCTOR_TURN_DEADLINE_SECONDS", "60"))
TOOL_TIMEOUT_SECONDS = float(os.environ.get("DOCTOR_TOOL_TIMEOUT_SECONDS", "4"))
MAX_TOOL_ROUNDS = int(os.environ.get("DOCTOR_MAX_TOOL_ROUNDS", "3"))

# §3.3 — окно разговорной памяти (по аналогии с нынешним Simple Memory: 10 ходов/6ч).
SESSION_WINDOW_TURNS = int(os.environ.get("DOCTOR_SESSION_WINDOW_TURNS", "10"))
SESSION_TTL_HOURS = float(os.environ.get("DOCTOR_SESSION_TTL_HOURS", "6"))

# Индикатор "печатает" гаснет в Telegram сам через ~5с — обновляем чаще.
TYPING_REFRESH_SECONDS = float(os.environ.get("DOCTOR_TYPING_REFRESH_SECONDS", "4.0"))
