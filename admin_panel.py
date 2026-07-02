# -*- coding: utf-8 -*-
"""
Neway RP — Админ-панель (-admin).

Кнопочная панель управления ботом: база данных лицензий, выдача и отзыв
лицензий, заметки, безвозвратная очистка истории, удаление сообщений.
Одно сообщение, все «экраны» переключаются через редактирование этого
же сообщения. Ввод данных — через всплывающие окошки Discord (Modal).

⚠️ ФУНКЦИЯ В BETA-ТЕСТИРОВАНИИ. Возможны ошибки и изменения в будущем.

Доступ: только роли из config.ADMIN_PANEL_ALLOWED_ROLE_IDS.
Нажимать кнопки на панели может только тот, кто её вызвал командой -admin.

Вся бизнес-логика (выдача/отзыв лицензий, поиск, права на каналы и т.д.)
переиспользует те же функции и правила, что и обычные текстовые команды
в bot.py — панель — это просто альтернативный интерфейс к ним.
"""

from __future__ import annotations

import datetime
import re

import discord
from discord import ui

import config
import license_db

# ============================================================
#  ОБЩИЕ УТИЛИТЫ
# ============================================================

BETA_NOTICE = "🧪 *Эта функция находится в BETA-тестировании.*"


def _footer() -> str:
    return f"{config.FOOTER_TEXT} • Админ-панель (BETA)"


def make_panel_embed(title: str, description: str, color: int = None) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color if color is not None else config.COLOR_ADMIN,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_footer(text=_footer())
    return embed


def has_admin_panel_access(member: discord.Member) -> bool:
    return bool({r.id for r in member.roles} & config.ADMIN_PANEL_ALLOWED_ROLE_IDS)


def license_type_label(license_type: int) -> str:
    return "Участника РП" if license_type == 1 else "Менеджера РП"


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


def parse_user_id(raw: str) -> int | None:
    """Принимает как упоминание (<@id> / <@!id>), так и голый числовой ID."""
    raw = raw.strip()
    m = re.match(r"^<@!?(\d+)>$", raw)
    if m:
        return int(m.group(1))
    if raw.isdigit():
        return int(raw)
    return None


def parse_channel_id(raw: str) -> int | None:
    """Принимает как упоминание канала (<#id>), так и голый числовой ID."""
    raw = raw.strip()
    m = re.match(r"^<#(\d+)>$", raw)
    if m:
        return int(m.group(1))
    if raw.isdigit():
        return int(raw)
    return None


# ============================================================
#  ГЛАВНОЕ МЕНЮ
# ============================================================

MAIN_MENU_TEXT = (
    f"{BETA_NOTICE}\n\n"
    "Выберите раздел ниже. Все действия выполняются от вашего имени и "
    "логируются точно так же, как обычные текстовые команды.\n\n"
    "**Разделы:**\n"
    "🗃 **База данных** — поиск лицензий по ID, пользователю или никнейму\n"
    "📜 **Выдать лицензию** — выдать лицензию РП и права на канал\n"
    "🚫 **Отозвать лицензию** — отозвать лицензию и снять роль/права\n"
    "📝 **Заметка** — добавить/изменить заметку о пользователе\n"
    "🧾 **Wipe истории** — безвозвратно стереть историю лицензий (только Администратор)\n"
    "🗑 **Удалить сообщения** — очистить сообщения в канале (-del)\n"
    "🧹 **Удалить сообщения юзера** — очистить сообщения пользователя по серверу (-delus)\n"
    "📖 **Справка** — список команд и прав использования\n\n"
    f"Панель активна **{config.ADMIN_PANEL_TIMEOUT_SECONDS // 60} минут** "
    "с момента вызова, затем кнопки отключатся."
)


def build_main_menu_embed() -> discord.Embed:
    return make_panel_embed("🛠 Админ-панель Neway RP", MAIN_MENU_TEXT)


# ============================================================
#  БАЗОВЫЙ VIEW С ПРОВЕРКОЙ АВТОРА И ТАЙМАУТОМ
# ============================================================

class OwnerOnlyView(ui.View):
    """
    View, кнопки которого может нажимать только пользователь, вызвавший
    панель (owner_id). По истечении timeout все компоненты отключаются
    прямо на исходном сообщении.
    """

    def __init__(self, owner_id: int, *, timeout: float = None):
        super().__init__(timeout=timeout or config.ADMIN_PANEL_TIMEOUT_SECONDS)
        self.owner_id = owner_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "⛔ Эта панель вызвана не вами — управлять ей может только автор вызова.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ============================================================
#  ГЛАВНОЕ МЕНЮ — VIEW
# ============================================================

