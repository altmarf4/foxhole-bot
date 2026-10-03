from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

import discord

from foxbot.constants import PANEL_TIMEOUT, Colour
from foxbot.core.text import SELECT_DESCRIPTION_LIMIT, SELECT_LABEL_LIMIT, clip

log = logging.getLogger(__name__)

Callback = Callable[[discord.Interaction], Awaitable[Any]]


def _message_kwargs(
    content: str | None,
    embed: discord.Embed | None,
    embeds: Sequence[discord.Embed] | None,
    view: discord.ui.View | None,
    file: discord.File | None,
    files: Sequence[discord.File] | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if embeds is not None:
        kwargs["embeds"] = list(embeds)
    if view is not None:
        kwargs["view"] = view
    if file is not None:
        kwargs["file"] = file
    if files is not None:
        kwargs["files"] = list(files)
    return kwargs


async def reply(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    embeds: Sequence[discord.Embed] | None = None,
    view: discord.ui.View | None = None,
    file: discord.File | None = None,
    files: Sequence[discord.File] | None = None,
    ephemeral: bool = True,
) -> None:
    kwargs = _message_kwargs(content, embed, embeds, view, file, files)
    if interaction.response.is_done():
        await interaction.followup.send(ephemeral=ephemeral, **kwargs)
    else:
        await interaction.response.send_message(ephemeral=ephemeral, **kwargs)


async def edit(interaction: discord.Interaction, **kwargs: Any) -> None:
    if interaction.response.is_done():
        await interaction.edit_original_response(**kwargs)
    else:
        await interaction.response.edit_message(**kwargs)


async def deny(interaction: discord.Interaction, text: str) -> None:
    try:
        await reply(interaction, text)
    except discord.HTTPException:
        log.debug("Could not send denial message", exc_info=True)


async def report_error(interaction: discord.Interaction, error: BaseException, where: str) -> None:
    log.error("Unhandled error in %s", where, exc_info=error)
    try:
        await reply(interaction, "Something went wrong while doing that. The error was logged for the bot admins.")
    except discord.HTTPException:
        pass


def option(label: str, value: str, description: str | None = None, default: bool = False) -> discord.SelectOption:
    return discord.SelectOption(
        label=clip(label, SELECT_LABEL_LIMIT) or "-",
        value=value,
        description=clip(description, SELECT_DESCRIPTION_LIMIT) if description else None,
        default=default,
    )


class BaseView(discord.ui.View):
    def __init__(self, *, owner_id: int | None = None, timeout: float | None = PANEL_TIMEOUT):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await deny(interaction, "This panel belongs to someone else. Open your own from the board or command.")
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await report_error(interaction, error, f"{type(self).__name__} item {getattr(item, 'custom_id', item)}")


class BaseModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_error(interaction, error, f"modal {type(self).__name__}")


def text_field(
    label: str,
    *,
    default: str | None = None,
    placeholder: str | None = None,
    description: str | None = None,
    required: bool = True,
    max_length: int = 100,
    min_length: int | None = None,
    paragraph: bool = False,
) -> discord.ui.Label:
    component = discord.ui.TextInput(
        style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short,
        default=default,
        placeholder=placeholder,
        required=required,
        max_length=max_length,
        min_length=min_length,
    )
    return discord.ui.Label(text=label, description=description, component=component)


def select_field(
    label: str,
    choices: Sequence[str],
    *,
    default: str | None = None,
    description: str | None = None,
    placeholder: str | None = None,
    required: bool = True,
) -> discord.ui.Label:
    values = list(dict.fromkeys(choices))[:25]
    if default is not None and default not in values:
        values = [default, *values][:25]
    component = discord.ui.Select(
        placeholder=placeholder,
        options=[option(value, value, default=value == default) for value in values],
        min_values=1 if required else 0,
        max_values=1,
        required=required,
    )
    return discord.ui.Label(text=label, description=description, component=component)


def text_value(label: discord.ui.Label) -> str:
    return str(label.component.value or "").strip()


def select_value(label: discord.ui.Label) -> str | None:
    values = label.component.values
    return str(values[0]) if values else None


@dataclass
class PickOption:
    value: str
    label: str
    description: str | None = None


class PickerView(BaseView):
    def __init__(
        self,
        options: list[PickOption],
        on_pick: Callable[[discord.Interaction, str], Awaitable[Any]],
        *,
        owner_id: int,
        placeholder: str = "Select an entry...",
        page_size: int = 25,
        extra_buttons: Sequence[discord.ui.Button] = (),
    ):
        super().__init__(owner_id=owner_id)
        self.options = options
        self.on_pick = on_pick
        self.page_size = page_size
        self.page = 0
        self.placeholder = placeholder
        self.extra_buttons = list(extra_buttons)
        self._build()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.options) + self.page_size - 1) // self.page_size)

    def _build(self) -> None:
        self.clear_items()
        start = self.page * self.page_size
        chunk = self.options[start : start + self.page_size]
        if chunk:
            select = discord.ui.Select(
                placeholder=clip(self.placeholder, 150),
                options=[option(o.label, o.value, o.description) for o in chunk],
                row=0,
            )
            select.callback = self._picked
            self.add_item(select)
        if self.page_count > 1:
            prev_button = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, row=1, disabled=self.page == 0)
            prev_button.callback = self._previous
            self.add_item(prev_button)
            indicator = discord.ui.Button(
                label=f"Page {self.page + 1}/{self.page_count}", style=discord.ButtonStyle.secondary, row=1, disabled=True
            )
            self.add_item(indicator)
            next_button = discord.ui.Button(
                label="Next", style=discord.ButtonStyle.secondary, row=1, disabled=self.page >= self.page_count - 1
            )
            next_button.callback = self._next
            self.add_item(next_button)
        for button in self.extra_buttons:
            button.row = 2
            self.add_item(button)

    async def _picked(self, interaction: discord.Interaction) -> None:
        select = next(item for item in self.children if isinstance(item, discord.ui.Select))
        await self.on_pick(interaction, select.values[0])

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._build()
        await interaction.response.edit_message(view=self)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page = min(self.page_count - 1, self.page + 1)
        self._build()
        await interaction.response.edit_message(view=self)


