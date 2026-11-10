from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class Nav(CallbackData, prefix="nav"):
    to: str  # main | accounts | add | delete | stats | settings | notif | history | help


class Acc(CallbackData, prefix="acc"):
    action: str  # list | view | pause | resume | mute | del_list | del_ask | del_yes
    id: int = 0
    page: int = 0


class Notif(CallbackData, prefix="ntf"):
    action: str  # menu | all | type | accounts | acc | acc_all
    value: str = ""
    page: int = 0


class Sett(CallbackData, prefix="set"):
    action: str
    value: str = ""


class Hist(CallbackData, prefix="hst"):
    action: str
    value: str = ""
    page: int = 0


class Stat(CallbackData, prefix="st"):
    period: str = "day"


class Noop(CallbackData, prefix="noop"):
    pass