class MainMenuView(OwnerOnlyView):
    def __init__(self, bot: "discord.Client", owner_id: int):
        super().__init__(owner_id)
        self.bot = bot

    async def _goto(self, interaction: discord.Interaction, embed: discord.Embed, view: "OwnerOnlyView"):
        view.message = self.message
        await interaction.response.edit_message(embed=embed, view=view)

    @ui.button(label="База данных", emoji="🗃", style=discord.ButtonStyle.primary, row=0)
    async def btn_data(self, interaction: discord.Interaction, button: ui.Button):
        if not has_role(interaction.user, config.DATA_ALLOWED_ROLE_IDS):
            await interaction.response.send_message(NO_ACCESS_TEXT, ephemeral=True)
            return
        view = DataSearchView(self.bot, self.owner_id)
        embed = make_panel_embed(
            "🗃 База данных лицензий",
            f"{BETA_NOTICE}\n\n"
            "Нажмите **Найти**, чтобы ввести запрос: ID лицензии (`RPP-0000001`), "
            "упоминание/ID пользователя или никнейм.",
        )
        await self._goto(interaction, embed, view)

    @ui.button(label="Выдать лицензию", emoji="📜", style=discord.ButtonStyle.success, row=0)
    async def btn_license(self, interaction: discord.Interaction, button: ui.Button):
        if not has_role(interaction.user, config.LICENSE_ALLOWED_ROLE_IDS):
            await interaction.response.send_message(NO_ACCESS_TEXT, ephemeral=True)
            return
        view = LicenseIssueView(self.bot, self.owner_id)
        embed = make_panel_embed(
            "📜 Выдать лицензию",
            f"{BETA_NOTICE}\n\n"
            "Нажмите **Выдать**, чтобы указать пользователя, тип лицензии (1 или 2) "
            "и ID канала-форума из категории лицензий.",
        )
        await self._goto(interaction, embed, view)

    @ui.button(label="Отозвать лицензию", emoji="🚫", style=discord.ButtonStyle.danger, row=0)
    async def btn_cancel(self, interaction: discord.Interaction, button: ui.Button):
        if not has_role(interaction.user, config.CANCEL_ALLOWED_ROLE_IDS):
            await interaction.response.send_message(NO_ACCESS_TEXT, ephemeral=True)
            return
        view = LicenseCancelView(self.bot, self.owner_id)
        embed = make_panel_embed(
            "🚫 Отозвать лицензию",
            f"{BETA_NOTICE}\n\n"
            "Нажмите **Отозвать**, чтобы указать пользователя и тип лицензии (1 или 2).",
        )
        await self._goto(interaction, embed, view)

    @ui.button(label="Заметка", emoji="📝", style=discord.ButtonStyle.secondary, row=1)
    async def btn_note(self, interaction: discord.Interaction, button: ui.Button):
        if not has_role(interaction.user, config.DATA_ALLOWED_ROLE_IDS):
            await interaction.response.send_message(NO_ACCESS_TEXT, ephemeral=True)
            return
        view = NoteView(self.bot, self.owner_id)
        embed = make_panel_embed(
            "📝 Заметка о пользователе",
            f"{BETA_NOTICE}\n\n"
            "Нажмите **Записать**, чтобы указать пользователя и текст заметки. "
            "Существующая заметка будет заменена.",
        )
        await self._goto(interaction, embed, view)

    @ui.button(label="Wipe истории", emoji="🧾", style=discord.ButtonStyle.danger, row=1)
    async def btn_wipe(self, interaction: discord.Interaction, button: ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Очистка истории лицензий доступна только участникам "
                "с правом **Администратор**.",
                ephemeral=True,
            )
            return
        view = WipeView(self.bot, self.owner_id)
        embed = make_panel_embed(
            "🧾 Безвозвратная очистка истории лицензий",
            f"{BETA_NOTICE}\n\n"
            "⚠️ Это действие **необратимо**. Нажмите **Найти пользователя**, чтобы "
            "посмотреть, что будет удалено, прежде чем подтвердить.",
        )
        await self._goto(interaction, embed, view)

    @ui.button(label="Удалить сообщения", emoji="🗑", style=discord.ButtonStyle.secondary, row=2)
    async def btn_del(self, interaction: discord.Interaction, button: ui.Button):
        channel = interaction.channel
        if not has_delete_access(interaction.user, channel):
            await interaction.response.send_message(
                "⛔ У вас нет доступа к удалению сообщений в этом канале.",
                ephemeral=True,
            )
            return
        view = DelView(self.bot, self.owner_id)
        embed = make_panel_embed(
            "🗑 Удалить сообщения в канале",
            f"{BETA_NOTICE}\n\n"
            f"Удаление будет выполнено в канале {channel.mention} (где открыта панель).\n"
            "Нажмите **Удалить**, чтобы указать количество сообщений.",
        )
        await self._goto(interaction, embed, view)

    @ui.button(label="Удалить сообщения юзера", emoji="🧹", style=discord.ButtonStyle.secondary, row=2)
    async def btn_delus(self, interaction: discord.Interaction, button: ui.Button):
        channel = interaction.channel
        if not has_delete_access(interaction.user, channel):
            await interaction.response.send_message(
                "⛔ У вас нет доступа к удалению сообщений в этом канале.",
                ephemeral=True,
            )
            return
        view = DelUsView(self.bot, self.owner_id)
        embed = make_panel_embed(
            "🧹 Удалить сообщения пользователя по серверу",
            f"{BETA_NOTICE}\n\n"
            "Нажмите **Удалить**, чтобы указать пользователя и количество сообщений.",
        )
        await self._goto(interaction, embed, view)

    @ui.button(label="Справка", emoji="📖", style=discord.ButtonStyle.secondary, row=3)
    async def btn_help(self, interaction: discord.Interaction, button: ui.Button):
        view = HelpView(self.bot, self.owner_id)
        embed = build_help_embed(interaction.user)
        await self._goto(interaction, embed, view)

    @ui.button(label="Закрыть", emoji="🔒", style=discord.ButtonStyle.danger, row=3)
    async def btn_close(self, interaction: discord.Interaction, button: ui.Button):
        for item in self.children:
            item.disabled = True
        embed = make_panel_embed(
            "🔒 Панель закрыта",
            "Админ-панель закрыта. Вызовите `-admin` заново, чтобы открыть её снова.",
        )
        self.stop()
        await interaction.response.edit_message(embed=embed, view=self)


