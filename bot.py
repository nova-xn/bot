# -*- coding: utf-8 -*-
"""
Neway RP — Discord-бот модерации.

Команды:
    -report  @user                   — отправить репорт на пользователя
    -cancel  @user <1|2>             — отозвать лицензию и снять роль
                                        (только в LICENSE_COMMANDS_CHANNEL_ID, кроме админов)
    -license @user <1|2> <channel_id>— выдать лицензию участнику РП
                                        (только в LICENSE_COMMANDS_CHANNEL_ID, кроме админов)
    -data    [запрос]                — база данных лицензий
                                        (только в LICENSE_COMMANDS_CHANNEL_ID, кроме админов)
    -note    @user <текст>           — добавить/изменить заметку к пользователю
    -wipe    @user confirm           — безвозвратно очистить историю лицензий пользователя
    -del     <количество>            — удалить сообщения в канале
                                        (см. config.py: доступ по ролям и категориям)
    -delus   @user <количество>      — удалить сообщения пользователя по серверу
                                        (см. config.py: доступ по ролям и категориям)
    -admin                            — кнопочная админ-панель (BETA-тестирование)

Бот закреплён за одним единственным сервером (GUILD_ID).
"""

import datetime
import re

import discord
from discord.ext import commands, tasks

import config
import license_db
import admin_panel
from cooldown import check_cooldown, check_del_cooldown, check_delus_cooldown

# ============================================================
#  ИНИЦИАЛИЗАЦИЯ
# ============================================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=config.PREFIX, intents=intents, help_command=None)

license_db.init_db()


# ============================================================
#  СЛУЖЕБНЫЕ ФУНКЦИИ
# ============================================================

def make_embed(title: str, description: str, color: int) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=config.FOOTER_TEXT)
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed


def has_role(member: discord.Member, role_ids: set) -> bool:
    return bool({r.id for r in member.roles} & role_ids)


def in_license_commands_channel(ctx: commands.Context) -> bool:
    """
    -license, -cancel и -data можно вызывать только в канале
    config.LICENSE_COMMANDS_CHANNEL_ID.
    Исключение: участники с правом Администратор на сервере — им можно
    из любого канала.
    """
    if ctx.author.guild_permissions.administrator:
        return True
    return ctx.channel.id == config.LICENSE_COMMANDS_CHANNEL_ID


def license_type_label(license_type: int) -> str:
    return "Участника РП" if license_type == 1 else "Менеджера РП"


def license_prefix(license_type: int) -> str:
    return "RPP" if license_type == 1 else "RPM"


def license_role_id(license_type: int) -> int:
    return config.LICENSE_1_ROLE_ID if license_type == 1 else config.LICENSE_2_ROLE_ID


def fmt_dt(iso_str: str | None) -> str:
    if not iso_str:
        return "—"
    try:
        dt = datetime.datetime.fromisoformat(iso_str)
        return dt.strftime("%d.%m.%Y %H:%M UTC")
    except ValueError:
        return iso_str


# ============================================================
#  ГЛОБАЛЬНЫЙ ЧЕК: только наш сервер
# ============================================================

@bot.check
async def globally_restrict_guild(ctx: commands.Context) -> bool:
    return ctx.guild is not None and ctx.guild.id == config.GUILD_ID


# ============================================================
#  ГЛОБАЛЬНЫЙ ЧЕК: не выполнять одно и то же сообщение дважды
# ============================================================
#
# Если Discord по какой-то причине доставит одно и то же событие
# сообщения повторно (переподключение к gateway) — или если по ошибке
# параллельно запущены два процесса бота с одним токеном — этот чек
# гарантирует, что тело команды всё равно выполнится только один раз.
# Проверка атомарна на уровне БД (см. license_db.claim_message), поэтому
# защита работает даже между разными процессами, а не только внутри
# одного.

@bot.check
async def globally_deduplicate_messages(ctx: commands.Context) -> bool:
    try:
        return license_db.claim_message(ctx.message.id)
    except Exception as e:
        # Если БД временно недоступна — не блокируем работу бота из-за
        # этого чека, просто пропускаем защиту и логируем предупреждение.
        print(f"[ПРЕДУПРЕЖДЕНИЕ] Не удалось проверить дубликат сообщения: {e}")
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
            print(f"[ВНИМАНИЕ] Бот присутствует на чужом сервере: {guild.name} ({guild.id})")
    if not cleanup_processed_messages_loop.is_running():
        cleanup_processed_messages_loop.start()


@tasks.loop(hours=6)
async def cleanup_processed_messages_loop():
    """Периодически чистит таблицу processed_messages от старых записей."""
    try:
        deleted = license_db.cleanup_processed_messages(older_than_hours=24)
        if deleted:
            print(f"[OK] Очищено {deleted} старых записей processed_messages.")
    except Exception as e:
        print(f"[ПРЕДУПРЕЖДЕНИЕ] Не удалось очистить processed_messages: {e}")


@bot.event
async def on_member_join(member: discord.Member):
    if member.guild.id != config.GUILD_ID:
        return
    role = member.guild.get_role(config.AUTOROLE_ON_JOIN_ID)
    if role is None:
        print(f"[ОШИБКА авторолей] Роль {config.AUTOROLE_ON_JOIN_ID} не найдена.")
        return
    try:
        await member.add_roles(role, reason="Автовыдача роли новому участнику")
    except discord.Forbidden:
        print(f"[ОШИБКА авторолей] Нет прав на выдачу роли '{role.name}'.")
    except discord.HTTPException as e:
        print(f"[ОШИБКА авторолей] {e}")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CheckFailure):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=make_embed(
            "❌ Не хватает аргументов",
            "Проверьте правильность вызова команды.",
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
        return
    print(f"[ОШИБКА] {error}")
    try:
        await ctx.send(embed=make_embed(
            "❌ Произошла ошибка",
            "Что-то пошло не так. Подробности — в консоли бота.",
            config.COLOR_ERROR,
        ))
    except discord.HTTPException:
        pass


# ============================================================
#  КОМАНДА: -report
# ============================================================

@bot.command(name="report")
async def report(ctx: commands.Context, member: discord.Member = None):
    if member is None:
        await ctx.send(embed=make_embed(
            "❌ Не указан участник",
            "Нужно упомянуть пользователя. Пример: `-report @user`",
            config.COLOR_ERROR,
        ))
        return

    remaining = check_cooldown(ctx.author)
    if remaining > 0:
        await ctx.send(embed=make_embed(
            "⏳ Подождите",
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
            "Канал для репортов не найден.",
            config.COLOR_ERROR,
        ))
        return

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
    report_embed.add_field(name="📍 Канал отправки", value=ctx.channel.mention, inline=False)

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
            value="Сообщений не найдено.",
            inline=False,
        )
    report_embed.set_footer(text=config.FOOTER_TEXT)
    await log_channel.send(embed=report_embed)
    await ctx.send(embed=make_embed(
        "✅ Репорт отправлен",
        f"Жалоба на {member.mention} зарегистрирована и передана администрации.",
        config.COLOR_SUCCESS,
    ))


