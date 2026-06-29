# -*- coding: utf-8 -*-
"""
Кастомная реализация кулдауна.

Зачем своя, а не discord.ext.commands.cooldown?
Потому что нам нужен РАЗНЫЙ кулдаун в зависимости от ролей
пользователя (7 сек для привилегированных, 60 сек для всех
остальных), а встроенный механизм cooldown в discord.py не
умеет менять время динамически в зависимости от вызывающего.
"""

import time
from config import (
    SHORT_COOLDOWN_ROLE_IDS,
    SHORT_COOLDOWN_SECONDS,
    DEFAULT_COOLDOWN_SECONDS,
)

# Структура: {user_id: timestamp_когда_можно_снова}
_cooldowns: dict[int, float] = {}


def get_cooldown_seconds(member) -> int:
    """Возвращает длительность кулдауна для конкретного участника."""
    member_role_ids = {role.id for role in getattr(member, "roles", [])}
    if member_role_ids & SHORT_COOLDOWN_ROLE_IDS:
        return SHORT_COOLDOWN_SECONDS
    return DEFAULT_COOLDOWN_SECONDS


def check_cooldown(member) -> float:
    """
    Проверяет кулдаун пользователя.
    Возвращает:
        0          — если кулдауна нет, можно выполнять команду
        число > 0  — сколько секунд осталось ждать
    Если кулдаун прошёл — обновляет отметку времени автоматически
    (т.е. вызывать только один раз на старте обработки команды).
    """
    now = time.monotonic()
    user_id = member.id

    expires_at = _cooldowns.get(user_id)
    if expires_at is not None and now < expires_at:
        return expires_at - now

    # Кулдауна не было или он истёк — ставим новый
    duration = get_cooldown_seconds(member)
    _cooldowns[user_id] = now + duration
    return 0


def reset_cooldown(user_id: int) -> None:
    """На случай, если когда-нибудь понадобится сбросить кулдаун вручную."""
    _cooldowns.pop(user_id, None)
