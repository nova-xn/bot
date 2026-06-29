# -*- coding: utf-8 -*-
"""
Neway RP — Discord-бот модерации.

Команды:
    -report @user   — отправить репорт на пользователя
    -cancel  @user  — снять определённые роли с пользователя (только для
                       определённых ролей-модераторов)

Бот закреплён за одним единственным сервером (GUILD_ID) и не будет
выполнять команды на любом другом сервере.
"""

import datetime

import discord
from discord.ext import commands

import config
from cooldown import check_cooldown, check_del_cooldown, check_delus_cooldown

# ============================================================
#  ИНИЦИАЛИЗАЦИЯ БОТА
# ============================================================

intents = discord.Intents.default()
intents.members = True          # нужно, чтобы видеть роли участников и снимать их
intents.message_content = True  # нужно, чтобы читать текст команд с префиксом "-"

bot = commands.Bot(command_prefix=config.PREFIX, intents=intents, help_command=None)


# ============================================================
#  СЛУЖЕБНЫЕ ФУНКЦИИ
# ============================================================

def make_embed(title: str, description: str, color: int) -> discord.Embed:
    """Единый стиль embed-сообщений для всего бота."""
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=config.FOOTER_TEXT)
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed


# ============================================================
#  ГЛОБАЛЬНАЯ ПРОВЕРКА: бот работает ТОЛЬКО на одном сервере
# ============================================================

@bot.check
async def globally_restrict_guild(ctx: commands.Context) -> bool:
    if ctx.guild is None or ctx.guild.id != config.GUILD_ID:
        return False
    return True


# ============================================================
#  СОБЫТИЯ
# ============================================================

@bot.event
async def on_ready():
    print(f"[OK] Бот запущен как {bot.user} (ID: {bot.user.id})")
    print(f"[OK] Работает только на сервере: {config.GUILD_ID}")

    for guild in bot.guilds:
        if guild.id != config.GUILD_ID:
            print(f"[ВНИМАНИЕ] Бот присутствует на чужом сервере: {guild.name} ({guild.id}). "
                  f"Команды там работать не будут.")


@bot.event
async def on_member_join(member: discord.Member):
    """Автовыдача роли каждому новому участнику нашего сервера."""

    # Реагируем только на наш сервер — на случай, если бот вдруг окажется ещё где-то
    if member.guild.id != config.GUILD_ID:
        return

    role = member.guild.get_role(config.AUTOROLE_ON_JOIN_ID)
    if role is None:
        print(f"[ОШИБКА авторолей] Роль с ID {config.AUTOROLE_ON_JOIN_ID} не найдена на сервере.")
        return

    try:
        await member.add_roles(role, reason="Автовыдача роли новому участнику")
        print(f"[OK] Выдана роль '{role.name}' пользователю {member} ({member.id})")
    except discord.Forbidden:
        print(f"[ОШИБКА авторолей] Не хватает прав, чтобы выдать роль '{role.name}'. "
              f"Проверьте, что роль бота выше этой роли в иерархии сервера.")
    except discord.HTTPException as e:
        print(f"[ОШИБКА авторолей] Не удалось выдать роль пользователю {member}: {e}")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    # Если команду вызвали не на нашем сервере или check не прошёл — просто молчим
    if isinstance(error, commands.CheckFailure):
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=make_embed(
            "❌ Не указан участник",
            "Нужно упомянуть пользователя. Пример: `-report @user`",
            config.COLOR_ERROR,
        ))
        return

    if isinstance(error, (commands.MemberNotFound, commands.BadArgument)):
        await ctx.send(embed=make_embed(
            "❌ Пользователь не найден",
            "Не удалось найти упомянутого пользователя на сервере.",
            config.COLOR_ERROR,
        ))
        return

    if isinstance(error, commands.CommandNotFound):
        return  # молча игнорируем несуществующие команды

    print(f"[ОШИБКА] {error}")
    try:
        await ctx.send(embed=make_embed(
            "❌ Произошла ошибка",
            "Что-то пошло не так при выполнении команды. Подробности — в консоли бота.",
            config.COLOR_ERROR,
        ))
    except discord.HTTPException:
        pass


