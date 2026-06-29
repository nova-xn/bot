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


# ============================================================
#  ОТДЕЛЬНЫЙ КУЛДАУН ДЛЯ КОМАНД УДАЛЕНИЯ (-del / -delus)
# ============================================================
#
# У этих команд фиксированное время кулдауна (не зависит от ролей),
# и оно не должно пересекаться с общим кулдауном -report/-cancel —
# иначе модератор, удалив сообщения, не сможет тут же отправить -cancel.
# Поэтому ведём отдельные словари по каждой команде.

_del_cooldowns: dict[int, float] = {}
_delus_cooldowns: dict[int, float] = {}


def _check_fixed_cooldown(storage: dict[int, float], user_id: int, duration: int) -> float:
    now = time.monotonic()
    expires_at = storage.get(user_id)
    if expires_at is not None and now < expires_at:
        return expires_at - now
    storage[user_id] = now + duration
    return 0


def check_del_cooldown(user_id: int, duration: int) -> float:
    """Кулдаун для команды -del. Возвращает 0, если можно выполнять, иначе — сколько секунд ждать."""
    return _check_fixed_cooldown(_del_cooldowns, user_id, duration)


def check_delus_cooldown(user_id: int, duration: int) -> float:
    """Кулдаун для команды -delus. Возвращает 0, если можно выполнять, иначе — сколько секунд ждать."""
    return _check_fixed_cooldown(_delus_cooldowns, user_id, duration)
