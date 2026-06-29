# -*- coding: utf-8 -*-
"""
Neway RP — Discord-бот модерации (с системой лицензий).
"""
import datetime
import discord
from discord.ext import commands
import config
import database
from cooldown import check_cooldown, check_del_cooldown, check_delus_cooldown

# Инициализация БД при старте
database.init_db()

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix=config.PREFIX, intents=intents, help_command=None)

# ============================================================
# СЛУЖЕБНЫЕ ФУНКЦИИ
# ============================================================
def make_embed(title: str, description: str, color: int) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=config.FOOTER_TEXT)
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed

def has_access(member: discord.Member, allowed_roles: set) -> bool:
    member_role_ids = {role.id for role in member.roles}
    return bool(member_role_ids & allowed_roles)

def get_text_permissions(license_type: int) -> discord.Permissions:
    """Возвращает права для канала в зависимости от типа лицензии."""
    perms = discord.Permissions(
        read_messages=True, read_message_history=True, send_messages=True,
        embed_links=True, attach_files=True, add_reactions=True,
        use_external_emojis=True, use_external_stickers=True,
        send_messages_in_threads=True, create_public_threads=True,
        create_private_threads=True, send_tts_messages=True
    )
    if license_type == 2:
        perms.manage_messages = True
        perms.manage_threads = True
    return perms

# ============================================================
# СОБЫТИЯ
# ============================================================
@bot.event
async def on_ready():
    print(f"[OK] Бот запущен как {bot.user} (ID: {bot.user.id})")
    print(f"[OK] База данных лицензий инициализирована.")

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CheckFailure):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=make_embed("❌ Не указан аргумент", str(error), config.COLOR_ERROR))
        return
    if isinstance(error, (commands.MemberNotFound, commands.BadArgument)):
        await ctx.send(embed=make_embed("❌ Ошибка аргумента", "Не удалось найти пользователя или канал.", config.COLOR_ERROR))
        return
    if isinstance(error, commands.CommandNotFound):
        return
    print(f"[ОШИБКА] {error}")

# ============================================================
# КОМАНДА: -license
# ============================================================
@bot.command(name="license")
@commands.check(lambda ctx: has_access(ctx.author, config.LICENSE_ALLOWED_ROLE_IDS))
async def license_cmd(ctx: commands.Context, member: discord.Member, license_type: int, channel: discord.TextChannel):
    """Выдать лицензию пользователю. Пример: -license @user 1 #канал"""
    
    if license_type not in (1, 2):
        return await ctx.send(embed=make_embed("❌ Ошибка", "Тип лицензии должен быть 1 или 2.", config.COLOR_ERROR))

    if channel.category_id != config.LICENSE_CATEGORY_ID:
        return await ctx.send(embed=make_embed(
            "⛔ Отказано в доступе", 
            f"Канал {channel.mention} не находится в нужной категории. Выдача лицензий разрешена только в каналах категории `{config.LICENSE_CATEGORY_ID}`.", 
            config.COLOR_ERROR
        ))

    role_id = config.ROLE_LICENSE_1 if license_type == 1 else config.ROLE_LICENSE_2
    role = ctx.guild.get_role(role_id)
    if not role:
        return await ctx.send(embed=make_embed("❌ Ошибка", "Роль лицензии не найдена на сервере.", config.COLOR_ERROR))

    # 1. Выдаем роль
    try:
        await member.add_roles(role, reason=f"Выдача лицензии типа {license_type} от {ctx.author}")
    except discord.Forbidden:
        return await ctx.send(embed=make_embed("❌ Ошибка", "Не хватает прав для выдачи роли.", config.COLOR_ERROR))

    # 2. Настраиваем права в канале
    perms = get_text_permissions(license_type)
    try:
        await channel.set_permissions(member, overwrite=discord.PermissionOverwrite.from_pair(perms, discord.Permissions.none()), reason="Выдача лицензии")
    except discord.Forbidden:
        return await ctx.send(embed=make_embed("❌ Ошибка", "Не хватает прав для настройки канала.", config.COLOR_ERROR))

    # 3. Сохраняем в БД
    license_id = database.add_license(member.id, license_type, channel.id)

    # 4. Ответ и лог
    type_name = "участника РП" if license_type == 1 else "модератора РП"
    await ctx.send(embed=make_embed("✅ Лицензия выдана", f"Пользователю {member.mention} выдана лицензия **{type_name}** (ID: `{license_id}`) в канале {channel.mention}.", config.COLOR_SUCCESS))

    log_channel = bot.get_channel(config.LICENSE_LOG_CHANNEL_ID)
    if log_channel:
        log_embed = discord.Embed(title="📜 Выдача лицензии", color=config.COLOR_SUCCESS, timestamp=datetime.datetime.now(datetime.timezone.utc))
        log_embed.add_field(name="Модератор", value=f"{ctx.author.mention}", inline=True)
        log_embed.add_field(name="Получатель", value=f"{member.mention} (`{member.id}`)", inline=True)
        log_embed.add_field(name="Тип / ID", value=f"Тип {license_type} | `{license_id}`", inline=False)
        log_embed.add_field(name="Канал", value=f"{channel.mention}", inline=True)
        log_embed.set_footer(text=config.FOOTER_TEXT)
        await log_channel.send(embed=log_embed)

