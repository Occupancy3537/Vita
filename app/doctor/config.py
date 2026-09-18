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
#
# 2026-09-18: Влад — «хочу, чтобы доктор был умным сам, без ручных правил под
# каждый случай» (см. AGENT_SYNC.md-стиль разбор кейса про B12/Nutrition_Analyzer).
# Первый шаг из предложенного меню — не патчить промпт под конкретные пропуски,
# а поднять саму способность модели рассуждать: GLM 5.3 (не Flash) + эффорт
# рассуждения не 'low', а полный. Дороже по токену (~10х completion-цена у
# Z.AI на OpenRouter), но абсолютные суммы всё равно центы за консультацию —
# цена другого порядка, не другого масштаба бюджета. Crusoe не обслуживает
# полный GLM 5.3 (только Flash) — оставлен в списке безвредно: allow_fallbacks
# просто пропустит его, обслужат Fireworks/BaseTen (проверено через
# /models/z-ai/glm-5.3/endpoints перед переключением, не угадывалось).
DOCTOR_MODEL = os.environ.get("DOCTOR_MODEL", "z-ai/glm-5.3")
DOCTOR_PROVIDER_ORDER = os.environ.get("DOCTOR_PROVIDER_ORDER", "Crusoe,Fireworks,BaseTen").split(",")
DOCTOR_REASONING_EFFORT = os.environ.get("DOCTOR_REASONING_EFFORT", "high")

# §3.4 — бюджет агентного цикла. Подняты вместе с переходом на полный GLM 5.3 +
# effort=high — прежние 60с/раунд-30с были откалиброваны под Flash с effort=low
# (наблюдался обрыв ответа по finish_reason=length именно от нехватки лимита
# на рассуждение, см. STATE.md 2026-09-16 инцидент с дневником питания — тот же
# класс модели, тот же риск, если не поднять бюджет вместе с эффортом).
TURN_DEADLINE_SECONDS = float(os.environ.get("DOCTOR_TURN_DEADLINE_SECONDS", "180"))
MODEL_CALL_TIMEOUT_SECONDS = float(os.environ.get("DOCTOR_MODEL_CALL_TIMEOUT_SECONDS", "90"))
TOOL_TIMEOUT_SECONDS = float(os.environ.get("DOCTOR_TOOL_TIMEOUT_SECONDS", "4"))
MAX_TOOL_ROUNDS = int(os.environ.get("DOCTOR_MAX_TOOL_ROUNDS", "3"))

# §3.3 — окно разговорной памяти (по аналогии с нынешним Simple Memory: 10 ходов/6ч).
SESSION_WINDOW_TURNS = int(os.environ.get("DOCTOR_SESSION_WINDOW_TURNS", "10"))
SESSION_TTL_HOURS = float(os.environ.get("DOCTOR_SESSION_TTL_HOURS", "6"))

# Индикатор "печатает" гаснет в Telegram сам через ~5с — обновляем чаще.
TYPING_REFRESH_SECONDS = float(os.environ.get("DOCTOR_TYPING_REFRESH_SECONDS", "4.0"))
