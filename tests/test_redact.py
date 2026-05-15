"""PII redactor tests."""

from shed.redact import line_has_pii, redact


def test_email_outside_whitelist_is_redacted():
    assert line_has_pii("contact me at someone@evil.example.com")


def test_whitelisted_email_kept():
    assert not line_has_pii("user is tarunsp23@gmail.com")
    assert not line_has_pii("eng team is foo@anthropic.com")


def test_phone_redacted():
    assert line_has_pii("call (415) 555-1212")
    assert line_has_pii("+1 415 555 1212")
    assert line_has_pii("415-555-1212")


def test_ssn_redacted():
    assert line_has_pii("ssn 123-45-6789")


def test_api_keys_redacted():
    assert line_has_pii("token sk-abcdefghij1234567890XYZ")
    assert line_has_pii("github ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1234")
    assert line_has_pii("aws AKIAABCDEFGHIJKLMNOP")


def test_secret_assignment_redacted():
    assert line_has_pii('password = "hunter2"')
    assert line_has_pii("API_KEY: foo")


def test_credit_card_with_luhn_redacted():
    # 4242 4242 4242 4242 — Stripe test card, valid Luhn
    assert line_has_pii("card 4242 4242 4242 4242")


def test_random_long_digits_kept_unless_luhn():
    # 12 random digits, not a valid card.
    assert not line_has_pii("transaction 1234567890987654321")


def test_redact_replaces_lines():
    text = "safe line\ncall 415-555-1212\nanother safe line\n"
    out = redact(text)
    assert "[shed: redacted]" in out
    assert "safe line" in out
    assert "415-555-1212" not in out