# ============================================================
# КОМАНДА: -cancel (ПЕРЕПИСАНА ПОД ЛИЦЕНЗИИ)
# ============================================================
@bot.command(name="cancel")
@commands.check(lambda ctx: has_access(ctx.author, config.CANCEL_ALLOWED_ROLE_IDS))
async def cancel(ctx: commands.Context, member: discord.Member, license_type: int):
    """Отозвать лицензии указанного типа. Пример: -cancel @user 1"""
    if license_type not in (1, 2):
        return await ctx.send(embed=make_embed("❌ Ошибка", "Тип лицензии должен быть 1 или 2.", config.COLOR_ERROR))

    # Ищем все активные лицензии этого типа у юзера
    active_licenses = database.get_user_licenses(member.id, license_type=license_type, status='active')
    
    if not active_licenses:
        return await ctx.send(embed=make_embed("ℹ️ Не найдено", f"У {member.mention} нет активных лицензий типа {license_type}.", config.COLOR_INFO))

    # 1. Отзываем в БД
    database.revoke_licenses(member.id, license_type)

    # 2. Снимаем права в каналах
    channels_fixed = 0
    for lic in active_licenses:
        channel = bot.get_channel(lic[3]) # channel_id is index 3
        if channel:
            try:
                await channel.set_permissions(member, overwrite=None, reason=f"Отзыв лицензии типа {license_type}")
                channels_fixed += 1
            except discord.HTTPException:
                pass

    # 3. Снимаем роль (только если не осталось других активных лицензий ЭТОГО ТИПА)
    role_id = config.ROLE_LICENSE_1 if license_type == 1 else config.ROLE_LICENSE_2
    role = ctx.guild.get_role(role_id)
    if role and role in member.roles:
        # Проверяем, не осталось ли лицензий (мы только что их отозвали, но вдруг была рассинхронизация)
        remaining = database.get_user_licenses(member.id, license_type=license_type, status='active')
        if not remaining:
            try:
                await member.remove_roles(role, reason=f"Отзыв всех лицензий типа {license_type}")
            except discord.Forbidden:
                pass

    # 4. Ответ и лог
    type_name = "участника РП" if license_type == 1 else "модератора РП"
    await ctx.send(embed=make_embed("✅ Лицензии отозваны", f"У {member.mention} отозваны все лицензии **{type_name}** ({len(active_licenses)} шт.). Права в {channels_fixed} каналах сняты.", config.COLOR_SUCCESS))

    log_channel = bot.get_channel(config.CANCEL_LOG_CHANNEL_ID)
    if log_channel:
        log_embed = discord.Embed(title="🚫 Отзыв лицензий", color=config.COLOR_CANCEL, timestamp=datetime.datetime.now(datetime.timezone.utc))
        log_embed.add_field(name="Модератор", value=f"{ctx.author.mention}", inline=True)
        log_embed.add_field(name="Цель", value=f"{member.mention} (`{member.id}`)", inline=True)
        log_embed.add_field(name="Тип", value=f"Тип {license_type} ({type_name})", inline=False)
        log_embed.add_field(name="Отозвано", value=f"{len(active_licenses)} лицензий в {channels_fixed} каналах", inline=False)
        log_embed.set_footer(text=config.FOOTER_TEXT)
        await log_channel.send(embed=log_embed)

