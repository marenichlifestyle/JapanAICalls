from app.bot.keyboards import (
    call_confirm_keyboard,
    call_language_keyboard,
    phone_review_keyboard,
    request_call_confirm_keyboard,
    request_call_language_keyboard,
)


def _flatten_callbacks(markup) -> set[str]:
    values: set[str] = set()
    for row in markup.inline_keyboard:
        for btn in row:
            if btn.callback_data:
                values.add(btn.callback_data)
    return values


def test_call_confirm_keyboard_contains_actions() -> None:
    markup = call_confirm_keyboard(42)
    callbacks = _flatten_callbacks(markup)
    assert "call:42" in callbacks
    assert "cancel:42" in callbacks


def test_call_language_keyboard_contains_languages() -> None:
    markup = call_language_keyboard(99)
    callbacks = _flatten_callbacks(markup)
    assert "lang:ru:99" in callbacks
    assert "lang:ja:99" in callbacks


def test_call_language_keyboard_cars_com_english_only() -> None:
    markup = call_language_keyboard(100, source="cars.com")
    callbacks = _flatten_callbacks(markup)
    assert callbacks == {"lang:en:100"}


def test_phone_review_keyboard_contains_actions() -> None:
    markup = phone_review_keyboard(7, 3)
    callbacks = _flatten_callbacks(markup)
    assert "phone_review:approve:7:0" in callbacks
    assert "phone_review:approve:7:1" in callbacks
    assert "phone_review:approve:7:2" in callbacks
    assert "phone_review:reject:7" in callbacks


def test_request_call_language_keyboard_contains_en_and_ja_callbacks() -> None:
    markup = request_call_language_keyboard(55, recommended="ja")
    callbacks = _flatten_callbacks(markup)
    assert callbacks == {"request:lang:en:55", "request:lang:ja:55"}
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert any("日本語" in label and "рекомендовано" in label for label in labels)


def test_request_call_confirm_keyboard_contains_auto_and_manual_modes() -> None:
    markup = request_call_confirm_keyboard(77, 5)
    callbacks = _flatten_callbacks(markup)
    assert "request:start:auto:77" in callbacks
    assert "request:start:manual:77" in callbacks
    assert "request:change_goal:77" in callbacks
    assert "request:cancel:77" in callbacks
