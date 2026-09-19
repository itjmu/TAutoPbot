"""features / referrals components."""

import html

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from app import accounts as accounts
from app import database as database
from app import timeutils as timeutils
from app import ui as ui
from app.i18n import tr
from app.states import PromoRedeem

router = Router(name="features.referrals")


@router.callback_query(F.data == "ref:open")
@router.callback_query(F.data == "ref:check")
async def referral_menu(c: CallbackQuery, bot: Bot):
    uid = c.from_user.id
    if c.data == "ref:check":
        await accounts.check_referral(uid, bot)
    me = await bot.get_me()
    r = database.one(
        "SELECT COUNT(*) n,SUM(rewarded_at IS NOT NULL) paid FROM referrals WHERE inviter_id=?",
        (uid,),
    )
    text = tr(
        "👥 Ваша ссылка:\nhttps://t.me/{v0}?start=ref_{v1}\n\nНаграда: {v2} дн. пригласившему и {v3} дн. приглашённому.\nУсловие: {v4}.\nПриглашено: {v5}; награждено: {v6}.",
        v0=me.username,
        v1=uid,
        v2=database.setting("ref_inviter_days"),
        v3=database.setting("ref_invitee_days"),
        v4=accounts.CONDITION_LABELS.get(
            database.setting("ref_condition"), tr("не задано")
        ),
        v5=r["n"],
        v6=r["paid"] or 0,
    )
    if database.setting("ref_condition") == "subscription":
        text += tr("\nКаналы: ") + html.escape(database.setting("ref_subscription"))
    await ui.edit(
        c,
        text,
        ui.kb(
            [
                [
                    InlineKeyboardButton(
                        text=tr("🔄 Проверить моё условие"), callback_data="ref:check"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=tr("⬅️ Настройки"), callback_data="menu:settings"
                    )
                ],
            ]
        ),
    )
    await c.answer()


@router.callback_query(F.data == "promo:redeem")
async def promo_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(PromoRedeem.value)
    await ui.edit(c, tr("Введите промокод."), ui.back("menu:settings"))
    await c.answer()


async def redeem_promo(uid, code, bot):
    code = code.strip().upper()
    promo = database.one("SELECT * FROM promos WHERE code=?", (code,))
    if not promo or not promo["active"]:
        raise ValueError(tr("Промокод недоступен."))
    if not await accounts.reward_condition(uid, promo["condition"], bot):
        raise ValueError(
            tr("Сначала выполните условие: ")
            + accounts.CONDITION_LABELS[promo["condition"]]
        )
    with database.atomic():
        promo = database.one("SELECT * FROM promos WHERE code=?", (code,))
        if not promo or not promo["active"]:
            raise ValueError(tr("Промокод недоступен."))
        if (
            promo["expires_at"]
            and timeutils.parse_dt(promo["expires_at"]) <= timeutils.now()
        ):
            raise ValueError(tr("Срок промокода истёк."))
        if promo["personal_id"] and promo["personal_id"] != uid:
            raise ValueError(tr("Промокод предназначен другому пользователю."))
        if database.one(
            "SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, uid)
        ):
            raise ValueError(tr("Вы уже использовали этот код."))
        used = database.one("SELECT COUNT(*) n FROM promo_uses WHERE code=?", (code,))[
            "n"
        ]
        if used >= promo["max_uses"]:
            raise ValueError(tr("Активации закончились."))
        database.db.execute(
            "INSERT INTO promo_uses VALUES(?,?,?)", (code, uid, timeutils.iso())
        )
        accounts.premium_change_tx(uid, "add", promo["days"], "promo:" + code)
    return promo["days"]


@router.message(PromoRedeem.value, F.text)
async def promo_value(m: Message, state: FSMContext, bot: Bot):
    days = await redeem_promo(m.from_user.id, m.text, bot)
    await state.clear()
    await m.answer(
        tr("🎟 Добавлено {v0} дней Premium.", v0=days),
        reply_markup=ui.settings_kb(m.from_user.id),
    )