NO_ACCESS_TEXT = "⛔ У вас нет доступа к этому разделу панели."


# ============================================================
#  ОБЩАЯ КНОПКА «НАЗАД В МЕНЮ»
# ============================================================

class BackButton(ui.Button):
    def __init__(self, row: int = 4):
        super().__init__(label="Назад в меню", emoji="↩️", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction):
        view: "SubView" = self.view
        menu = MainMenuView(view.bot, view.owner_id)
        menu.message = view.message
        await interaction.response.edit_message(embed=build_main_menu_embed(), view=menu)


class SubView(OwnerOnlyView):
    """Базовый класс для «экранов» разделов — хранит bot и добавляет кнопку назад."""

    def __init__(self, bot: "discord.Client", owner_id: int, *, back_row: int = 4):
        super().__init__(owner_id)
        self.bot = bot
        self.add_item(BackButton(row=back_row))


# ============================================================
#  РАЗДЕЛ: БАЗА ДАННЫХ (-data)
# ============================================================

class DataSearchModal(ui.Modal, title="Поиск в базе данных"):
    query = ui.TextInput(
        label="ID лицензии / упоминание / ID / никнейм",
        placeholder="RPP-0000001, @user, 123456789012345678 или ник",
        required=True,
        max_length=100,
    )

    def __init__(self, view: "DataSearchView"):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        query = str(self.query.value).strip()
        records: list[dict] = []
        search_label = query

        if re.match(r"^RP[PM]-\d{7}$", query.upper()):
            records = license_db.search_by_license_id(query.upper())
            search_label = f"ID лицензии `{query.upper()}`"
        else:
            uid = parse_user_id(query)
            if uid is not None:
                records = license_db.search_by_discord_id(uid)
                search_label = f"Discord ID `{uid}`"
            else:
                records = license_db.search_by_username(query)
                search_label = f"никнейм `{query}`"

        embed = build_data_results_embed(interaction.guild, search_label, records)
        await interaction.response.edit_message(embed=embed, view=self.view_ref)


def build_data_results_embed(guild: discord.Guild, search_label: str, records: list[dict]) -> discord.Embed:
    if not records:
        return make_panel_embed(
            "🔍 Ничего не найдено",
            f"{BETA_NOTICE}\n\nПо запросу {search_label} лицензий не найдено.",
        )

    users_data: dict[int, list[dict]] = {}
    for rec in records:
        users_data.setdefault(rec["discord_id"], []).append(rec)

    def resolve_mod(user_id: int | None) -> str:
        if not user_id:
            return "—"
        member = guild.get_member(user_id)
        return member.mention if member else f"`{user_id}` (не на сервере)"

    lines = [f"{BETA_NOTICE}\n\n**Найдено пользователей: {len(users_data)}**\n"]
    for discord_id, user_records in users_data.items():
        first = user_records[0]
        lines.append(f"\n**👤 {first['username']}** (`{discord_id}`)")
        for rec in user_records[:5]:
            status = "🟢" if not rec["revoked"] else "🔴"
            ch = guild.get_channel(rec["channel_id"])
            ch_text = ch.mention if ch else f"~~удалён~~ (`{rec['channel_id']}`)"
            lines.append(
                f"{status} **{rec['license_id']}** — Тип {rec['license_type']} — {ch_text} "
                f"— выдал {resolve_mod(rec.get('issued_by'))}"
            )
        if len(user_records) > 5:
            lines.append(f"…и ещё {len(user_records) - 5} записей")
        note = license_db.get_note(discord_id)
        if note:
            preview = note if len(note) <= 150 else note[:150] + "…"
            lines.append(f"📝 Заметка: {preview}")

    description = "\n".join(lines)
    if len(description) > 4000:
        description = description[:3950] + "\n…результат обрезан"

    return make_panel_embed(f"🗃 Результаты поиска — {search_label}", description)


