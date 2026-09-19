"""features / payments components."""

import html
import os
import secrets

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from app import access as access
from app import accounts as accounts
from app import database as database
from app import plans
from app import timeutils as timeutils
from app import ui as ui
from app.i18n import tr
from app.states import PaymentInput
from config import ADMIN_ID, STARS_7_DAYS, STARS_30_DAYS

router = Router(name="features.payments")


def stars_price(days):
    return int(
        database.setting(f"stars_{days}")
        or (STARS_7_DAYS if days == 7 else STARS_30_DAYS)
    )


PAYMENT_FIELDS = {
    "stars_7": "Цена за 7 дней (Stars)",
    "stars_30": "Цена за 30 дней (Stars)",
    "manual_label": "Название другого способа",
    "manual_instructions": "Инструкция и стоимость",
    "manual_days": "Срок Premium в днях",
}


@router.callback_query(F.data == "payment:settings")
async def payment_settings(c: CallbackQuery):
    if not access.admin_only(c):
        raise ValueError(tr("Нет доступа."))
    rows = [
        [ui.choice(label, "payment:field:" + key)]
        for key, label in PAYMENT_FIELDS.items()
    ]
    rows += [
        [
            ui.choice(
                tr("Другой способ: ")
                + (
                    tr("ВКЛ")
                    if database.setting("manual_enabled") == "1"
                    else tr("ВЫКЛ")
                ),
                "payment:toggle",
            )
        ],
        [
            ui.choice(tr("Заявки на проверку"), "payment:pending"),
            ui.choice(tr("⬅️ Админ-панель"), "admin:main"),
        ],
    ]
    await ui.edit(
        c,
        tr(
            "💳 Premium бота\n7 дней: {v0} ⭐\n30 дней: {v1} ⭐\nДругой способ требует ручного подтверждения администратором.",
            v0=stars_price(7),
            v1=stars_price(30),
        ),
        ui.kb(rows),
    )
    await c.answer()


@router.callback_query(F.data.startswith("payment:field:"))
async def payment_field(c: CallbackQuery, state: FSMContext):
    if not access.admin_only(c):
        raise ValueError(tr("Нет доступа."))
    key = c.data.split(":")[2]
    if key not in PAYMENT_FIELDS:
        raise ValueError(tr("Неизвестная настройка."))
    await state.set_state(PaymentInput.setting_value)
    await state.set_data({"payment_key": key})
    await ui.edit(
        c,
        PAYMENT_FIELDS[key]
        + tr("\nТекущее значение: ")
        + html.escape(database.setting(key) or tr("не задано"))
        + tr("\nОтправьте новое значение."),
        ui.back("payment:settings"),
    )
    await c.answer()


