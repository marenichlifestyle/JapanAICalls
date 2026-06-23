from __future__ import annotations

import pytest
from aiogram.types import MenuButtonCommands

from app.bot.runner import configure_bot_commands


class FakeBot:
    def __init__(self) -> None:
        self.commands: list[dict] = []
        self.menu_button = None

    async def set_my_commands(self, commands, scope=None):
        self.commands.append({"commands": commands, "scope": scope})

    async def set_chat_menu_button(self, menu_button=None, **kwargs):
        self.menu_button = menu_button


@pytest.mark.asyncio
async def test_configure_bot_commands_sets_request_and_menu_button() -> None:
    bot = FakeBot()

    await configure_bot_commands(bot)

    assert len(bot.commands) == 3
    command_names = {command.command for command in bot.commands[0]["commands"]}
    assert command_names == {"start", "request", "cancel"}
    assert isinstance(bot.menu_button, MenuButtonCommands)
    assert bot.menu_button.type == "commands"
