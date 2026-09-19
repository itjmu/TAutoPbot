"""Language and timezone settings."""

from aiogram import F, Router
from aiogram.fsm.state import State, StatesGroup

from app import preferences, ui
from app.i18n import tr

router = Router(name="preferences")


class PreferenceInput(StatesGroup):
    timezone = State()


@router.callback_query(F.data == "settings:timezone")
async def timezone_menu(c, state):
    current = preferences.get_preferences(c.from_user.id)["timezone"]
    rows = [
        [ui.choice(label, "prefs:tz:" + name)]
        for label, name in [
            (tr("Алматы / Астана — UTC+5"), "Asia/Almaty"),
            (tr("Москва — UTC+3"), "Europe/Moscow"),
            (tr("Карачи — UTC+5"), "Asia/Karachi"),
            (tr("Ташкент — UTC+5"), "Asia/Tashkent"),
            ("UTC", "UTC"),
        ]
    ]
    rows += [
        [ui.choice(tr("Другой часовой пояс"), "prefs:tz:custom")],
        [ui.choice(tr("Назад"), "menu:settings")],
    ]
    await ui.edit(
        c,
        tr("Ваш часовой пояс: ")
        + current
        + tr("\nСейчас: ")
        + preferences.display(c.from_user.id, preferences.local_now(c.from_user.id)),
        ui.kb(rows),
    )
    await c.answer()


@router.callback_query(F.data.startswith("prefs:tz:"))
async def timezone_choice(c, state):
    value = c.data[len("prefs:tz:") :]
    if value == "custom":
        await state.set_state(PreferenceInput.timezone)
        await ui.edit(
            c,
            tr("Введите город в формате Asia/Almaty или смещение UTC+05:00."),
            ui.back("settings:timezone"),
        )
    else:
        preferences.save(c.from_user.id, timezone=value)
        await ui.edit(
            c,
            tr(
                "Часовой пояс сохранён. Уже запланированные публикации сохраняют своё точное время."
            ),
            ui.back("menu:settings"),
        )
    await c.answer()


@router.message(PreferenceInput.timezone, F.text, ~F.text.startswith("/"))
async def timezone_text(m, state):
    preferences.save(m.from_user.id, timezone=m.text.strip())
    await state.clear()
    await m.answer(tr("Часовой пояс сохранён."), reply_markup=ui.back("menu:settings"))


@router.callback_query(F.data == "settings:language")
async def language_menu(c):
    await ui.edit(
        c,
        "Язык / Language / Тіл",
        ui.kb(
            [
                [
                    ui.choice("Русский", "prefs:lang:ru"),
                    ui.choice("English", "prefs:lang:en"),
                    ui.choice("Қазақша", "prefs:lang:kk"),
                ],
                [ui.choice(tr("Назад"), "menu:settings")],
            ]
        ),
    )
    await c.answer()


@router.callback_query(F.data.startswith("prefs:lang:"))
async def language_choice(c):
    language = c.data.split(":")[2]
    preferences.save(c.from_user.id, language=language)
    from app.i18n import language_context

    language_context.set(language)
    await ui.edit(
        c,
        {
            "ru": tr("Язык сохранён."),
            "en": "Language saved.",
            "kk": tr("Тіл сақталды."),
        }[language],
        ui.settings_kb(c.from_user.id),
    )
    await c.answer()
