"""Explicit router order: commands and payments precede state input; fallback last."""

from aiogram import Router

from app.features import (
    admin,
    audio,
    audio_utility,
    channels,
    common,
    conditions,
    contests,
    downloads,
    editors,
    fallback,
    multipost,
    payments,
    posts,
    preferences,
    published_editor,
    referrals,
    sources,
    support,
    user_admin,
)
from app.middleware import Guard


def build_router():
    root = Router(name="application")
    root.message.outer_middleware(Guard())
    root.callback_query.outer_middleware(Guard())
    root.include_routers(
        common.router,
        support.router,
        preferences.router,
        multipost.router,
        contests.router,
        payments.router,
        conditions.router,
        channels.router,
        audio.router,
        audio_utility.router,
        posts.router,
        published_editor.router,
        sources.router,
        admin.router,
        user_admin.router,
        referrals.router,
        editors.router,
        downloads.router,
        fallback.router,
    )
    return root