class EmbedPaginator(BaseView):
    def __init__(self, embeds: list[discord.Embed], *, owner_id: int):
        super().__init__(owner_id=owner_id)
        self.embeds = embeds
        self.page = 0
        self._sync()

    def _sync(self) -> None:
        last = len(self.embeds) - 1
        self.first_page.disabled = self.page == 0
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= last
        self.last_page.disabled = self.page >= last

    async def start(self, interaction: discord.Interaction) -> None:
        if len(self.embeds) == 1:
            await reply(interaction, embed=self.embeds[0])
        else:
            await reply(interaction, embed=self.embeds[0], view=self)

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self.embeds[self.page], view=self)

    @discord.ui.button(label="First", style=discord.ButtonStyle.secondary)
    async def first_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = 0
        await self._show(interaction)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        await self._show(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(len(self.embeds) - 1, self.page + 1)
        await self._show(interaction)

    @discord.ui.button(label="Last", style=discord.ButtonStyle.secondary)
    async def last_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = len(self.embeds) - 1
        await self._show(interaction)


class ConfirmView(BaseView):
    def __init__(
        self,
        *,
        owner_id: int,
        on_confirm: Callback,
        on_cancel: Callback | None = None,
        confirm_label: str = "Confirm",
        danger: bool = True,
    ):
        super().__init__(owner_id=owner_id)
        self._on_confirm = on_confirm
        self._on_cancel = on_cancel
        confirm = discord.ui.Button(
            label=confirm_label,
            style=discord.ButtonStyle.danger if danger else discord.ButtonStyle.success,
        )
        confirm.callback = self._confirm
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        self.stop()
        await self._on_confirm(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        if self._on_cancel is not None:
            await self._on_cancel(interaction)
        else:
            await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)


def notice(text: str, colour: int = Colour.INFO, title: str | None = None) -> discord.Embed:
    return discord.Embed(title=title, description=text, colour=colour)