# ============================================================
#  КОМАНДА: -report
# ============================================================

@bot.command(name="report")
async def report(ctx: commands.Context, member: discord.Member = None):
    """Отправить репорт на участника сервера."""

    if member is None:
        await ctx.send(embed=make_embed(
            "❌ Не указан участник",
            "Нужно упомянуть пользователя. Пример: `-report @user`",
            config.COLOR_ERROR,
        ))
        return

    # --- Кулдаун ---
    remaining = check_cooldown(ctx.author)
    if remaining > 0:
        await ctx.send(embed=make_embed(
            "⏳ Подождите",
            f"Эту команду можно использовать раз в некоторое время. "
            f"Попробуйте снова через **{remaining:.0f} сек.**",
            config.COLOR_ERROR,
        ))
        return

    if member.bot:
        await ctx.send(embed=make_embed(
            "❌ Нельзя пожаловаться на бота",
            "Выберите реального участника сервера.",
            config.COLOR_ERROR,
        ))
        return

    log_channel = bot.get_channel(config.REPORT_LOG_CHANNEL_ID)
    if log_channel is None:
        await ctx.send(embed=make_embed(
            "❌ Ошибка конфигурации",
            "Канал для репортов не найден. Сообщите администрации.",
            config.COLOR_ERROR,
        ))
        return

    # --- Собираем последние сообщения упомянутого пользователя по серверу ---
    collected_messages = []
    try:
        for channel in ctx.guild.text_channels:
            perms = channel.permissions_for(ctx.guild.me)
            if not (perms.view_channel and perms.read_message_history):
                continue
            try:
                async for msg in channel.history(limit=200):
                    if msg.author.id == member.id:
                        collected_messages.append(msg)
            except (discord.Forbidden, discord.HTTPException):
                continue

        collected_messages.sort(key=lambda m: m.created_at, reverse=True)
        collected_messages = collected_messages[: config.REPORT_MESSAGE_LIMIT]
    except Exception as e:
        print(f"[ОШИБКА сбора сообщений] {e}")

    # --- Формируем красивый репорт ---
    report_embed = discord.Embed(
        title="🚩 Новый репорт",
        color=config.COLOR_REPORT,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    report_embed.set_thumbnail(url=member.display_avatar.url)

    report_embed.add_field(
        name="📌 Кого репортнули",
        value=f"{member.mention} (`{member}`)\nID: `{member.id}`",
        inline=False,
    )
    report_embed.add_field(
        name="🙋 Кто отправил репорт",
        value=f"{ctx.author.mention} (`{ctx.author}`)\nID: `{ctx.author.id}`",
        inline=False,
    )
    report_embed.add_field(
        name="📍 Канал отправки команды",
        value=f"{ctx.channel.mention}",
        inline=False,
    )

    # --- Данные дискорд-профиля ---
    created_at = member.created_at.strftime("%d.%m.%Y %H:%M (UTC)")
    joined_at = member.joined_at.strftime("%d.%m.%Y %H:%M (UTC)") if member.joined_at else "Неизвестно"
    roles = [r.mention for r in member.roles if r.name != "@everyone"]
    roles_text = ", ".join(roles) if roles else "Нет ролей"

    profile_info = (
        f"**Аккаунт создан:** {created_at}\n"
        f"**Зашёл на сервер:** {joined_at}\n"
        f"**Роли:** {roles_text}"
    )
    report_embed.add_field(name="🪪 Данные профиля", value=profile_info, inline=False)

    # --- Последние сообщения со ссылками ---
    if collected_messages:
        lines = []
        for msg in collected_messages:
            content_preview = msg.content.strip()
            if not content_preview:
                if msg.attachments:
                    content_preview = "*[вложение/файл без текста]*"
                elif msg.embeds:
                    content_preview = "*[embed-сообщение без текста]*"
                else:
                    content_preview = "*[пустое сообщение]*"
            if len(content_preview) > 150:
                content_preview = content_preview[:150] + "…"

            time_str = msg.created_at.strftime("%d.%m %H:%M")
            lines.append(f"[{time_str}]({msg.jump_url}) — {content_preview}")

        messages_text = "\n".join(lines)
        if len(messages_text) > 1024:
            messages_text = messages_text[:1000] + "\n…и другие сообщения"

        report_embed.add_field(
            name=f"💬 Последние сообщения ({len(collected_messages)})",
            value=messages_text,
            inline=False,
        )
    else:
        report_embed.add_field(
            name="💬 Последние сообщения",
            value="Сообщений не найдено (либо бот не имеет доступа к каналам).",
            inline=False,
        )

    report_embed.set_footer(text=config.FOOTER_TEXT)

    await log_channel.send(embed=report_embed)

    # --- Ответ в исходном канале ---
    await ctx.send(embed=make_embed(
        "✅ Репорт отправлен",
        f"Жалоба на {member.mention} успешно зарегистрирована и передана администрации.",
        config.COLOR_SUCCESS,
    ))


# ============================================================
#  КОМАНДА: -cancel
# ============================================================

@bot.command(name="cancel")
async def cancel(ctx: commands.Context, member: discord.Member = None):
    """Снять определённые роли с участника. Доступно только модерации."""

    if member is None:
        await ctx.send(embed=make_embed(
            "❌ Не указан участник",
            "Нужно упомянуть пользователя. Пример: `-cancel @user`",
            config.COLOR_ERROR,
        ))
        return

    # --- Проверка прав (роли) ---
    author_role_ids = {role.id for role in ctx.author.roles}
    if not (author_role_ids & config.CANCEL_ALLOWED_ROLE_IDS):
        await ctx.send(embed=make_embed(
            "⛔ Недостаточно прав",
            "У вас нет доступа к этой команде.",
            config.COLOR_ERROR,
        ))
        return

    # --- Кулдаун (после проверки прав, чтобы не палить кулдаун всем подряд) ---
    remaining = check_cooldown(ctx.author)
    if remaining > 0:
        await ctx.send(embed=make_embed(
            "⏳ Подождите",
            f"Эту команду можно использовать раз в некоторое время. "
            f"Попробуйте снова через **{remaining:.0f} сек.**",
            config.COLOR_ERROR,
        ))
        return

    log_channel = bot.get_channel(config.CANCEL_LOG_CHANNEL_ID)

    # --- Снимаем роли ---
    roles_to_remove = [
        role for role in member.roles if role.id in config.ROLES_TO_REMOVE_ON_CANCEL
    ]

    if not roles_to_remove:
        await ctx.send(embed=make_embed(
            "ℹ️ Нечего снимать",
            f"У {member.mention} не найдено ни одной роли из списка для снятия.",
            config.COLOR_INFO,
        ))
        if log_channel:
            info_embed = make_embed(
                "ℹ️ Cancel — роли не найдены",
                f"**Модератор:** {ctx.author.mention} (`{ctx.author}`)\n"
                f"**Цель:** {member.mention} (`{member}`)\n"
                f"**Результат:** у пользователя не было ни одной из снимаемых ролей.",
                config.COLOR_CANCEL,
            )
            await log_channel.send(embed=info_embed)
        return

    try:
        await member.remove_roles(*roles_to_remove, reason=f"Команда -cancel от {ctx.author}")
    except discord.Forbidden:
        await ctx.send(embed=make_embed(
            "❌ Не удалось снять роли",
            "У бота не хватает прав. Проверьте, что роль бота выше снимаемых ролей в иерархии.",
            config.COLOR_ERROR,
        ))
        return
    except discord.HTTPException as e:
        await ctx.send(embed=make_embed(
            "❌ Ошибка Discord",
            f"Не удалось снять роли из-за ошибки: `{e}`",
            config.COLOR_ERROR,
        ))
        return

    removed_names = ", ".join(role.mention for role in roles_to_remove)

    # --- Ответ исполнителю ---
    await ctx.send(embed=make_embed(
        "✅ Роли сняты",
        f"С участника {member.mention} были сняты роли: {removed_names}",
        config.COLOR_SUCCESS,
    ))

    # --- Лог в канал ---
    if log_channel:
        log_embed = discord.Embed(
            title="📋 Лог команды -cancel",
            color=config.COLOR_CANCEL,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        log_embed.set_thumbnail(url=member.display_avatar.url)
        log_embed.add_field(
            name="🛠 Модератор",
            value=f"{ctx.author.mention} (`{ctx.author}`)\nID: `{ctx.author.id}`",
            inline=False,
        )
        log_embed.add_field(
            name="🎯 Цель",
            value=f"{member.mention} (`{member}`)\nID: `{member.id}`",
            inline=False,
        )
        log_embed.add_field(
            name="🗑 Снятые роли",
            value=removed_names,
            inline=False,
        )
        log_embed.add_field(
            name="📍 Канал выполнения",
            value=ctx.channel.mention,
            inline=False,
        )
        log_embed.set_footer(text=config.FOOTER_TEXT)
        await log_channel.send(embed=log_embed)


# ============================================================
#  ОБЩАЯ ПРОВЕРКА ПРАВ ДЛЯ -del / -delus
# ============================================================

def has_delete_access(member: discord.Member) -> bool:
    member_role_ids = {role.id for role in member.roles}
    return bool(member_role_ids & config.DELETE_ALLOWED_ROLE_IDS)


# ============================================================
#  КОМАНДА: -del (массовое удаление сообщений в канале)
# ============================================================

@bot.command(name="del")
async def del_messages(ctx: commands.Context, amount: int = None):
    """Удалить указанное количество последних сообщений в текущем канале."""

    # Удаляем само сообщение с командой сразу, чтобы не мешало (не критично, если не получится)
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass

    # --- Проверка прав ---
    if not has_delete_access(ctx.author):
        await ctx.send(embed=make_embed(
            "⛔ Недостаточно прав",
            "У вас нет доступа к этой команде.",
            config.COLOR_ERROR,
        ), delete_after=8)
        return

    # --- Проверка аргумента ---
    if amount is None:
        await ctx.send(embed=make_embed(
            "❌ Не указано количество",
            f"Укажите, сколько сообщений удалить. Пример: `{config.PREFIX}del 100`",
            config.COLOR_ERROR,
        ), delete_after=8)
        return

    if amount <= 0:
        await ctx.send(embed=make_embed(
            "❌ Неверное количество",
            "Количество сообщений должно быть больше нуля.",
            config.COLOR_ERROR,
        ), delete_after=8)
        return

    if amount > config.DEL_MAX_MESSAGES:
        await ctx.send(embed=make_embed(
            "❌ Превышен лимит",
            f"За один раз можно удалить не более **{config.DEL_MAX_MESSAGES}** сообщений.",
            config.COLOR_ERROR,
        ), delete_after=8)
        return

    # --- Кулдаун (фиксированный, после проверки прав) ---
    remaining = check_del_cooldown(ctx.author.id, config.DEL_COOLDOWN_SECONDS)
    if remaining > 0:
        await ctx.send(embed=make_embed(
            "⏳ Подождите",
            f"Команду `-del` можно использовать раз в {config.DEL_COOLDOWN_SECONDS} сек. "
            f"Попробуйте снова через **{remaining:.0f} сек.**",
            config.COLOR_ERROR,
        ), delete_after=8)
        return

    # --- Удаление ---
    try:
        deleted = await ctx.channel.purge(limit=amount)
    except discord.Forbidden:
        await ctx.send(embed=make_embed(
            "❌ Не удалось удалить сообщения",
            "У бота не хватает прав `Управление сообщениями` в этом канале.",
            config.COLOR_ERROR,
        ), delete_after=8)
        return
    except discord.HTTPException as e:
        await ctx.send(embed=make_embed(
            "❌ Ошибка Discord",
            f"Не удалось удалить сообщения из-за ошибки: `{e}`",
            config.COLOR_ERROR,
        ), delete_after=8)
        return

    # --- Ответ исполнителю (временное сообщение, само исчезнет) ---
    await ctx.send(embed=make_embed(
        "✅ Сообщения удалены",
        f"Удалено **{len(deleted)}** сообщений в {ctx.channel.mention}.",
        config.COLOR_SUCCESS,
    ), delete_after=8)

    # --- Лог в канал ---
    log_channel = bot.get_channel(config.DELETE_LOG_CHANNEL_ID)
    if log_channel:
        log_embed = discord.Embed(
            title="🗑 Лог команды -del",
            color=config.COLOR_DELETE,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        log_embed.add_field(
            name="🛠 Модератор",
            value=f"{ctx.author.mention} (`{ctx.author}`)\nID: `{ctx.author.id}`",
            inline=False,
        )
        log_embed.add_field(
            name="📍 Канал",
            value=ctx.channel.mention,
            inline=False,
        )
        log_embed.add_field(
            name="🔢 Запрошено / Удалено",
            value=f"{amount} / {len(deleted)}",
            inline=False,
        )
        log_embed.set_footer(text=config.FOOTER_TEXT)
        await log_channel.send(embed=log_embed)


# ============================================================
#  КОМАНДА: -delus (удаление сообщений пользователя по всему серверу)
# ============================================================

@bot.command(name="delus")
async def delus_messages(ctx: commands.Context, member: discord.Member = None, amount: int = None):
    """Удалить последние N сообщений указанного пользователя по всем каналам сервера."""

    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass

    # --- Проверка прав ---
    if not has_delete_access(ctx.author):
        await ctx.send(embed=make_embed(
            "⛔ Недостаточно прав",
            "У вас нет доступа к этой команде.",
            config.COLOR_ERROR,
        ), delete_after=8)
        return

    # --- Проверка аргументов ---
    if member is None or amount is None:
        await ctx.send(embed=make_embed(
            "❌ Неверный синтаксис",
            f"Используйте: `{config.PREFIX}delus @пользователь количество`\n"
            f"Пример: `{config.PREFIX}delus @user 100`",
            config.COLOR_ERROR,
        ), delete_after=8)
        return

    if amount <= 0:
        await ctx.send(embed=make_embed(
            "❌ Неверное количество",
            "Количество сообщений должно быть больше нуля.",
            config.COLOR_ERROR,
        ), delete_after=8)
        return

    if amount > config.DELUS_MAX_MESSAGES:
        await ctx.send(embed=make_embed(
            "❌ Превышен лимит",
            f"За один раз можно удалить не более **{config.DELUS_MAX_MESSAGES}** сообщений пользователя.",
            config.COLOR_ERROR,
        ), delete_after=8)
        return

    # --- Кулдаун (фиксированный, после проверки прав) ---
    remaining = check_delus_cooldown(ctx.author.id, config.DELUS_COOLDOWN_SECONDS)
    if remaining > 0:
        await ctx.send(embed=make_embed(
            "⏳ Подождите",
            f"Команду `-delus` можно использовать раз в {config.DELUS_COOLDOWN_SECONDS} сек. "
            f"Попробуйте снова через **{remaining:.0f} сек.**",
            config.COLOR_ERROR,
        ), delete_after=8)
        return

    # --- Уведомление, что процесс начался (поиск по всем каналам может занять время) ---
    status_msg = await ctx.send(embed=make_embed(
        "⏳ Идёт удаление",
        f"Ищу и удаляю до **{amount}** последних сообщений {member.mention} по всем каналам сервера. "
        f"Это может занять некоторое время...",
        config.COLOR_INFO,
    ))

    # --- Собираем сообщения пользователя по всем каналам ---
    target_messages = []
    channels_scanned = 0
    per_channel_breakdown = {}  # channel -> количество найденных сообщений

    for channel in ctx.guild.text_channels:
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.read_message_history and perms.manage_messages):
            continue
        channels_scanned += 1
        found_in_channel = []
        try:
            async for msg in channel.history(limit=config.DELUS_SCAN_LIMIT_PER_CHANNEL):
                if msg.author.id == member.id:
                    found_in_channel.append(msg)
        except (discord.Forbidden, discord.HTTPException):
            continue

        if found_in_channel:
            per_channel_breakdown[channel] = len(found_in_channel)
            target_messages.extend(found_in_channel)

    # Берём только нужное количество (сначала самые новые)
    target_messages.sort(key=lambda m: m.created_at, reverse=True)
    target_messages = target_messages[:amount]

    if not target_messages:
        await status_msg.edit(embed=make_embed(
            "ℹ️ Сообщения не найдены",
            f"Не найдено ни одного сообщения от {member.mention} в доступных боту каналах.",
            config.COLOR_INFO,
        ))
        return

    # --- Группируем найденные сообщения по каналам для удаления через purge/bulk delete ---
    messages_by_channel: dict[discord.TextChannel, list[discord.Message]] = {}
    for msg in target_messages:
        messages_by_channel.setdefault(msg.channel, []).append(msg)

    deleted_total = 0
    deleted_per_channel = {}

    for channel, msgs in messages_by_channel.items():
        try:
            await channel.delete_messages(msgs)  # умеет удалять пачками (до 100 за раз — discord.py разбивает сам)
            deleted_total += len(msgs)
            deleted_per_channel[channel] = len(msgs)
        except discord.HTTPException:
            # Если bulk delete не сработал (например, сообщения старше 14 дней) — удаляем по одному
            ok_count = 0
            for m in msgs:
                try:
                    await m.delete()
                    ok_count += 1
                except discord.HTTPException:
                    continue
            deleted_total += ok_count
            deleted_per_channel[channel] = ok_count

    # --- Ответ исполнителю ---
    await status_msg.edit(embed=make_embed(
        "✅ Сообщения удалены",
        f"Удалено **{deleted_total}** сообщений пользователя {member.mention} "
        f"в **{len(deleted_per_channel)}** канал(ах).",
        config.COLOR_SUCCESS,
    ))

    # --- Лог в канал ---
    log_channel = bot.get_channel(config.DELETE_LOG_CHANNEL_ID)
    if log_channel:
        breakdown_lines = [
            f"{ch.mention} — {count}" for ch, count in deleted_per_channel.items()
        ]
        breakdown_text = "\n".join(breakdown_lines) if breakdown_lines else "—"
        if len(breakdown_text) > 1024:
            breakdown_text = breakdown_text[:1000] + "\n…и другие каналы"

        log_embed = discord.Embed(
            title="🗑 Лог команды -delus",
            color=config.COLOR_DELETE,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        log_embed.set_thumbnail(url=member.display_avatar.url)
        log_embed.add_field(
            name="🛠 Модератор",
            value=f"{ctx.author.mention} (`{ctx.author}`)\nID: `{ctx.author.id}`",
            inline=False,
        )
        log_embed.add_field(
            name="🎯 Цель",
            value=f"{member.mention} (`{member}`)\nID: `{member.id}`",
            inline=False,
        )
        log_embed.add_field(
            name="🔢 Запрошено / Удалено",
            value=f"{amount} / {deleted_total}",
            inline=False,
        )
        log_embed.add_field(
            name="📍 Каналы",
            value=breakdown_text,
            inline=False,
        )
        log_embed.set_footer(text=config.FOOTER_TEXT)
        await log_channel.send(embed=log_embed)


# ============================================================
#  ЗАПУСК
# ============================================================

if __name__ == "__main__":
    if not config.BOT_TOKEN:
        raise SystemExit(
            "Не найден токен бота! Установите переменную окружения BOT_TOKEN "
            "(см. файл .env или README.md)."
        )
    bot.run(config.BOT_TOKEN)
