from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AddAccount(StatesGroup):
    waiting_input = State()


class HistoryInput(StatesGroup):
    search = State()
    dates = State()


class SettingsInput(StatesGroup):
    timezone = State()
