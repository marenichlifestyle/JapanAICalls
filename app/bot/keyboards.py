from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def call_confirm_keyboard(job_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Прозвонить", callback_data=f"call:{job_id}")
    kb.button(text="Отмена", callback_data=f"cancel:{job_id}")
    kb.adjust(2)
    return kb.as_markup()


def start_mode_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Прозвонить по ссылке", callback_data="mode:link")
    kb.button(text="Прозвонить по запросу", callback_data="mode:request")
    kb.adjust(1)
    return kb.as_markup()


def call_language_keyboard(job_id: int, source: str | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if (source or "").lower() == "cars.com":
        kb.button(text="English", callback_data=f"lang:en:{job_id}")
        kb.adjust(1)
    else:
        kb.button(text="Русский", callback_data=f"lang:ru:{job_id}")
        kb.button(text="Японский", callback_data=f"lang:ja:{job_id}")
        kb.adjust(2)
    return kb.as_markup()


def phone_review_keyboard(job_id: int, candidates_count: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    safe_count = max(0, min(candidates_count, 5))
    for idx in range(safe_count):
        kb.button(text=f"Подтвердить #{idx + 1}", callback_data=f"phone_review:approve:{job_id}:{idx}")
    kb.button(text="Отклонить", callback_data=f"phone_review:reject:{job_id}")
    if safe_count <= 2:
        kb.adjust(1)
    else:
        kb.adjust(2, 2, 1)
    return kb.as_markup()


def request_call_confirm_keyboard(campaign_id: int, count: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Запустить прозвон {count} номеров", callback_data=f"request:start:{campaign_id}")
    kb.button(text="Изменить цель", callback_data=f"request:change_goal:{campaign_id}")
    kb.button(text="Отмена", callback_data=f"request:cancel:{campaign_id}")
    kb.adjust(1)
    return kb.as_markup()


def request_call_language_keyboard(campaign_id: int, recommended: str | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    en_label = "English"
    ja_label = "日本語"
    if recommended == "en":
        en_label += " (рекомендовано)"
    elif recommended == "ja":
        ja_label += " (рекомендовано)"
    kb.button(text=en_label, callback_data=f"request:lang:en:{campaign_id}")
    kb.button(text=ja_label, callback_data=f"request:lang:ja:{campaign_id}")
    kb.adjust(1)
    return kb.as_markup()


def request_call_next_keyboard(campaign_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Прозвонить следующего", callback_data=f"request:next:{campaign_id}")
    kb.button(text="Остановить", callback_data=f"request:stop:{campaign_id}")
    kb.adjust(1)
    return kb.as_markup()
