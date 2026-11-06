from pfc.services.email.service import sanitize_email_html


def test_email_html_sanitizer_keeps_safe_formatting_and_removes_scripts() -> None:
    result = sanitize_email_html(
        "<p>Safe <strong>text</strong></p>"
        '<script>alert("unsafe")</script>'
        '<a href="javascript:alert(1)">unsafe link</a>'
    )

    assert "<p>Safe <strong>text</strong></p>" in result
    assert "<script>" not in result
    assert "alert" not in result
    assert 'href="javascript:' not in result
