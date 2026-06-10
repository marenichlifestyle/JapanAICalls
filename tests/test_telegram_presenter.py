from app.models import Job
from app.services.telegram_presenter import (
    build_final_report_html,
    build_transcript_expandable_html,
    truncate_for_telegram,
)


def test_final_report_html_is_escaped() -> None:
    job = Job(
        listing_url="https://example.com/?a=<b>",
        car_full='BMW <M5> & "Competition"',
        car_short="BMW M5",
        extracted_phone="+81123456789",
        call_phone="+33768013446",
        status="completed",
        call_status="done",
        price_used_jpy=7_138_000,
        price_used_type="total_price",
        analysis_price_confirmed=True,
        analysis_actual_price="7138000",
        analysis_price_change_reason='No changes & <none>',
        analysis_condition_notes='Great "shape"',
        analysis_conclusion="<ok>",
        analysis_ai_quality_score=87,
        analysis_ai_quality_reason="агент уточнил цену & VIN",
    )

    html = build_final_report_html(job)
    assert "<b>Финальный отчёт</b>" in html
    assert "Язык звонка" in html
    assert "&lt;M5&gt;" in html
    assert "&amp;" in html
    assert "Оценка AI" in html
    assert "87/100" in html
    assert "агент уточнил цену &amp; VIN" in html
    assert "<script>" not in html


def test_final_report_hides_ai_quality_when_score_missing() -> None:
    html = build_final_report_html(Job(listing_url="https://example.com", status="completed"))
    assert "Оценка AI" not in html


def test_transcript_expandable_blockquote() -> None:
    html, file_text = build_transcript_expandable_html("agent: привет\nuser: да")
    assert "<blockquote expandable>" in html
    assert "</blockquote>" in html
    assert file_text is None


def test_transcript_overflow_returns_file_text() -> None:
    long_text = "очень длинный текст " * 500
    html, file_text = build_transcript_expandable_html(long_text)
    assert "<blockquote expandable>" in html
    assert file_text is not None
    assert len(file_text) > len(html)


def test_truncate_for_telegram() -> None:
    assert truncate_for_telegram("abc", 10) == "abc"
    assert truncate_for_telegram("abcdef", 4) == "abc…"