# ============================================================
#  КОМАНДА: -license @user <1|2> <channel_id>
# ============================================================

@bot.command(name="license")
async def license_cmd(
    ctx: commands.Context,
    member: discord.Member = None,
    license_type: int = None,
    channel_id: int = None,
):
    """Выдать лицензию участнику и права на канал."""

    # --- Проверка канала вызова ---
    if not in_license_commands_channel(ctx):
        await ctx.send(embed=make_embed(
            "⛔ Неверный канал",
            f"Команда `{config.PREFIX}license` доступна только в "
            f"<#{config.LICENSE_COMMANDS_CHANNEL_ID}> "
            f"(кроме участников с правом Администратор).",
            config.COLOR_ERROR,
        ))
        return

    # --- Проверка прав исполнителя ---
    if not has_role(ctx.author, config.LICENSE_ALLOWED_ROLE_IDS):
        await ctx.send(embed=make_embed(
            "⛔ Недостаточно прав",
            "У вас нет доступа к команде `-license`.",
            config.COLOR_ERROR,
        ))
        return

    # --- Проверка аргументов ---
    if member is None or license_type is None or channel_id is None:
        await ctx.send(embed=make_embed(
            "❌ Неверный синтаксис",
            f"Использование: `{config.PREFIX}license @пользователь <1|2> <ID канала>`\n"
            f"Пример: `{config.PREFIX}license @user 1 1234567890123456789`",
            config.COLOR_ERROR,
        ))
        return

    if license_type not in (1, 2):
        await ctx.send(embed=make_embed(
            "❌ Неверный тип лицензии",
            "Тип лицензии должен быть **1** (Участник РП) или **2** (Менеджер РП).",
            config.COLOR_ERROR,
        ))
        return

    # --- Проверка канала ---
    target_channel = ctx.guild.get_channel(channel_id)
    if target_channel is None:
        await ctx.send(embed=make_embed(
            "❌ Канал не найден",
            f"Канал с ID `{channel_id}` не существует на этом сервере.",
            config.COLOR_ERROR,
        ))
        return

    if not isinstance(target_channel, discord.ForumChannel):
        await ctx.send(embed=make_embed(
            "❌ Неверный тип канала",
            "Указанный ID должен принадлежать каналу-форуму.",
            config.COLOR_ERROR,
        ))
        return

    # Канал должен находиться в разрешённой категории
    if (
        target_channel.category_id is None
        or target_channel.category_id != config.LICENSE_CATEGORY_ID
    ):
        await ctx.send(embed=make_embed(
            "⛔ Канал не в разрешённой категории",
            f"Канал {target_channel.mention} не входит в категорию, "
            f"доступную для лицензий.\n"
            f"Убедитесь, что канал находится в правильной категории.",
            config.COLOR_ERROR,
        ))
        return

    # --- Проверка: на канал ещё не выдана другая активная лицензия ---
    existing = license_db.get_active_license_by_channel(channel_id)
    if existing is not None:
        await ctx.send(embed=make_embed(
            "⛔ Каналу уже назначена лицензия",
            f"На канал {target_channel.mention} уже выдана активная лицензия "
            f"`{existing['license_id']}` ({license_type_label(existing['license_type'])}) "
            f"для <@{existing['discord_id']}>.\n\n"
            f"Один канал не может одновременно обслуживать больше одной "
            f"активной лицензии. Сначала отзовите текущую командой "
            f"`{config.PREFIX}cancel <@{existing['discord_id']}> {existing['license_type']}`, "
            f"либо укажите другой канал.",
            config.COLOR_ERROR,
        ))
        return

    # --- Выдача роли ---
    role_id = license_role_id(license_type)
    role = ctx.guild.get_role(role_id)
    if role is None:
        await ctx.send(embed=make_embed(
            "❌ Роль не найдена",
            f"Роль лицензии (ID `{role_id}`) не найдена на сервере.",
            config.COLOR_ERROR,
        ))
        return

    try:
        await member.add_roles(role, reason=f"Лицензия тип {license_type} от {ctx.author}")
    except discord.Forbidden:
        await ctx.send(embed=make_embed(
            "❌ Не удалось выдать роль",
            "У бота не хватает прав. Проверьте иерархию ролей.",
            config.COLOR_ERROR,
        ))
        return
    except discord.HTTPException as e:
        await ctx.send(embed=make_embed(
            "❌ Ошибка Discord",
            f"Не удалось выдать роль: `{e}`",
            config.COLOR_ERROR,
        ))
        return

    # --- Выдача прав на канал-форум ---
    # Тип 1: можно создавать темы и писать в них, но НЕ управлять чужими темами
    #        (переименовать/закрыть/удалить чужую тему нельзя; свою тему как
    #        автор можно переименовать — это стандартное поведение Discord
    #        для автора темы и не регулируется отдельным правом).
    # Тип 2: то же самое + manage_threads — управление любыми темами на форуме
    #        (аналог manage_messages для текстовых каналов).
    overwrite = discord.PermissionOverwrite(
        view_channel=True,
        read_message_history=True,
        create_public_threads=True,
        send_messages_in_threads=True,
        embed_links=True,
        attach_files=True,
        add_reactions=True,
        use_external_emojis=True,
        mention_everyone=False,
        manage_messages=(license_type == 2),
        manage_threads=(license_type == 2),
    )

    try:
        await target_channel.set_permissions(
            member,
            overwrite=overwrite,
            reason=f"Лицензия тип {license_type} от {ctx.author}",
        )
    except discord.Forbidden:
        await ctx.send(embed=make_embed(
            "❌ Не удалось выдать права на канал",
            "У бота не хватает прав `Управление каналом`.",
            config.COLOR_ERROR,
        ))
        return
    except discord.HTTPException as e:
        await ctx.send(embed=make_embed(
            "❌ Ошибка Discord",
            f"Не удалось выдать права на канал: `{e}`",
            config.COLOR_ERROR,
        ))
        return

    # --- Запись в БД ---
    try:
        lic_id = license_db.issue_license(
            license_type=license_type,
            discord_id=member.id,
            username=str(member),
            channel_id=channel_id,
            issued_by=ctx.author.id,
        )
    except license_db.ChannelAlreadyLicensedError as e:
        # Кто-то успел выдать лицензию на этот канал буквально в последний
        # момент (гонка между двумя одновременными вызовами команды) — база
        # данных отклонила запись. Откатываем уже выданные роль и права,
        # чтобы они не остались висеть без соответствующей записи в БД.
        existing = e.existing
        try:
            await member.remove_roles(role, reason="Откат: канал уже занят другой лицензией")
        except discord.HTTPException:
            pass
        try:
            await target_channel.set_permissions(
                member, overwrite=None,
                reason="Откат: канал уже занят другой лицензией",
            )
        except discord.HTTPException:
            pass
        await ctx.send(embed=make_embed(
            "⛔ Каналу уже назначена лицензия",
            f"На канал {target_channel.mention} уже выдана активная лицензия "
            f"`{existing['license_id']}` для <@{existing['discord_id']}>. "
            f"Попробуйте выдать лицензию ещё раз, указав другой канал.",
            config.COLOR_ERROR,
        ))
        return

    label = license_type_label(license_type)

    # --- Ответ в канале ---
    await ctx.send(embed=make_embed(
        f"✅ Лицензия {label} выдана",
        f"{member.mention} получил лицензию **{label}**.\n"
        f"Канал: {target_channel.mention}\n"
        f"ID лицензии: `{lic_id}`",
        config.COLOR_LICENSE,
    ))

    # --- Лог в канал выдачи лицензий ---
    log_channel = bot.get_channel(config.LICENSE_ISSUE_LOG_CHANNEL_ID)
    if log_channel:
        log_embed = discord.Embed(
            title=f"📜 Выдана лицензия — {label}",
            color=config.COLOR_LICENSE,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        log_embed.set_thumbnail(url=member.display_avatar.url)
        log_embed.add_field(
            name="👤 Получатель",
            value=f"{member.mention} (`{member}`)\nID: `{member.id}`",
            inline=False,
        )
        log_embed.add_field(
            name="🛠 Выдал",
            value=f"{ctx.author.mention} (`{ctx.author}`)\nID: `{ctx.author.id}`",
            inline=False,
        )
        log_embed.add_field(name="📋 Тип лицензии", value=f"Тип {license_type} — {label}", inline=True)
        log_embed.add_field(name="🆔 ID лицензии", value=f"`{lic_id}`", inline=True)
        log_embed.add_field(name="📍 Канал", value=target_channel.mention, inline=False)
        log_embed.set_footer(text=config.FOOTER_TEXT)
        await log_channel.send(embed=log_embed)


# ============================================================
#  КОМАНДА: -cancel @user <1|2>
# ============================================================

@bot.command(name="cancel")
async def cancel(
    ctx: commands.Context,
    member: discord.Member = None,
    license_type: int = None,
):
    """Отозвать лицензию (и снять роль + права на канал) у участника."""

    # --- Проверка канала вызова ---
    if not in_license_commands_channel(ctx):
        await ctx.send(embed=make_embed(
            "⛔ Неверный канал",
            f"Команда `{config.PREFIX}cancel` доступна только в "
            f"<#{config.LICENSE_COMMANDS_CHANNEL_ID}> "
            f"(кроме участников с правом Администратор).",
            config.COLOR_ERROR,
        ))
        return

    if member is None:
        await ctx.send(embed=make_embed(
            "❌ Не указан участник",
            f"Использование: `{config.PREFIX}cancel @пользователь <1|2>`\n"
            f"Пример: `{config.PREFIX}cancel @user 1`",
            config.COLOR_ERROR,
        ))
        return

    if license_type not in (1, 2):
        await ctx.send(embed=make_embed(
            "❌ Неверный тип лицензии",
            f"Укажите тип лицензии: **1** или **2**.\n"
            f"Использование: `{config.PREFIX}cancel @пользователь <1|2>`",
            config.COLOR_ERROR,
        ))
        return

    # --- Проверка прав ---
    if not has_role(ctx.author, config.CANCEL_ALLOWED_ROLE_IDS):
        await ctx.send(embed=make_embed(
            "⛔ Недостаточно прав",
            "У вас нет доступа к этой команде.",
            config.COLOR_ERROR,
        ))
        return

    # --- Кулдаун ---
    remaining = check_cooldown(ctx.author)
    if remaining > 0:
        await ctx.send(embed=make_embed(
            "⏳ Подождите",
            f"Попробуйте снова через **{remaining:.0f} сек.**",
            config.COLOR_ERROR,
        ))
        return

    label = license_type_label(license_type)
    role_id = license_role_id(license_type)

    # --- Отзыв из БД + получаем список каналов ---
    revoked_records = license_db.revoke_licenses(
        discord_id=member.id,
        license_type=license_type,
        revoked_by=ctx.author.id,
    )

    if not revoked_records:
        await ctx.send(embed=make_embed(
            "ℹ️ Нет активных лицензий",
            f"У {member.mention} нет активных лицензий типа **{license_type}** ({label}).",
            config.COLOR_INFO,
        ))
        return

    # --- Снимаем роль ---
    role = ctx.guild.get_role(role_id)
    role_removed = False
    if role and role in member.roles:
        try:
            await member.remove_roles(role, reason=f"Отзыв лицензии тип {license_type} от {ctx.author}")
            role_removed = True
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[ОШИБКА снятия роли при cancel] {e}")

    # --- Снимаем права со всех каналов из отозванных лицензий ---
    affected_channels = []
    for rec in revoked_records:
        ch = ctx.guild.get_channel(rec["channel_id"])
        if ch is not None:
            try:
                await ch.set_permissions(
                    member,
                    overwrite=None,  # None = удалить кастомный overwrite
                    reason=f"Отзыв лицензии тип {license_type} от {ctx.author}",
                )
                affected_channels.append(ch)
            except (discord.Forbidden, discord.HTTPException) as e:
                print(f"[ОШИБКА снятия прав канала при cancel] {e}")

    channels_text = ", ".join(ch.mention for ch in affected_channels) if affected_channels else "—"
    revoked_ids = ", ".join(f"`{r['license_id']}`" for r in revoked_records)

    # --- Ответ исполнителю ---
    await ctx.send(embed=make_embed(
        f"✅ Лицензия {label} отозвана",
        f"У {member.mention} отозваны все лицензии типа **{license_type}** ({label}).\n"
        f"ID лицензий: {revoked_ids}\n"
        f"Каналы: {channels_text}\n"
        f"Роль снята: {'да' if role_removed else 'нет (не было / ошибка)'}",
        config.COLOR_CANCEL,
    ))

    # --- Лог в канал ---
    log_channel = bot.get_channel(config.LICENSE_REVOKE_LOG_CHANNEL_ID)
    if log_channel:
        log_embed = discord.Embed(
            title=f"🚫 Отозвана лицензия — {label}",
            color=config.COLOR_REVOKE,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        log_embed.set_thumbnail(url=member.display_avatar.url)
        log_embed.add_field(
            name="🎯 Цель",
            value=f"{member.mention} (`{member}`)\nID: `{member.id}`",
            inline=False,
        )
        log_embed.add_field(
            name="🛠 Модератор",
            value=f"{ctx.author.mention} (`{ctx.author}`)\nID: `{ctx.author.id}`",
            inline=False,
        )
        log_embed.add_field(name="📋 Тип лицензии", value=f"Тип {license_type} — {label}", inline=True)
        log_embed.add_field(name="🆔 ID лицензий", value=revoked_ids, inline=False)
        log_embed.add_field(name="📍 Каналы", value=channels_text, inline=False)
        log_embed.add_field(name="🏷 Роль снята", value="Да" if role_removed else "Нет", inline=True)
        log_embed.add_field(name="📌 Канал выполнения", value=ctx.channel.mention, inline=True)
        log_embed.set_footer(text=config.FOOTER_TEXT)
        await log_channel.send(embed=log_embed)


# ============================================================
#  КОМАНДА: -data [запрос]
#  Поиск: по ID лицензии (RPP-/RPM-), по Discord ID (@user или числовой ID),
#          по никнейму (любая другая строка)
# ============================================================

@bot.command(name="data")
async def data_cmd(ctx: commands.Context, *, query: str = None):
    """Просмотр базы данных лицензий."""

    # --- Проверка канала вызова ---
    if not in_license_commands_channel(ctx):
        await ctx.send(embed=make_embed(
            "⛔ Неверный канал",
            f"Команда `{config.PREFIX}data` доступна только в "
            f"<#{config.LICENSE_COMMANDS_CHANNEL_ID}> "
            f"(кроме участников с правом Администратор).",
            config.COLOR_ERROR,
        ))
        return

    if not has_role(ctx.author, config.DATA_ALLOWED_ROLE_IDS):
        await ctx.send(embed=make_embed(
            "⛔ Недостаточно прав",
            "У вас нет доступа к базе данных лицензий.",
            config.COLOR_ERROR,
        ))
        return

    can_see_notes = has_role(ctx.author, config.DATA_ALLOWED_ROLE_IDS)

    if query is None:
        await ctx.send(embed=make_embed(
            "ℹ️ Использование команды -data",
            "Поиск по базе лицензий:\n"
            f"`{config.PREFIX}data RPP-0000001` — по ID лицензии\n"
            f"`{config.PREFIX}data @пользователь` — по упоминанию\n"
            f"`{config.PREFIX}data 123456789012345678` — по Discord ID\n"
            f"`{config.PREFIX}data никнейм` — по юзернейму",
            config.COLOR_DATA,
        ))
        return

    # Определяем тип запроса
    query = query.strip()
    records: list[dict] = []
    search_label = query

    # 1) ID лицензии (RPP-XXXXXXX или RPM-XXXXXXX)
    if re.match(r"^RP[PM]-\d{7}$", query.upper()):
        records = license_db.search_by_license_id(query.upper())
        search_label = f"ID лицензии `{query.upper()}`"

    # 2) Упоминание или числовой Discord ID
    elif re.match(r"^<@!?\d+>$", query) or query.isdigit():
        raw_id = re.sub(r"[<@!>]", "", query)
        if raw_id.isdigit():
            discord_id = int(raw_id)
            records = license_db.search_by_discord_id(discord_id)
            search_label = f"Discord ID `{discord_id}`"

    # 3) Никнейм
    else:
        records = license_db.search_by_username(query)
        search_label = f"никнейм `{query}`"

    if not records:
        await ctx.send(embed=make_embed(
            "🔍 Ничего не найдено",
            f"По запросу {search_label} лицензий не найдено.",
            config.COLOR_DATA,
        ))
        return

    # Группируем по пользователям для удобного вывода
    users_data: dict[int, list[dict]] = {}
    for rec in records:
        users_data.setdefault(rec["discord_id"], []).append(rec)

    for discord_id, user_records in users_data.items():
        first = user_records[0]

        embed = discord.Embed(
            title=f"📋 База данных лицензий — {first['username']}",
            color=config.COLOR_DATA,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(
            name="👤 Пользователь",
            value=f"`{first['username']}` | Discord ID: `{discord_id}`",
            inline=False,
        )

        def resolve_mod(user_id: int | None) -> str:
            """Discord ID модератора -> упоминание, иначе просто ID (например, если он покинул сервер)."""
            if not user_id:
                return "—"
            member = ctx.guild.get_member(user_id)
            return member.mention if member else f"`{user_id}` (не на сервере)"

        # Список лицензий
        lic_lines = []
        for rec in user_records:
            status = "🟢 Активна" if not rec["revoked"] else "🔴 Отозвана"
            ch = ctx.guild.get_channel(rec["channel_id"])
            ch_status = ch.mention if ch else f"~~удалён~~ (`{rec['channel_id']}`)"
            issuer = resolve_mod(rec.get("issued_by"))
            line = (
                f"**{rec['license_id']}** — Тип {rec['license_type']} "
                f"({license_type_label(rec['license_type'])})\n"
                f"  Статус: {status}\n"
                f"  Канал: {ch_status}\n"
                f"  Выдана: {fmt_dt(rec['issued_at'])} — выдал {issuer}"
            )
            if rec["revoked"]:
                revoker = resolve_mod(rec.get("revoked_by"))
                line += f"\n  Отозвана: {fmt_dt(rec['revoked_at'])} — снял {revoker}"
            lic_lines.append(line)

        licenses_text = "\n\n".join(lic_lines)
        if len(licenses_text) > 1024:
            licenses_text = licenses_text[:1000] + "\n…"

        embed.add_field(name="📜 Лицензии", value=licenses_text or "—", inline=False)

        # Заметка (только для привилегированных ролей)
        if can_see_notes:
            note = license_db.get_note(discord_id)
            embed.add_field(
                name="📝 Заметка (видна только персоналу)",
                value=note if note else "*Заметок нет*",
                inline=False,
            )

        embed.set_footer(text=config.FOOTER_TEXT)
        await ctx.send(embed=embed)


# ============================================================
#  КОМАНДА: -note @user <текст>
#  Добавить/изменить заметку к пользователю в БД
# ============================================================

@bot.command(name="note")
async def note_cmd(ctx: commands.Context, member: discord.Member = None, *, text: str = None):
    """Добавить или изменить заметку к пользователю."""

    if not has_role(ctx.author, config.DATA_ALLOWED_ROLE_IDS):
        await ctx.send(embed=make_embed(
            "⛔ Недостаточно прав",
            "У вас нет доступа к этой команде.",
            config.COLOR_ERROR,
        ))
        return

    if member is None or text is None:
        await ctx.send(embed=make_embed(
            "❌ Неверный синтаксис",
            f"Использование: `{config.PREFIX}note @пользователь текст заметки`",
            config.COLOR_ERROR,
        ))
        return

    license_db.set_note(
        discord_id=member.id,
        note_text=text,
        updated_by=ctx.author.id,
    )

    await ctx.send(embed=make_embed(
        "✅ Заметка сохранена",
        f"Заметка для {member.mention} обновлена.\n"
        f"**Текст:** {text}",
        config.COLOR_SUCCESS,
    ))


# ============================================================
#  КОМАНДА: -wipe @user confirm
#  Безвозвратно удаляет всю историю лицензий пользователя из БД.
# ============================================================

@bot.command(name="wipe")
async def wipe_cmd(
    ctx: commands.Context,
    member: discord.Member = None,
    confirm: str = None,
):
    """Полностью и безвозвратно удаляет историю лицензий пользователя из базы."""

    if not ctx.author.guild_permissions.administrator:
        await ctx.send(embed=make_embed(
            "⛔ Недостаточно прав",
            f"Команда `{config.PREFIX}wipe` доступна только участникам "
            f"с правом **Администратор** на сервере.",
            config.COLOR_ERROR,
        ))
        return

    if member is None:
        await ctx.send(embed=make_embed(
            "❌ Неверный синтаксис",
            f"Использование: `{config.PREFIX}wipe @пользователь confirm`\n"
            f"Сначала вызовите команду без `confirm` — бот покажет, что будет удалено.",
            config.COLOR_ERROR,
        ))
        return

    history = license_db.search_by_discord_id(member.id)
    if not history:
        await ctx.send(embed=make_embed(
            "ℹ️ История пуста",
            f"У {member.mention} нет ни одной записи в истории лицензий — удалять нечего.",
            config.COLOR_INFO,
        ))
        return

    active = [r for r in history if not r["revoked"]]
    revoked = [r for r in history if r["revoked"]]

    # --- Без "confirm" на конце — только показываем предупреждение ---
    if confirm is None or confirm.lower() != "confirm":
        warning = (
            f"У {member.mention} найдено **{len(history)}** записей в истории лицензий:\n"
            f"• Активных: **{len(active)}**\n"
            f"• Отозванных: **{len(revoked)}**\n\n"
        )
        if active:
            warning += (
                "⚠️ Среди них есть **активные** лицензии. Очистка истории "
                "**не снимает** роль и права на канале — если нужно лишить "
                f"доступа, сначала отзовите их через `{config.PREFIX}cancel`.\n\n"
            )
        warning += (
            "Это действие **необратимо** и полностью сотрёт записи из базы "
            "(в отличие от `-cancel`, который только помечает их отозванными).\n\n"
            f"Чтобы подтвердить, повторите команду с `confirm` на конце:\n"
            f"```\n{config.PREFIX}wipe {member.id} confirm\n```"
        )
        await ctx.send(embed=make_embed(
            "⚠️ Подтвердите очистку истории",
            warning,
            config.COLOR_REVOKE,
        ))
        return

    # --- Подтверждено — удаляем ---
    deleted = license_db.clear_history(member.id)

    await ctx.send(embed=make_embed(
        "🧾 История лицензий очищена",
        f"Пользователь: {member.mention} (`{member.id}`)\n"
        f"Удалено записей: **{deleted}**",
        config.COLOR_SUCCESS,
    ))

    log_channel = bot.get_channel(config.CANCEL_LOG_CHANNEL_ID)
    if log_channel:
        log_embed = discord.Embed(
            title="🧾 Очищена история лицензий",
            color=config.COLOR_REVOKE,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        log_embed.set_thumbnail(url=member.display_avatar.url)
        log_embed.add_field(
            name="👤 Пользователь",
            value=f"{member.mention} (`{member}`)\nID: `{member.id}`",
            inline=False,
        )
        log_embed.add_field(
            name="🛠 Выполнил",
            value=f"{ctx.author.mention} (`{ctx.author}`)\nID: `{ctx.author.id}`",
            inline=False,
        )
        log_embed.add_field(name="🗑 Удалено записей", value=str(deleted), inline=True)
        log_embed.set_footer(text=config.FOOTER_TEXT)
        await log_channel.send(embed=log_embed)


# ============================================================
#  ОБЩАЯ ПРОВЕРКА ПРАВ ДЛЯ -del / -delus
# ============================================================
#
# Правила (подробные комментарии — в config.py, раздел
# "-del / -delus — доступ по ролям и каналам"):
#
#   1. Роль из DELETE_GLOBAL_ROLE_IDS    → можно в любом канале сервера.
#   2. Роль из DELETE_EXEMPT_ROLE_IDS    → можно в каналах категории
#      DELETE_CATEGORY_ID, но НЕЛЬЗЯ в каналах категории
#      DELETE_EXEMPT_CATEGORY_ID.
#   3. Роль из DELETE_NO_EXEMPT_ROLE_IDS → можно и в DELETE_CATEGORY_ID,
#      и в DELETE_EXEMPT_CATEGORY_ID.
#   4. Ни одной подходящей роли → доступа нет.
#
# DELETE_CATEGORY_ID и DELETE_EXEMPT_CATEGORY_ID — это две РАЗНЫЕ,
# независимые категории каналов (Discord не поддерживает вложенные
# категории), поэтому канал может относиться ровно к одной из них.

def has_delete_access(member: discord.Member, channel: discord.abc.GuildChannel) -> bool:
    member_role_ids = {r.id for r in member.roles}

    # 1) Супер-роли — можно везде, категории не проверяем
    if member_role_ids & config.DELETE_GLOBAL_ROLE_IDS:
        return True

    category_id = getattr(channel, "category_id", None)
    in_main_category = category_id == config.DELETE_CATEGORY_ID
    in_exempt_category = category_id == config.DELETE_EXEMPT_CATEGORY_ID

    # Вне обеих категорий доступа нет ни у кого, кроме супер-ролей выше
    if not (in_main_category or in_exempt_category):
        return False

    # 2) Роли с исключением — запрещено именно в категории-исключении
    if member_role_ids & config.DELETE_EXEMPT_ROLE_IDS:
        return not in_exempt_category

    # 3) Роли без исключения — можно в обеих категориях
    if member_role_ids & config.DELETE_NO_EXEMPT_ROLE_IDS:
        return True

    return False


# ============================================================
#  КОМАНДА: -del
# ============================================================

@bot.command(name="del")
async def del_messages(ctx: commands.Context, amount: int = None):
    """Удалить указанное количество последних сообщений в текущем канале."""

    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass

    if not has_delete_access(ctx.author, ctx.channel):
        await ctx.send(embed=make_embed(
            "⛔ Недостаточно прав",
            "У вас нет доступа к этой команде в данном канале.",
            config.COLOR_ERROR,
        ), delete_after=8)
        return

    if amount is None:
        await ctx.send(embed=make_embed(
            "❌ Не указано количество",
            f"Пример: `{config.PREFIX}del 100`",
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

    remaining = check_del_cooldown(ctx.author.id, config.DEL_COOLDOWN_SECONDS)
    if remaining > 0:
        await ctx.send(embed=make_embed(
            "⏳ Подождите",
            f"Попробуйте снова через **{remaining:.0f} сек.**",
            config.COLOR_ERROR,
        ), delete_after=8)
        return

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
            f"Не удалось удалить сообщения: `{e}`",
            config.COLOR_ERROR,
        ), delete_after=8)
        return

    await ctx.send(embed=make_embed(
        "✅ Сообщения удалены",
        f"Удалено **{len(deleted)}** сообщений в {ctx.channel.mention}.",
        config.COLOR_SUCCESS,
    ), delete_after=8)

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
        log_embed.add_field(name="📍 Канал", value=ctx.channel.mention, inline=False)
        log_embed.add_field(name="🔢 Запрошено / Удалено", value=f"{amount} / {len(deleted)}", inline=False)
        log_embed.set_footer(text=config.FOOTER_TEXT)
        await log_channel.send(embed=log_embed)


# ============================================================
#  КОМАНДА: -delus
# ============================================================

@bot.command(name="delus")
async def delus_messages(ctx: commands.Context, member: discord.Member = None, amount: int = None):
    """Удалить последние N сообщений указанного пользователя по всем каналам сервера."""

    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass

    if not has_delete_access(ctx.author, ctx.channel):
        await ctx.send(embed=make_embed(
            "⛔ Недостаточно прав",
            "У вас нет доступа к этой команде в данном канале.",
            config.COLOR_ERROR,
        ), delete_after=8)
        return

    if member is None or amount is None:
        await ctx.send(embed=make_embed(
            "❌ Неверный синтаксис",
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

    remaining = check_delus_cooldown(ctx.author.id, config.DELUS_COOLDOWN_SECONDS)
    if remaining > 0:
        await ctx.send(embed=make_embed(
            "⏳ Подождите",
            f"Попробуйте снова через **{remaining:.0f} сек.**",
            config.COLOR_ERROR,
        ), delete_after=8)
        return

    status_msg = await ctx.send(embed=make_embed(
        "⏳ Идёт удаление",
        f"Ищу и удаляю до **{amount}** последних сообщений {member.mention} по всем каналам...",
        config.COLOR_INFO,
    ))

    target_messages = []
    per_channel_breakdown = {}

    for channel in ctx.guild.text_channels:
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.read_message_history and perms.manage_messages):
            continue
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

    target_messages.sort(key=lambda m: m.created_at, reverse=True)
    target_messages = target_messages[:amount]

    if not target_messages:
        await status_msg.edit(embed=make_embed(
            "ℹ️ Сообщения не найдены",
            f"Не найдено ни одного сообщения от {member.mention} в доступных каналах.",
            config.COLOR_INFO,
        ))
        return

    messages_by_channel: dict[discord.TextChannel, list[discord.Message]] = {}
    for msg in target_messages:
        messages_by_channel.setdefault(msg.channel, []).append(msg)

    deleted_total = 0
    deleted_per_channel = {}

    for channel, msgs in messages_by_channel.items():
        try:
            await channel.delete_messages(msgs)
            deleted_total += len(msgs)
            deleted_per_channel[channel] = len(msgs)
        except discord.HTTPException:
            ok_count = 0
            for m in msgs:
                try:
                    await m.delete()
                    ok_count += 1
                except discord.HTTPException:
                    continue
            deleted_total += ok_count
            deleted_per_channel[channel] = ok_count

    await status_msg.edit(embed=make_embed(
        "✅ Сообщения удалены",
        f"Удалено **{deleted_total}** сообщений пользователя {member.mention} "
        f"в **{len(deleted_per_channel)}** канал(ах).",
        config.COLOR_SUCCESS,
    ))

    log_channel = bot.get_channel(config.DELETE_LOG_CHANNEL_ID)
    if log_channel:
        breakdown_lines = [f"{ch.mention} — {count}" for ch, count in deleted_per_channel.items()]
        breakdown_text = "\n".join(breakdown_lines) if breakdown_lines else "—"
        if len(breakdown_text) > 1024:
            breakdown_text = breakdown_text[:1000] + "\n…"

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
        log_embed.add_field(name="🔢 Запрошено / Удалено", value=f"{amount} / {deleted_total}", inline=False)
        log_embed.add_field(name="📍 Каналы", value=breakdown_text, inline=False)
        log_embed.set_footer(text=config.FOOTER_TEXT)
        await log_channel.send(embed=log_embed)


# ============================================================
#  КОМАНДА: -bot
#  Информация о боте: разработчик, версия, дата последнего обновления.
# ============================================================

BOT_VERSION = "2.0.1 beta"
BOT_LAST_UPDATE = "02.07.2026"
BOT_DEVELOPER_ID = 1148265088277545081


@bot.command(name="bot")
async def bot_info_cmd(ctx: commands.Context):
    """Показывает информацию о боте: разработчик, версия, дата обновления."""
    embed = discord.Embed(
        title="🤖 О боте",
        color=config.COLOR_INFO,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(name="👨‍💻 Разработчик", value=f"<@{BOT_DEVELOPER_ID}>", inline=False)
    embed.add_field(name="🏷 Версия", value=f"`{BOT_VERSION}`", inline=True)
    embed.add_field(name="🗓 Последнее обновление", value=BOT_LAST_UPDATE, inline=True)
    embed.set_footer(text=config.FOOTER_TEXT)
    await ctx.send(embed=embed)


# ============================================================
#  КОМАНДА: -help
#  Показывает подробное описание всех команд бота.
#  Каждый пользователь видит только те команды, к которым у него есть доступ.
# ============================================================

# Структура: каждая запись — один «блок» помощи.
# required_roles=None    → команда доступна всем участникам сервера.
# required_roles=<set>   → блок показывается только если у вызывающего есть
#                          хотя бы одна из перечисленных ролей.
# required_roles="ADMIN" → блок показывается только участникам с правом
#                          Администратор на сервере (не зависит от ролей).

_HELP_BLOCKS = [
    # ── -report ──────────────────────────────────────────────────────────────
    {
        "required_roles": None,  # доступна всем
        "emoji": "🚩",
        "name": "-report @пользователь",
        "description": (
            "Отправляет жалобу на участника сервера в канал администрации.\n"
            "Бот автоматически собирает последние сообщения нарушителя по всему серверу "
            "и прикладывает их к репорту вместе с данными профиля.\n\n"
            "**Кулдаун:** 60 сек (7 сек для привилегированных ролей).\n\n"
            "**Пример:**\n"
            "```\n-report @username\n```"
        ),
    },
    # ── -license ─────────────────────────────────────────────────────────────
    {
        "required_roles": config.LICENSE_ALLOWED_ROLE_IDS,
        "emoji": "📜",
        "name": "-license @пользователь <тип> <ID_канала>",
        "description": (
            "Выдаёт лицензию участнику РП и предоставляет права на указанный канал-форум.\n\n"
            "**Типы лицензий:**\n"
            "• `1` — Лицензия **Участника РП** (RPP-XXXXXXX)\n"
            "  Роль: <@&1520321736631914679>\n"
            "  Права на форуме: создавать темы и писать в темах, "
            "*без* права управлять чужими темами.\n\n"
            "• `2` — Лицензия **Менеджера РП** (RPM-XXXXXXX)\n"
            "  Роль: <@&1520321736631914678>\n"
            "  Права на форуме: создавать темы и писать в темах, "
            "*а также* управлять любыми темами на канале.\n\n"
            "**Ограничения:**\n"
            "• Канал должен быть **форумом** и находиться в специальной категории лицензий.\n"
            "  Если указать текстовый канал или канал вне этой категории — команда будет отклонена.\n"
            "• Одному участнику можно выдать несколько лицензий одного типа на разные каналы.\n"
            "• На **один канал** одновременно может действовать только **одна** активная "
            "лицензия — выдать вторую на тот же канал, пока первая не отозвана, нельзя.\n"
            f"• Саму команду можно вызвать только в <#{config.LICENSE_COMMANDS_CHANNEL_ID}> "
            "(участникам с правом Администратор — можно из любого канала).\n\n"
            "**Примеры:**\n"
            "```\n"
            "-license @username 1 1234567890123456789\n"
            "-license @username 2 9876543210987654321\n"
            "```"
        ),
    },
    # ── -cancel ──────────────────────────────────────────────────────────────
    {
        "required_roles": config.CANCEL_ALLOWED_ROLE_IDS,
        "emoji": "🚫",
        "name": "-cancel @пользователь <тип>",
        "description": (
            "Отзывает **все** активные лицензии указанного типа у участника:\n"
            "• снимает соответствующую роль;\n"
            "• удаляет выданные боту overwrite-права со **всех** каналов, "
            "на которые действовали лицензии этого типа;\n"
            "• помечает лицензии в базе данных как отозванные.\n\n"
            "**Типы:**\n"
            "• `1` — отозвать лицензию Участника РП\n"
            "• `2` — отозвать лицензию Менеджера РП\n\n"
            "Если у участника одновременно активны обе лицензии, "
            "каждая отзывается отдельной командой.\n\n"
            f"Команду можно вызвать только в <#{config.LICENSE_COMMANDS_CHANNEL_ID}> "
            "(участникам с правом Администратор — можно из любого канала).\n\n"
            "**Примеры:**\n"
            "```\n"
            "-cancel @username 1\n"
            "-cancel @username 2\n"
            "```"
        ),
    },
    # ── -data ────────────────────────────────────────────────────────────────
    {
        "required_roles": config.DATA_ALLOWED_ROLE_IDS,
        "emoji": "🗃",
        "name": "-data [запрос]",
        "description": (
            "Открывает базу данных лицензий и выводит информацию по найденным участникам.\n\n"
            "**Варианты поиска:**\n"
            "• По ID лицензии — `RPP-0000001` или `RPM-0000003`\n"
            "• По упоминанию — `@пользователь`\n"
            "• По Discord ID — числовой ID профиля\n"
            "• По никнейму — частичное совпадение, регистр не важен\n\n"
            "**Что отображается:**\n"
            "— Все лицензии участника (активные 🟢 и отозванные 🔴)\n"
            "— Тип, ID лицензии, дата выдачи и отзыва\n"
            "— Статус канала (существует или удалён)\n"
            "— Заметка персонала (видна только привилегированным ролям)\n\n"
            f"Команду можно вызвать только в <#{config.LICENSE_COMMANDS_CHANNEL_ID}> "
            "(участникам с правом Администратор — можно из любого канала).\n\n"
            "**Примеры:**\n"
            "```\n"
            "-data RPP-0000001\n"
            "-data @username\n"
            "-data 123456789012345678\n"
            "-data username\n"
            "```"
        ),
    },
    # ── -note ────────────────────────────────────────────────────────────────
    {
        "required_roles": config.DATA_ALLOWED_ROLE_IDS,
        "emoji": "📝",
        "name": "-note @пользователь <текст>",
        "description": (
            "Добавляет или перезаписывает заметку к участнику в базе данных лицензий.\n"
            "Заметка видна только ролям с доступом к `-data` и не отображается "
            "обычным участникам.\n\n"
            "Если заметка уже существует — она будет **заменена** новым текстом.\n\n"
            "**Пример:**\n"
            "```\n"
            "-note @username Подозрительное поведение, следить за активностью.\n"
            "```"
        ),
    },
    # ── -wipe ────────────────────────────────────────────────────────
    {
        "required_roles": "ADMIN",
        "emoji": "🧾",
        "name": "-wipe @пользователь confirm",
        "description": (
            "Полностью и **безвозвратно** удаляет всю историю лицензий "
            "пользователя из базы данных (и активные, и уже отозванные записи).\n\n"
            "**Доступна только участникам с правом Администратор** на сервере "
            "(не зависит от конкретных ролей).\n\n"
            "В отличие от `-cancel`, который только помечает лицензии отозванными "
            "и хранит их в истории, эта команда стирает записи насовсем — "
            "используйте её только для очистки ошибочных/тестовых записей.\n\n"
            "**Как работает:**\n"
            "• Вызов **без** `confirm` — покажет, сколько записей найдено "
            "(активных/отозванных), но ничего не удалит.\n"
            "• Вызов **с** `confirm` в конце — подтверждает и выполняет удаление.\n\n"
            "⚠️ Команда не снимает роль и права на канале — если у пользователя "
            "есть активные лицензии, сначала отзовите их через `-cancel`.\n\n"
            "**Примеры:**\n"
            "```\n"
            "-wipe @username\n"
            "-wipe @username confirm\n"
            "```"
        ),
    },
    # ── -del ─────────────────────────────────────────────────────────────────
    {
        "required_roles": (
            config.DELETE_GLOBAL_ROLE_IDS
            | config.DELETE_EXEMPT_ROLE_IDS
            | config.DELETE_NO_EXEMPT_ROLE_IDS
        ),
        "emoji": "🗑",
        "name": "-del <количество>",
        "description": (
            "Удаляет последние N сообщений в **текущем канале**, где вызвана команда.\n"
            "Само сообщение с командой тоже удаляется автоматически.\n\n"
            f"**Лимит:** до **{config.DEL_MAX_MESSAGES}** сообщений за один вызов.\n"
            f"**Кулдаун:** {config.DEL_COOLDOWN_SECONDS} сек.\n\n"
            "**Где можно использовать:**\n"
            "• Роли-«супер-модераторы» — в **любом** канале сервера.\n"
            f"• Остальные роли с доступом — только в каналах категории "
            f"<#{config.DELETE_CATEGORY_ID}>.\n"
            f"  Часть из них дополнительно **не может** использовать команду "
            f"в каналах подкатегории <#{config.DELETE_EXEMPT_CATEGORY_ID}>.\n\n"
            "**Пример:**\n"
            "```\n"
            "-del 50\n"
            "```"
        ),
    },
    # ── -delus ───────────────────────────────────────────────────────────────
    {
        "required_roles": (
            config.DELETE_GLOBAL_ROLE_IDS
            | config.DELETE_EXEMPT_ROLE_IDS
            | config.DELETE_NO_EXEMPT_ROLE_IDS
        ),
        "emoji": "🧹",
        "name": "-delus @пользователь <количество>",
        "description": (
            "Удаляет последние N сообщений указанного пользователя "
            "**по всем каналам сервера**, к которым у бота есть доступ.\n"
            "Само сообщение с командой удаляется автоматически.\n\n"
            f"**Лимит:** до **{config.DELUS_MAX_MESSAGES}** сообщений за один вызов.\n"
            f"**Кулдаун:** {config.DELUS_COOLDOWN_SECONDS} сек.\n\n"
            "Операция может занять некоторое время — бот уведомит о прогрессе.\n\n"
            "**Где можно вызвать команду** (проверяется канал вызова, "
            "а не каналы, где реально удаляются сообщения):\n"
            "• Роли-«супер-модераторы» — из **любого** канала сервера.\n"
            f"• Остальные роли с доступом — только из каналов категории "
            f"<#{config.DELETE_CATEGORY_ID}>.\n"
            f"  Часть из них дополнительно **не может** вызывать команду "
            f"из каналов подкатегории <#{config.DELETE_EXEMPT_CATEGORY_ID}>.\n\n"
            "**Пример:**\n"
            "```\n"
            "-delus @username 100\n"
            "```"
        ),
    },
    # ── -admin ───────────────────────────────────────────────────────────────
    {
        "required_roles": config.ADMIN_PANEL_ALLOWED_ROLE_IDS,
        "emoji": "🛠",
        "name": "-admin",
        "description": (
            "🧪 **BETA-тестирование.**\n\n"
            "Открывает кнопочную админ-панель: база данных, выдача и отзыв "
            "лицензий, заметки, безвозвратная очистка истории, удаление "
            "сообщений — всё в одном интерфейсе, без ввода текстовых команд.\n\n"
            "Ввод данных (пользователь, канал, тип лицензии и т.д.) — через "
            "всплывающие окошки Discord. Управлять панелью может только тот, "
            "кто её вызвал. Панель автоматически отключается через "
            f"**{config.ADMIN_PANEL_TIMEOUT_SECONDS // 60} минут**.\n\n"
            "**Пример:**\n"
            "```\n"
            "-admin\n"
            "```"
        ),
    },
    # ── -bot ─────────────────────────────────────────────────────────────────
    {
        "required_roles": None,  # доступна всем
        "emoji": "🤖",
        "name": "-bot",
        "description": (
            "Показывает информацию о боте: разработчика, текущую версию "
            "и дату последнего обновления.\n\n"
            "**Пример:**\n"
            "```\n"
            "-bot\n"
            "```"
        ),
    },
]


def _help_block_visible(block: dict, ctx: commands.Context) -> bool:
    required = block["required_roles"]
    if required is None:
        return True
    if required == "ADMIN":
        return ctx.author.guild_permissions.administrator
    author_role_ids = {role.id for role in ctx.author.roles}
    return bool(author_role_ids & required)


@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    """Показывает список доступных команд с подробным описанием."""

    # Фильтруем блоки: показываем только те, к которым у пользователя есть доступ
    visible_blocks = [block for block in _HELP_BLOCKS if _help_block_visible(block, ctx)]

    if not visible_blocks:
        await ctx.send(embed=make_embed(
            "ℹ️ Справка",
            "У вас нет доступа ни к одной команде бота.",
            config.COLOR_INFO,
        ))
        return

    # Создаём заголовочный embed
    header_embed = discord.Embed(
        title="📖 Neway RP — Справка по командам",
        description=(
            f"Все команды начинаются с префикса `{config.PREFIX}`.\n"
            "Ниже перечислены **только те команды**, к которым у вас есть доступ.\n\n"
            f"**Доступно команд:** {len(visible_blocks)}"
        ),
        color=config.COLOR_INFO,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    header_embed.set_footer(text=config.FOOTER_TEXT)
    await ctx.send(embed=header_embed)

    # Отправляем отдельный embed на каждую команду
    for block in visible_blocks:
        embed = discord.Embed(
            title=f"{block['emoji']}  `{config.PREFIX}{block['name']}`",
            description=block["description"],
            color=config.COLOR_INFO,
        )
        embed.set_footer(text=config.FOOTER_TEXT)
        await ctx.send(embed=embed)


# ============================================================
#  КОМАНДА: -admin — кнопочная админ-панель (BETA)
# ============================================================

@bot.command(name="admin")
async def admin_panel_cmd(ctx: commands.Context):
    """Открыть кнопочную админ-панель. Функция в BETA-тестировании."""

    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass

    if not admin_panel.has_admin_panel_access(ctx.author):
        await ctx.send(embed=make_embed(
            "⛔ Недостаточно прав",
            "У вас нет доступа к админ-панели.",
            config.COLOR_ERROR,
        ), delete_after=8)
        return

    await admin_panel.open_admin_panel(bot, ctx)


# ============================================================
#  ЗАПУСК
# ============================================================

if __name__ == "__main__":
    if not config.BOT_TOKEN:
        raise SystemExit(
            "Не найден токен бота! Установите переменную окружения BOT_TOKEN."
        )
    bot.run(config.BOT_TOKEN)
