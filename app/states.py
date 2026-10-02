"""states components."""

from aiogram.fsm.state import State, StatesGroup


class AddChannel(StatesGroup):
    waiting = State()


class PostCreate(StatesGroup):
    topic = State()
    content = State()
    schedule = State()
    delete = State()
    pin = State()
    edit_content = State()
    cover = State()
    edit_text = State()
    buttons = State()
    idle = State()


class ConditionCreate(StatesGroup):
    name = State()
    item = State()
    question = State()


class SourceCreate(StatesGroup):
    source = State()


class Broadcast(StatesGroup):
    content = State()


class JoinChallenge(StatesGroup):
    answer = State()


class TemplateEdit(StatesGroup):
    value = State()


class PremiumAdmin(StatesGroup):
    value = State()


class PromoRedeem(StatesGroup):
    value = State()


class PaymentInput(StatesGroup):
    setting_value = State()
    reference = State()


class GuidedInput(StatesGroup):
    value = State()


class DownloadInput(StatesGroup):
    url = State()
