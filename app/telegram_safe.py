"""Живой инцидент (2026-09-23): токен Telegram-бота доктора был скомпрометирован
(старая, известная утечка — см. app/doctor/telegram.py и backups/infra/SECRETS.md,
"утёк в plaintext" ещё в n8n-эру, отложено месяцы назад, эксплуатировано только
сегодня) — кто-то повесил свой вебхук / держал параллельный опрос getUpdates
тем же токеном. При разборе инцидента обнаружен ЖИВОЙ, ТЕКУЩИЙ источник риска
того же класса: каждый вызов Telegram Bot API строит URL вида
"https://api.telegram.org/bot<TOKEN>/method" — httpx.raise_for_status() на
ошибке поднимает исключение, чьё текстовое представление несёт этот URL
ЦЕЛИКОМ (см. "httpx.HTTPStatusError: ... for url '...bot<TOKEN>/getUpdates'" —
ровно так этот инцидент и был впервые замечен, в `docker logs`). Любой
logger.exception() дальше по цепочке печатает токен в логи контейнера открытым
текстом. Dozzle, который выставлял логи контейнера наружу без авторизации, уже
убран (2026-09-23, D1) — но сама утечка в логи оставалась бы жива при любом
другом способе добраться до docker logs (SSH, будущая переконфигурация). Токен
OpenRouter/других API-ключей эта проблема не касается — они едут в заголовке
Authorization, не в URL, httpx их в текст исключения не включает."""
import re

import httpx

_TOKEN_IN_URL_RE = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")


def redact(text: str) -> str:
    return _TOKEN_IN_URL_RE.sub("/bot***", str(text))


def raise_for_status_safe(resp: httpx.Response) -> None:
    """resp.raise_for_status(), но с токеном бота вычищенным из сообщения
    ДО того, как исключение уйдёт наверх и, возможно, в лог. Тип исключения
    сознательно сужен до RuntimeError — ни один вызывающий в проекте не
    матчит httpx.HTTPStatusError по типу (везде голый except Exception),
    сохранять оригинальный тип ради этого не нужно."""
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(redact(str(e))) from None
