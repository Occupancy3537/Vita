"""Бэклог продуктовых/системных находок (2026-09-23, Шаг 1 «петли
самоулучшения продукта» — Влад: «есть проверка ошибок и логи, но пока я не
скажу исправить, никто не исправляет»).

Раньше сбой фонового цикла или ночного cron-скрипта уходил ТОЛЬКО в
Telegram-алерт (app/err_dedup.py, дедуп 60 мин) — сообщение прокручивается в
ленте и теряется: ничего не хранит «это уже было 5 раз», ничего не
расставляет приоритеты, и главное — ничто не инициирует пересмотр САМО, без
явной просьбы Влада. card.issue_log — durable-бэклог: одна строка на
РЕАЛЬНУЮ проблему (дедуп по natural_key), не на каждое срабатывание,
закрывается вручную/скриптом при деплое фикса (resolve_issue), никогда не
закрывается сама по себе.

Область НАМЕРЕННО ограничена системными/продуктовыми находками (сбой цикла,
находка код-ревью/аудита) — НЕ включает health.anomaly_log (аномалии
биомаркеров Влада) и card.rf_event (красные флаги неотложных состояний): у
обоих уже есть свой доведённый до конца контур (anomaly_log ->
weekly_advisor -> recommendations_log; rf_event -> экстренная эскалация) и
своя семантика «исправить» (Владу изменить образ жизни / врачу
среагировать — не Клоду написать код). Смешивать их с этим бэклогом было бы
category error, а не экономией одной таблицы на всё.

Пока единственный автоматический источник — app/err_dedup.py::run_notify()
(единая точка входа и для alert_on_failure/фоновых циклов, и для трёх
ночных cron-скриптов — см. её докстринг). Находки код-ревью/аудитов (как
дашборд-анализ 2026-09-22) добавляются тем же record_issue() из разового
скрипта — функция не завязана на err_dedup ничем, кроме текущего вызывающего.

Fail-safe (record_issue): запись сюда никогда не бросает — вызывается из уже
существующих except-путей, вторичный сбой здесь не должен маскировать
исходную ошибку (тот же принцип, что у app/run_log.py). resolve_issue —
осознанное исключение: это ручное действие при закрытии находки, не
автоматический побочный эффект детектора, пусть бросает как обычный SQL-вызов."""
import logging

from app.db import schema

logger = logging.getLogger(__name__)

_VALID_SEVERITY = {"critical", "important", "minor"}


def record_issue(cur, natural_key: str, source: str, summary: str, severity: str = "important") -> None:
    """Записать/обновить одну находку. Повтор одного natural_key: last_seen
    и occurrences всегда растут, summary обновляется на свежий текст (видно
    последнее проявление, не первое); 'fixed' -> 'open' (повтор чинённого —
    сам по себе сигнал, заслуживает нового взгляда, не тихого молчания);
    'snoozed'/'wontfix' — статус НЕ трогаем (это уже принятое решение), но
    occurrences/last_seen всё равно растут, чтобы решение было видно на
    актуальных данных, а не заморожено на дате принятия."""
    if severity not in _VALID_SEVERITY:
        severity = "important"
    try:
        cur.execute(
            "INSERT INTO {t} (natural_key, source, severity, summary) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (natural_key) DO UPDATE SET "
            "last_seen = now(), occurrences = {t}.occurrences + 1, summary = EXCLUDED.summary, "
            "status = CASE WHEN {t}.status = 'fixed' THEN 'open' ELSE {t}.status END"
            .format(t=schema() + ".issue_log"),
            (natural_key, source, severity, (summary or "")[:800]),
        )
    except Exception:
        logger.warning("issue_log: не удалось записать находку %r", natural_key, exc_info=True)


def resolve_issue(cur, natural_key: str, resolution_ref: str, status: str = "fixed") -> bool:
    """Закрыть находку — вручную/скриптом при деплое фикса, НИКОГДА
    автоматически из детектора. Возвращает False, если такой находки нет
    (опечатка в natural_key не должна тихо создавать новую пустую строку —
    UPDATE, не upsert)."""
    cur.execute(
        "UPDATE {t} SET status = %s, resolved_at = now(), resolution_ref = %s WHERE natural_key = %s"
        .format(t=schema() + ".issue_log"),
        (status, resolution_ref, natural_key),
    )
    return cur.rowcount > 0