# ============================================================
# КОМАНДА: -data (БАЗА ДАННЫХ И ЗАМЕТКИ)
# ============================================================
@bot.group(name="data", invoke_without_command=True)
@commands.check(lambda ctx: has_access(ctx.author, config.DATA_ALLOWED_ROLE_IDS))
async def data(ctx):
    """Управление базой данных лицензий. Используйте -data help"""
    help_text = (
        "**Команды базы данных:**\n"
        f"`{config.PREFIX}data search <запрос>` — поиск по ID лицензии, ID юзера или Нику.\n"
        f"`{config.PREFIX}data note @пользователь <текст>` — добавить/изменить заметку.\n"
    )
    await ctx.send(embed=make_embed("📊 База данных лицензий", help_text, config.COLOR_INFO))

@data.command(name="search")
async def data_search(ctx: commands.Context, *, query: str):
    """Поиск лицензий."""
    results = database.search_licenses(query)
    
    # Если не нашли по ID/Лицензии, ищем по Нику
    if not results and not query.isdigit():
        all_user_ids = database.get_all_active_user_ids()
        for uid in all_user_ids:
            member = ctx.guild.get_member(uid)
            if member and (query.lower() in member.name.lower() or query.lower() in member.display_name.lower()):
                results.extend(database.get_user_licenses(uid))
        # Убираем дубликаты, если юзер попался несколько раз
        results = list({row[0]: row for row in results}.values())

    if not results:
        return await ctx.send(embed=make_embed("🔍 Поиск", "Ничего не найдено по вашему запросу.", config.COLOR_INFO))

    embed = discord.Embed(title=f"🔍 Результаты поиска: {query}", color=config.COLOR_INFO)
    
    # Группируем по пользователям для красивого вывода
    users_data = {}
    for row in results:
        uid = row[1]
        if uid not in users_data:
            users_data[uid] = []
        users_data[uid].append(row)

    for uid, licenses in users_data.items():
        member = ctx.guild.get_member(uid)
        name = member.mention if member else f"Не на сервере (`{uid}`)"
        
        lines = []
        for lic in licenses:
            lid, _, ltype, cid, status, issued, revoked = lic
            channel = bot.get_channel(cid)
            ch_status = channel.mention if channel else f"❌ Удален (`{cid}`)"
            type_str = "РП (1)" if ltype == 1 else "Мод (2)"
            st_str = "🟢 Активна" if status == 'active' else "🔴 Отозвана"
            
            lines.append(f"• `{lid}` | Тип: {type_str} | Канал: {ch_status} | {st_str}")
        
        val = "\n".join(lines)
        if len(val) > 1024: val = val[:1000] + "..."
        
        # Заметка
        note_data = database.get_note(uid)
        note_str = f"\n📝 **Заметка:** {note_data[0]}" if note_data else ""
        
        embed.add_field(name=name, value=val + note_str, inline=False)

    await ctx.send(embed=embed)

@data.command(name="note")
async def data_note(ctx: commands.Context, member: discord.Member, *, text: str):
    """Добавить или изменить заметку к пользователю."""
    if len(text) > 1000:
        return await ctx.send(embed=make_embed("❌ Ошибка", "Заметка слишком длинная (макс. 1000 символов).", config.COLOR_ERROR))
        
    database.set_note(member.id, text, ctx.author.id)
    await ctx.send(embed=make_embed("✅ Заметка сохранена", f"Заметка для {member.mention} успешно обновлена.", config.COLOR_SUCCESS))

# ============================================================
# ЗАПУСК
# ============================================================
if __name__ == "__main__":
    if not config.BOT_TOKEN:
        raise SystemExit("Не найден токен бота!")
    bot.run(config.BOT_TOKEN)