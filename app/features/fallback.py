"""features / fallback components."""

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.types import Message

from app import ui as ui
from app.features import common as features_common
from app.i18n import tr
from app.states import PostCreate

router = Router(name="features.fallback")


@router.message(StateFilter(None), ~F.successful_payment, ~F.text.startswith("/"))
async def idle_message(m: Message, bot: Bot):
    await features_common.offer_incoming(m, bot)


@router.message(PostCreate.idle)
async def active_post_hint(m: Message):
    await ui.answer(
        m,
        tr("Пост открыт. Выберите действие под ним или нажмите /cancel, чтобы выйти."),
    )


@router.message(F.text.startswith("/"))
async def unknown_command(m: Message):
    await ui.answer(
        m,
        tr("Команда не найдена. Используйте /start или /cancel."),
        reply_markup=ui.main_kb(),
    )
