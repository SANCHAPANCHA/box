"""Operational alerts (monitoring errors) sent directly to Telegram."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from sqlalchemy import select

from core.database import SessionFactory, session_scope
from models import Subscription, User, XAccount
from repositories import KeyValueRepository
from services.formatting import esc
from services.timeutils import utcnow

log = logging.getLogger(__name__)

SendFunc = Callable[[int, str], Awaitable[None]]
GLOBAL_ALERT_COOLDOWN = timedelta(hours=1)

GLOBAL_ERROR_TEXT = {
    "auth": "X API отклонил токен (401). Проверьте <code>X_BEARER_TOKEN</code> в .env.",
    "payment": "X API вернул 402 Payment Required: закончились кредиты pay-per-use или не настроена оплата в "
    "console.x.com. Мониторинг приостановлен до пополнения баланса.",
    "forbidden": "X API вернул 403 Forbidden: у приложения нет доступа к эндпоинту чтения постов. "
    "Проверьте проект и приложение в console.x.com.",
}


class Alerter:
    def __init__(self, session_factory: SessionFactory, send: SendFunc) -> None:
        self._sf = session_factory
        self._send = send

    async def _recipients(self, account_id: int | None) -> list[int]:
        async with self._sf() as session:
            stmt = select(User.chat_id).where(User.error_notifications_enabled.is_(True))
            if account_id is not None:
                stmt = stmt.join(Subscription, Subscription.user_id == User.id).where(
                    Subscription.x_account_id == account_id
                )
            return list(dict.fromkeys(await session.scalars(stmt)))

    async def _broadcast(self, account_id: int | None, text: str) -> None:
        for chat_id in await self._recipients(account_id):
            try:
                await self._send(chat_id, text)
            except Exception:  # alerts must never break monitoring
                log.exception("failed to deliver alert to chat %s", chat_id)

    async def account_failed(self, account: XAccount, error: str, unavailable: bool) -> None:
        reason = "аккаунт недоступен (удалён, заблокирован или закрыт)" if unavailable else "ошибка запроса к X API"
        await self._broadcast(
            account.id,
            f"⚠️ <b>Ошибка мониторинга</b>\n\n👤 @{esc(account.username)}\n"
            f"Причина: {reason}\n<code>{esc(error[:300])}</code>\n\n"
            "Мониторинг продолжит попытки с увеличенным интервалом.",
        )

    async def account_recovered(self, account: XAccount) -> None:
        await self._broadcast(account.id, f"✅ Мониторинг @{esc(account.username)} восстановлен.")

    async def global_error(self, kind: str, detail: str) -> None:
        key = f"alert:global:{kind}"
        now = utcnow()
        async with session_scope(self._sf) as session:
            kv = KeyValueRepository(session)
            last = await kv.get(key)
            if last and now.timestamp() - float(last.get("ts", 0)) < GLOBAL_ALERT_COOLDOWN.total_seconds():
                return
            await kv.set(key, {"ts": now.timestamp()})
        text = GLOBAL_ERROR_TEXT.get(kind, "Ошибка X API.")
        await self._broadcast(None, f"🛑 <b>Мониторинг X остановлен</b>\n\n{text}\n\n<code>{esc(detail[:300])}</code>")