@router.message(PaymentInput.setting_value, F.text, ~F.text.startswith("/"))
async def payment_setting_value(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        raise ValueError(tr("Нет доступа."))
    key = (await state.get_data())["payment_key"]
    value = m.text.strip()
    if key not in PAYMENT_FIELDS:
        raise ValueError(tr("Неизвестная настройка."))
    if key.startswith("stars_") or key == "manual_days":
        if not value.isdigit() or not 1 <= int(value) <= (
            36500 if key == "manual_days" else 100000
        ):
            raise ValueError(tr("Введите положительное число в допустимом диапазоне."))
    elif not 1 <= len(value) <= (50 if key == "manual_label" else 2000):
        raise ValueError(tr("Слишком длинное или пустое значение."))
    database.execute(
        "INSERT INTO app_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    await state.clear()
    await m.answer(tr("✅ Сохранено."), reply_markup=ui.back("payment:settings"))


@router.callback_query(F.data == "payment:toggle")
async def payment_toggle(c: CallbackQuery):
    if not access.admin_only(c):
        raise ValueError(tr("Нет доступа."))
    enabled = database.setting("manual_enabled") != "1"
    if enabled and not all(
        database.setting(k)
        for k in ("manual_label", "manual_instructions", "manual_days")
    ):
        raise ValueError(tr("Сначала задайте название, инструкцию и срок."))
    database.execute(
        "INSERT INTO app_settings VALUES('manual_enabled',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("1" if enabled else "0",),
    )
    await payment_settings(c)


@router.callback_query(F.data == "premium:manual")
async def manual_payment_start(c: CallbackQuery, state: FSMContext):
    if database.setting("manual_enabled") != "1":
        raise ValueError(tr("Способ отключён."))
    if database.one(
        "SELECT 1 FROM manual_payments WHERE telegram_id=? AND status='pending'",
        (c.from_user.id,),
    ):
        raise ValueError(tr("Ваша заявка уже ожидает проверки."))
    await state.set_state(PaymentInput.reference)
    await state.set_data(
        {
            "days": int(database.setting("manual_days")),
            "method": database.setting("manual_label"),
        }
    )
    await ui.edit(
        c,
        html.escape(database.setting("manual_instructions"))
        + tr(
            "\n\nОтправьте номер операции / текст подтверждения. Premium будет выдан после проверки администратором."
        ),
        ui.back("settings:premium"),
    )
    await c.answer()


@router.message(PaymentInput.reference, F.text, ~F.text.startswith("/"))
async def manual_payment_reference(m: Message, state: FSMContext):
    if database.setting("manual_enabled") != "1":
        raise ValueError(tr("Способ отключён."))
    reference = m.text.strip()
    if not 3 <= len(reference) <= 1000:
        raise ValueError(tr("Подтверждение: 3–1000 символов."))
    data = await state.get_data()
    with database.atomic():
        if database.one(
            "SELECT 1 FROM manual_payments WHERE telegram_id=? AND status='pending'",
            (m.from_user.id,),
        ):
            raise ValueError(tr("Заявка уже ожидает проверки."))
        database.execute(
            "INSERT INTO manual_payments(telegram_id,days,reference,method,created_at) VALUES(?,?,?,?,?)",
            (m.from_user.id, data["days"], reference, data["method"], timeutils.iso()),
        )
    await state.clear()
    await m.answer(
        tr("✅ Заявка сохранена и доступна администратору для проверки."),
        reply_markup=ui.back("settings:premium"),
    )


@router.callback_query(F.data == "payment:pending")
async def manual_payment_pending(c: CallbackQuery):
    if not access.admin_only(c):
        raise ValueError(tr("Нет доступа."))
    row = database.one(
        "SELECT * FROM manual_payments WHERE status='pending' ORDER BY id LIMIT 1"
    )
    if not row:
        await ui.edit(c, tr("Нет ожидающих заявок."), ui.back("payment:settings"))
        await c.answer()
        return
    await ui.edit(
        c,
        tr(
            "Заявка #{v0}\nПользователь: {v1}\nСрок: {v2} дней\n",
            v0=row["id"],
            v1=row["telegram_id"],
            v2=row["days"],
        )
        + html.escape(row["method"] + "\n" + row["reference"]),
        ui.kb(
            [
                [
                    ui.choice(
                        tr("✅ Подтвердить"), f"payment:review:{row['id']}:approve"
                    ),
                    ui.choice(tr("❌ Отклонить"), f"payment:review:{row['id']}:reject"),
                ],
                [ui.choice(tr("⬅️ Оплата"), "payment:settings")],
            ]
        ),
    )
    await c.answer()


@router.callback_query(F.data.startswith("payment:review:"))
async def manual_payment_review(c: CallbackQuery):
    if not access.admin_only(c):
        raise ValueError(tr("Нет доступа."))
    _, _, pid, action = c.data.split(":")
    if action not in {"approve", "reject"}:
        raise ValueError(tr("Неизвестное действие."))
    with database.atomic():
        row = database.one(
            "SELECT * FROM manual_payments WHERE id=? AND status='pending'", (int(pid),)
        )
        if not row:
            raise ValueError(tr("Заявка уже обработана."))
        if action == "approve":
            accounts.premium_change_tx(
                row["telegram_id"],
                "add",
                row["days"],
                "manual_payment:" + pid,
                ADMIN_ID,
            )
        database.db.execute(
            "UPDATE manual_payments SET status=?,reviewed_at=? WHERE id=?",
            ("paid" if action == "approve" else "rejected", timeutils.iso(), int(pid)),
        )
    await ui.edit(c, tr("✅ Решение сохранено."), ui.back("payment:pending"))
    await c.answer()


@router.callback_query(F.data == "settings:premium")
async def premium_menu(c: CallbackQuery):
    rows = [
        [
            InlineKeyboardButton(
                text=tr("⭐ 7 дней — {v0}", v0=STARS_7_DAYS),
                callback_data="premium:buy:7",
            )
        ],
        [
            InlineKeyboardButton(
                text=tr("⭐ 30 дней — {v0}", v0=STARS_30_DAYS),
                callback_data="premium:buy:30",
            )
        ],
        [InlineKeyboardButton(text=tr("🎟 Промокод"), callback_data="promo:redeem")],
        [InlineKeyboardButton(text=tr("⬅️ Настройки"), callback_data="menu:settings")],
    ]
    rows[0][0].text = tr("⭐ 7 дней — {v0}", v0=stars_price(7))
    rows[1][0].text = tr("⭐ 30 дней — {v0}", v0=stars_price(30))
    if database.setting("manual_enabled") == "1" and database.setting(
        "manual_instructions"
    ):
        rows.insert(
            2,
            [
                ui.choice(
                    database.setting("manual_label") or tr("Другой способ"),
                    "premium:manual",
                )
            ],
        )
    await ui.edit(
        c,
        plans.description(),
        ui.kb(rows),
    )
    await c.answer()


@router.callback_query(F.data == "premium:trial")
async def premium_trial(c: CallbackQuery):
    if not await access.access_callback(c):
        return
    await c.answer(tr("Пробный Premium отключён."), show_alert=True)


@router.callback_query(F.data.startswith("premium:buy:"))
async def premium_buy(c: CallbackQuery, bot: Bot):
    days = int(c.data.split(":")[2])
    if days not in {7, 30}:
        raise ValueError(tr("Недопустимый тариф."))
    amount = stars_price(days)
    payload = "premium:" + secrets.token_urlsafe(20)
    database.execute(
        "INSERT INTO payments(telegram_id,days,amount,currency,payload,created_at) VALUES(?,?,?,?,?,?)",
        (c.from_user.id, days, amount, "XTR", payload, timeutils.iso()),
    )
    await bot.send_invoice(
        c.from_user.id,
        title=tr("Premium {v0} дней", v0=days),
        description=tr("Продление Premium на {v0} дней", v0=days),
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label="Premium", amount=amount)],
        provider_token="",
    )
    await c.answer()


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery, bot: Bot):
    p = database.one(
        "SELECT * FROM payments WHERE payload=? AND telegram_id=? AND status='pending'",
        (q.invoice_payload, q.from_user.id),
    )
    if not p or q.currency != "XTR" or q.total_amount != p["amount"]:
        await q.answer(ok=False, error_message=tr("Заказ недействителен."))
        return
    await q.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(m: Message, bot: Bot):
    p = m.successful_payment
    accounts.ensure_user(m.from_user)
    with database.atomic():
        row = database.one(
            "SELECT * FROM payments WHERE payload=? AND telegram_id=? AND status='pending'",
            (p.invoice_payload, m.from_user.id),
        )
        if not row or p.currency != row["currency"] or p.total_amount != row["amount"]:
            return
        if database.one(
            "SELECT 1 FROM payments WHERE telegram_charge_id=?",
            (p.telegram_payment_charge_id,),
        ):
            return
        database.db.execute(
            "UPDATE payments SET status='paid',telegram_charge_id=?,paid_at=? WHERE id=?",
            (p.telegram_payment_charge_id, timeutils.iso(), row["id"]),
        )
        accounts.premium_change_tx(m.from_user.id, "add", row["days"], "stars")
    await accounts.check_referral(m.from_user.id, bot)
    await m.answer(
        tr("✅ Premium продлён на {v0} дней.", v0=row["days"]),
        reply_markup=ui.main_kb(),
    )


@router.message(Command("paysupport"))
async def payment_support(m: Message):
    contact = os.getenv("SUPPORT_CONTACT", "").strip()
    await m.answer(
        tr("Поддержка платежей: ")
        + html.escape(contact or tr("администратор Telegram ID {v0}", v0=ADMIN_ID))
        + tr(". Укажите ваш ID и дату платежа.")
    )