class DataSearchView(SubView):
    @ui.button(label="Найти", emoji="🔍", style=discord.ButtonStyle.primary, row=0)
    async def btn_search(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(DataSearchModal(self))


# ============================================================
#  РАЗДЕЛ: ВЫДАТЬ ЛИЦЕНЗИЮ (-license)
# ============================================================

class LicenseIssueModal(ui.Modal, title="Выдать лицензию"):
    user_input = ui.TextInput(label="Пользователь (упоминание или ID)", required=True, max_length=100)
    type_input = ui.TextInput(label="Тип лицензии (1 или 2)", required=True, max_length=1)
    channel_input = ui.TextInput(label="ID канала-форума", required=True, max_length=30)

    def __init__(self, view: "LicenseIssueView"):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild

        uid = parse_user_id(str(self.user_input.value))
        member = guild.get_member(uid) if uid else None
        if member is None:
            await interaction.response.edit_message(
                embed=make_panel_embed("❌ Пользователь не найден", "Проверьте упоминание или ID и попробуйте снова.", config.COLOR_ERROR),
                view=self.view_ref,
            )
            return

        try:
            license_type = int(str(self.type_input.value).strip())
        except ValueError:
            license_type = None
        if license_type not in (1, 2):
            await interaction.response.edit_message(
                embed=make_panel_embed("❌ Неверный тип лицензии", "Тип должен быть **1** или **2**.", config.COLOR_ERROR),
                view=self.view_ref,
            )
            return

        channel_id = parse_channel_id(str(self.channel_input.value))
        target_channel = guild.get_channel(channel_id) if channel_id else None
        if target_channel is None:
            await interaction.response.edit_message(
                embed=make_panel_embed("❌ Канал не найден", f"Канал с ID `{channel_id}` не существует.", config.COLOR_ERROR),
                view=self.view_ref,
            )
            return

        if not isinstance(target_channel, discord.ForumChannel):
            await interaction.response.edit_message(
                embed=make_panel_embed("❌ Неверный тип канала", "Указанный ID должен принадлежать каналу-форуму.", config.COLOR_ERROR),
                view=self.view_ref,
            )
            return

        if target_channel.category_id != config.LICENSE_CATEGORY_ID:
            await interaction.response.edit_message(
                embed=make_panel_embed(
                    "⛔ Канал не в разрешённой категории",
                    f"Канал {target_channel.mention} не входит в категорию, доступную для лицензий.",
                    config.COLOR_ERROR,
                ),
                view=self.view_ref,
            )
            return

        existing = license_db.get_active_license_by_channel(channel_id)
        if existing is not None:
            await interaction.response.edit_message(
                embed=make_panel_embed(
                    "⛔ Каналу уже назначена лицензия",
                    f"На канал {target_channel.mention} уже выдана активная лицензия "
                    f"`{existing['license_id']}` для <@{existing['discord_id']}>.",
                    config.COLOR_ERROR,
                ),
                view=self.view_ref,
            )
            return

        role = guild.get_role(license_role_id(license_type))
        if role is None:
            await interaction.response.edit_message(
                embed=make_panel_embed("❌ Роль не найдена", "Роль лицензии не найдена на сервере.", config.COLOR_ERROR),
                view=self.view_ref,
            )
            return

        try:
            await member.add_roles(role, reason=f"Лицензия тип {license_type} от {interaction.user} (через админ-панель)")
        except discord.HTTPException as e:
            await interaction.response.edit_message(
                embed=make_panel_embed("❌ Не удалось выдать роль", f"`{e}`", config.COLOR_ERROR),
                view=self.view_ref,
            )
            return

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
                member, overwrite=overwrite,
                reason=f"Лицензия тип {license_type} от {interaction.user} (через админ-панель)",
            )
        except discord.HTTPException as e:
            await interaction.response.edit_message(
                embed=make_panel_embed("❌ Не удалось выдать права на канал", f"`{e}`", config.COLOR_ERROR),
                view=self.view_ref,
            )
            return

        try:
            lic_id = license_db.issue_license(
                license_type=license_type,
                discord_id=member.id,
                username=str(member),
                channel_id=channel_id,
                issued_by=interaction.user.id,
            )
        except license_db.ChannelAlreadyLicensedError as e:
            try:
                await member.remove_roles(role, reason="Откат: канал уже занят другой лицензией")
            except discord.HTTPException:
                pass
            try:
                await target_channel.set_permissions(member, overwrite=None, reason="Откат: канал уже занят другой лицензией")
            except discord.HTTPException:
                pass
            await interaction.response.edit_message(
                embed=make_panel_embed(
                    "⛔ Каналу уже назначена лицензия",
                    f"На канал уже выдана активная лицензия `{e.existing['license_id']}`.",
                    config.COLOR_ERROR,
                ),
                view=self.view_ref,
            )
            return

        label = license_type_label(license_type)
        await interaction.response.edit_message(
            embed=make_panel_embed(
                f"✅ Лицензия {label} выдана",
                f"{BETA_NOTICE}\n\n"
                f"{member.mention} получил лицензию **{label}**.\n"
                f"Канал: {target_channel.mention}\nID лицензии: `{lic_id}`",
                config.COLOR_SUCCESS,
            ),
            view=self.view_ref,
        )

        log_channel = self.view_ref.bot.get_channel(config.LICENSE_ISSUE_LOG_CHANNEL_ID)
        if log_channel:
            log_embed = discord.Embed(
                title=f"📜 Выдана лицензия — {label} (через админ-панель)",
                color=config.COLOR_LICENSE,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            log_embed.add_field(name="👤 Получатель", value=f"{member.mention} (`{member}`)", inline=False)
            log_embed.add_field(name="🛠 Выдал", value=f"{interaction.user.mention} (`{interaction.user}`)", inline=False)
            log_embed.add_field(name="📋 Тип", value=f"Тип {license_type} — {label}", inline=True)
            log_embed.add_field(name="🆔 ID лицензии", value=f"`{lic_id}`", inline=True)
            log_embed.add_field(name="📍 Канал", value=target_channel.mention, inline=False)
            log_embed.set_footer(text=_footer())
            await log_channel.send(embed=log_embed)


class LicenseIssueView(SubView):
    @ui.button(label="Выдать", emoji="📜", style=discord.ButtonStyle.success, row=0)
    async def btn_issue(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(LicenseIssueModal(self))


# ============================================================
#  РАЗДЕЛ: ОТОЗВАТЬ ЛИЦЕНЗИЮ (-cancel)
# ============================================================

class LicenseCancelModal(ui.Modal, title="Отозвать лицензию"):
    user_input = ui.TextInput(label="Пользователь (упоминание или ID)", required=True, max_length=100)
    type_input = ui.TextInput(label="Тип лицензии (1 или 2)", required=True, max_length=1)

    def __init__(self, view: "LicenseCancelView"):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild

        uid = parse_user_id(str(self.user_input.value))
        member = guild.get_member(uid) if uid else None
        if member is None:
            await interaction.response.edit_message(
                embed=make_panel_embed("❌ Пользователь не найден", "Проверьте упоминание или ID и попробуйте снова.", config.COLOR_ERROR),
                view=self.view_ref,
            )
            return

        try:
            license_type = int(str(self.type_input.value).strip())
        except ValueError:
            license_type = None
        if license_type not in (1, 2):
            await interaction.response.edit_message(
                embed=make_panel_embed("❌ Неверный тип лицензии", "Тип должен быть **1** или **2**.", config.COLOR_ERROR),
                view=self.view_ref,
            )
            return

        label = license_type_label(license_type)
        role_id = license_role_id(license_type)

        revoked_records = license_db.revoke_licenses(
            discord_id=member.id, license_type=license_type, revoked_by=interaction.user.id,
        )
        if not revoked_records:
            await interaction.response.edit_message(
                embed=make_panel_embed(
                    "ℹ️ Нет активных лицензий",
                    f"У {member.mention} нет активных лицензий типа **{license_type}** ({label}).",
                    config.COLOR_INFO,
                ),
                view=self.view_ref,
            )
            return

        role = guild.get_role(role_id)
        role_removed = False
        if role and role in member.roles:
            try:
                await member.remove_roles(role, reason=f"Отзыв лицензии тип {license_type} от {interaction.user} (панель)")
                role_removed = True
            except discord.HTTPException:
                pass

        affected_channels = []
        for rec in revoked_records:
            ch = guild.get_channel(rec["channel_id"])
            if ch is not None:
                try:
                    await ch.set_permissions(member, overwrite=None, reason=f"Отзыв лицензии тип {license_type} от {interaction.user} (панель)")
                    affected_channels.append(ch)
                except discord.HTTPException:
                    pass

        channels_text = ", ".join(ch.mention for ch in affected_channels) if affected_channels else "—"
        revoked_ids = ", ".join(f"`{r['license_id']}`" for r in revoked_records)

        await interaction.response.edit_message(
            embed=make_panel_embed(
                f"✅ Лицензия {label} отозвана",
                f"{BETA_NOTICE}\n\n"
                f"У {member.mention} отозваны лицензии типа **{license_type}** ({label}).\n"
                f"ID лицензий: {revoked_ids}\nКаналы: {channels_text}\n"
                f"Роль снята: {'да' if role_removed else 'нет'}",
                config.COLOR_CANCEL,
            ),
            view=self.view_ref,
        )

        log_channel = self.view_ref.bot.get_channel(config.LICENSE_REVOKE_LOG_CHANNEL_ID)
        if log_channel:
            log_embed = discord.Embed(
                title=f"🚫 Отозвана лицензия — {label} (через админ-панель)",
                color=config.COLOR_REVOKE,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            log_embed.add_field(name="🎯 Цель", value=f"{member.mention} (`{member}`)", inline=False)
            log_embed.add_field(name="🛠 Модератор", value=f"{interaction.user.mention} (`{interaction.user}`)", inline=False)
            log_embed.add_field(name="📋 Тип", value=f"Тип {license_type} — {label}", inline=True)
            log_embed.add_field(name="🆔 ID лицензий", value=revoked_ids, inline=False)
            log_embed.add_field(name="📍 Каналы", value=channels_text, inline=False)
            log_embed.set_footer(text=_footer())
            await log_channel.send(embed=log_embed)


class LicenseCancelView(SubView):
    @ui.button(label="Отозвать", emoji="🚫", style=discord.ButtonStyle.danger, row=0)
    async def btn_cancel_do(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(LicenseCancelModal(self))


# ============================================================
#  РАЗДЕЛ: ЗАМЕТКА (-note)
# ============================================================

class NoteModal(ui.Modal, title="Заметка о пользователе"):
    user_input = ui.TextInput(label="Пользователь (упоминание или ID)", required=True, max_length=100)
    text_input = ui.TextInput(label="Текст заметки", style=discord.TextStyle.paragraph, required=True, max_length=1000)

    def __init__(self, view: "NoteView"):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        uid = parse_user_id(str(self.user_input.value))
        member = interaction.guild.get_member(uid) if uid else None
        if member is None:
            await interaction.response.edit_message(
                embed=make_panel_embed("❌ Пользователь не найден", "Проверьте упоминание или ID и попробуйте снова.", config.COLOR_ERROR),
                view=self.view_ref,
            )
            return

        text = str(self.text_input.value)
        license_db.set_note(discord_id=member.id, note_text=text, updated_by=interaction.user.id)

        await interaction.response.edit_message(
            embed=make_panel_embed(
                "✅ Заметка сохранена",
                f"{BETA_NOTICE}\n\nЗаметка для {member.mention} обновлена.\n**Текст:** {text}",
                config.COLOR_SUCCESS,
            ),
            view=self.view_ref,
        )


class NoteView(SubView):
    @ui.button(label="Записать", emoji="📝", style=discord.ButtonStyle.primary, row=0)
    async def btn_note_do(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(NoteModal(self))


# ============================================================
#  РАЗДЕЛ: WIPE ИСТОРИИ (-wipe)
# ============================================================

class WipeFindModal(ui.Modal, title="Найти пользователя для очистки"):
    user_input = ui.TextInput(label="Пользователь (упоминание или ID)", required=True, max_length=100)

    def __init__(self, view: "WipeView"):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        uid = parse_user_id(str(self.user_input.value))
        member = interaction.guild.get_member(uid) if uid else None
        if member is None:
            await interaction.response.edit_message(
                embed=make_panel_embed("❌ Пользователь не найден", "Проверьте упоминание или ID и попробуйте снова.", config.COLOR_ERROR),
                view=self.view_ref,
            )
            return

        history = license_db.search_by_discord_id(member.id)
        if not history:
            await interaction.response.edit_message(
                embed=make_panel_embed(
                    "ℹ️ История пуста",
                    f"У {member.mention} нет ни одной записи в истории лицензий.",
                    config.COLOR_INFO,
                ),
                view=self.view_ref,
            )
            return

        active = [r for r in history if not r["revoked"]]
        revoked = [r for r in history if r["revoked"]]

        self.view_ref.target_member = member

        warning = (
            f"{BETA_NOTICE}\n\n"
            f"У {member.mention} найдено **{len(history)}** записей:\n"
            f"• Активных: **{len(active)}**\n• Отозванных: **{len(revoked)}**\n\n"
        )
        if active:
            warning += (
                "⚠️ Среди них есть **активные** лицензии — очистка истории **не снимает** "
                "роль и права на канале. Сначала отзовите их отдельно, если нужно.\n\n"
            )
        warning += "Нажмите **Подтвердить удаление**, чтобы стереть эти записи безвозвратно."

        self.view_ref.confirm_button.disabled = False
        await interaction.response.edit_message(
            embed=make_panel_embed("⚠️ Подтвердите очистку истории", warning, config.COLOR_REVOKE),
            view=self.view_ref,
        )


class WipeView(SubView):
    def __init__(self, bot: "discord.Client", owner_id: int):
        super().__init__(bot, owner_id)
        self.target_member: discord.Member | None = None

    @ui.button(label="Найти пользователя", emoji="🔍", style=discord.ButtonStyle.primary, row=0)
    async def btn_find(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(WipeFindModal(self))

    @ui.button(label="Подтвердить удаление", emoji="🗑", style=discord.ButtonStyle.danger, row=1, disabled=True)
    async def confirm_button(self, interaction: discord.Interaction, button: ui.Button):
        if self.target_member is None:
            await interaction.response.send_message("Сначала найдите пользователя.", ephemeral=True)
            return

        member = self.target_member
        deleted = license_db.clear_history(member.id)
        button.disabled = True
        self.target_member = None

        await interaction.response.edit_message(
            embed=make_panel_embed(
                "🧾 История лицензий очищена",
                f"{BETA_NOTICE}\n\nПользователь: {member.mention} (`{member.id}`)\n"
                f"Удалено записей: **{deleted}**",
                config.COLOR_SUCCESS,
            ),
            view=self,
        )

        log_channel = self.bot.get_channel(config.CANCEL_LOG_CHANNEL_ID)
        if log_channel:
            log_embed = discord.Embed(
                title="🧾 Очищена история лицензий (через админ-панель)",
                color=config.COLOR_REVOKE,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            log_embed.add_field(name="👤 Пользователь", value=f"{member.mention} (`{member}`)", inline=False)
            log_embed.add_field(name="🛠 Выполнил", value=f"{interaction.user.mention} (`{interaction.user}`)", inline=False)
            log_embed.add_field(name="🗑 Удалено записей", value=str(deleted), inline=True)
            log_embed.set_footer(text=_footer())
            await log_channel.send(embed=log_embed)


# ============================================================
#  РАЗДЕЛ: УДАЛИТЬ СООБЩЕНИЯ В КАНАЛЕ (-del)
# ============================================================

class DelModal(ui.Modal, title="Удалить сообщения в канале"):
    amount_input = ui.TextInput(label="Количество сообщений", required=True, max_length=6)

    def __init__(self, view: "DelView"):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(str(self.amount_input.value).strip())
        except ValueError:
            amount = None

        if amount is None or amount <= 0:
            await interaction.response.edit_message(
                embed=make_panel_embed("❌ Неверное количество", "Введите положительное целое число.", config.COLOR_ERROR),
                view=self.view_ref,
            )
            return

        if amount > config.DEL_MAX_MESSAGES:
            await interaction.response.edit_message(
                embed=make_panel_embed(
                    "❌ Превышен лимит",
                    f"За один раз можно удалить не более **{config.DEL_MAX_MESSAGES}** сообщений.",
                    config.COLOR_ERROR,
                ),
                view=self.view_ref,
            )
            return

        channel = interaction.channel
        await interaction.response.defer(ephemeral=False)
        try:
            deleted = await channel.purge(limit=amount)
        except discord.HTTPException as e:
            await interaction.edit_original_response(
                embed=make_panel_embed("❌ Ошибка Discord", f"`{e}`", config.COLOR_ERROR),
                view=self.view_ref,
            )
            return

        await interaction.edit_original_response(
            embed=make_panel_embed(
                "✅ Сообщения удалены",
                f"{BETA_NOTICE}\n\nУдалено **{len(deleted)}** сообщений в {channel.mention}.",
                config.COLOR_SUCCESS,
            ),
            view=self.view_ref,
        )

        log_channel = self.view_ref.bot.get_channel(config.DELETE_LOG_CHANNEL_ID)
        if log_channel:
            log_embed = discord.Embed(
                title="🗑 Лог команды -del (через админ-панель)",
                color=config.COLOR_DELETE,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            log_embed.add_field(name="🛠 Модератор", value=f"{interaction.user.mention} (`{interaction.user}`)", inline=False)
            log_embed.add_field(name="📍 Канал", value=channel.mention, inline=False)
            log_embed.add_field(name="🔢 Запрошено / Удалено", value=f"{amount} / {len(deleted)}", inline=False)
            log_embed.set_footer(text=_footer())
            await log_channel.send(embed=log_embed)


class DelView(SubView):
    @ui.button(label="Удалить", emoji="🗑", style=discord.ButtonStyle.danger, row=0)
    async def btn_del_do(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(DelModal(self))


# ============================================================
#  РАЗДЕЛ: УДАЛИТЬ СООБЩЕНИЯ ПОЛЬЗОВАТЕЛЯ (-delus)
# ============================================================

class DelUsModal(ui.Modal, title="Удалить сообщения пользователя"):
    user_input = ui.TextInput(label="Пользователь (упоминание или ID)", required=True, max_length=100)
    amount_input = ui.TextInput(label="Количество сообщений", required=True, max_length=6)

    def __init__(self, view: "DelUsView"):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        uid = parse_user_id(str(self.user_input.value))
        member = guild.get_member(uid) if uid else None
        if member is None:
            await interaction.response.edit_message(
                embed=make_panel_embed("❌ Пользователь не найден", "Проверьте упоминание или ID и попробуйте снова.", config.COLOR_ERROR),
                view=self.view_ref,
            )
            return

        try:
            amount = int(str(self.amount_input.value).strip())
        except ValueError:
            amount = None
        if amount is None or amount <= 0:
            await interaction.response.edit_message(
                embed=make_panel_embed("❌ Неверное количество", "Введите положительное целое число.", config.COLOR_ERROR),
                view=self.view_ref,
            )
            return
        if amount > config.DELUS_MAX_MESSAGES:
            await interaction.response.edit_message(
                embed=make_panel_embed(
                    "❌ Превышен лимит",
                    f"За один раз можно удалить не более **{config.DELUS_MAX_MESSAGES}** сообщений пользователя.",
                    config.COLOR_ERROR,
                ),
                view=self.view_ref,
            )
            return

        await interaction.response.edit_message(
            embed=make_panel_embed(
                "⏳ Идёт удаление",
                f"{BETA_NOTICE}\n\nИщу и удаляю до **{amount}** последних сообщений "
                f"{member.mention} по всем каналам...",
                config.COLOR_INFO,
            ),
            view=self.view_ref,
        )

        target_messages = []
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me)
            if not (perms.view_channel and perms.read_message_history and perms.manage_messages):
                continue
            try:
                async for msg in channel.history(limit=config.DELUS_SCAN_LIMIT_PER_CHANNEL):
                    if msg.author.id == member.id:
                        target_messages.append(msg)
            except discord.HTTPException:
                continue

        target_messages.sort(key=lambda m: m.created_at, reverse=True)
        target_messages = target_messages[:amount]

        if not target_messages:
            await interaction.edit_original_response(
                embed=make_panel_embed(
                    "ℹ️ Сообщения не найдены",
                    f"Не найдено ни одного сообщения от {member.mention} в доступных каналах.",
                    config.COLOR_INFO,
                ),
                view=self.view_ref,
            )
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

        await interaction.edit_original_response(
            embed=make_panel_embed(
                "✅ Сообщения удалены",
                f"{BETA_NOTICE}\n\nУдалено **{deleted_total}** сообщений пользователя "
                f"{member.mention} в **{len(deleted_per_channel)}** канал(ах).",
                config.COLOR_SUCCESS,
            ),
            view=self.view_ref,
        )

        log_channel = self.view_ref.bot.get_channel(config.DELETE_LOG_CHANNEL_ID)
        if log_channel:
            breakdown = "\n".join(f"{ch.mention} — {c}" for ch, c in deleted_per_channel.items()) or "—"
            if len(breakdown) > 1024:
                breakdown = breakdown[:1000] + "\n…"
            log_embed = discord.Embed(
                title="🗑 Лог команды -delus (через админ-панель)",
                color=config.COLOR_DELETE,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            log_embed.add_field(name="🛠 Модератор", value=f"{interaction.user.mention} (`{interaction.user}`)", inline=False)
            log_embed.add_field(name="🎯 Цель", value=f"{member.mention} (`{member}`)", inline=False)
            log_embed.add_field(name="🔢 Запрошено / Удалено", value=f"{amount} / {deleted_total}", inline=False)
            log_embed.add_field(name="📍 Каналы", value=breakdown, inline=False)
            log_embed.set_footer(text=_footer())
            await log_channel.send(embed=log_embed)


class DelUsView(SubView):
    @ui.button(label="Удалить", emoji="🧹", style=discord.ButtonStyle.danger, row=0)
    async def btn_delus_do(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(DelUsModal(self))


# ============================================================
#  РАЗДЕЛ: СПРАВКА
# ============================================================

def has_role(member: discord.Member, role_ids: set) -> bool:
    return bool({r.id for r in member.roles} & role_ids)


def has_delete_access(member: discord.Member, channel: discord.abc.GuildChannel) -> bool:
    """Дублирует bot.has_delete_access — держим отдельную копию, чтобы модуль
    панели не зависел от bot.py (иначе получится циклический импорт:
    bot.py импортирует admin_panel, admin_panel импортировал бы bot)."""
    member_role_ids = {r.id for r in member.roles}
    if member_role_ids & config.DELETE_GLOBAL_ROLE_IDS:
        return True

    category_id = getattr(channel, "category_id", None)
    in_main_category = category_id == config.DELETE_CATEGORY_ID
    in_exempt_category = category_id == config.DELETE_EXEMPT_CATEGORY_ID
    if not (in_main_category or in_exempt_category):
        return False

    if member_role_ids & config.DELETE_EXEMPT_ROLE_IDS:
        return not in_exempt_category
    if member_role_ids & config.DELETE_NO_EXEMPT_ROLE_IDS:
        return True
    return False


HELP_TEXT = (
    f"{BETA_NOTICE}\n\n"
    "**Команды бота и права на использование:**\n\n"
    "🚩 `-report @user` — репорт на участника. Доступно всем.\n\n"
    "📜 `-license @user <1|2> <ID канала>` — выдать лицензию.\n"
    f"Роли: <@&1520321736673853512>, <@&1520321736673853511>, <@&1520321736673853509>.\n"
    f"Только в <#{config.LICENSE_COMMANDS_CHANNEL_ID}> (админам — везде).\n\n"
    "🚫 `-cancel @user <1|2>` — отозвать лицензию.\n"
    f"Только в <#{config.LICENSE_COMMANDS_CHANNEL_ID}> (админам — везде).\n\n"
    "🗃 `-data [запрос]` / 📝 `-note @user текст` — база данных и заметки.\n"
    f"Только в <#{config.LICENSE_COMMANDS_CHANNEL_ID}> (админам — везде, кроме -note).\n\n"
    "🧾 `-wipe @user confirm` — безвозвратная очистка истории. Только Администратор.\n\n"
    f"🗑 `-del <кол-во>` / 🧹 `-delus @user <кол-во>` — удаление сообщений.\n"
    f"«Супер-роли» — везде. Остальные допущенные роли — только в категории "
    f"<#{config.DELETE_CATEGORY_ID}> (для части ролей есть исключение по подкатегории "
    f"<#{config.DELETE_EXEMPT_CATEGORY_ID}>).\n\n"
    "🛠 `-admin` — эта панель.\n"
    f"Роли: <@&1520321736673853512>, <@&1520321736673853511>, "
    f"<@&1520321736673853509>, <@&1520321736648949932>.\n\n"
    "Подробное описание каждой команды — текстовая команда `-help` "
    "(показывает только то, что доступно вам)."
)


def build_help_embed(user: discord.Member) -> discord.Embed:
    return make_panel_embed("📖 Справка по командам", HELP_TEXT)


class HelpView(SubView):
    pass  # только кнопка «Назад в меню», добавленная в SubView


# ============================================================
#  ТОЧКА ВХОДА: ЗАПУСК ПАНЕЛИ
# ============================================================

async def open_admin_panel(bot: "discord.Client", ctx) -> None:
    """
    Открывает главное меню админ-панели в ответ на команду -admin.
    ctx — discord.ext.commands.Context.
    """
    view = MainMenuView(bot, ctx.author.id)
    message = await ctx.send(embed=build_main_menu_embed(), view=view)
    view.message = message
