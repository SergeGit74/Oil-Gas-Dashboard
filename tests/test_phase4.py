import email
import json
import smtplib
from datetime import datetime, timedelta, timezone

import pytest

import alerts
import brief_email
import config
import high_impact_alerts as hia
import storage
from jobs import run_evaluation, send_brief_email

CYCLE = "2026-10-26T12:00:00+00:00"
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
HOOK = "https://hooks.example.test/T000/B000/XXX"


def art(url, title="Ukraine strikes Russian refinery, oil jumps", source="reuters.com", **extra):
    """A verified, high-impact article row with overridable fields."""
    base = {"url": url, "title": title, "source": source, "snippet": "SNIPPET-TEXT", "collected_at": CYCLE,
            "direction": "Bullish", "magnitude": 4, "confidence": 0.85, "content_type": "news",
            "flagged_injection": 0, "category": "Supply"}
    return {**base, **extra}


def save(rows, clusters=None):
    """Store articles and optionally assign their clusters; returns {url: id}."""
    storage.save_articles(rows)
    ids = {r["url"]: r["id"] for r in storage._rows("SELECT id, url FROM articles")}
    storage.set_clusters({ids[u]: c for u, c in (clusters or {}).items()})
    return ids


@pytest.fixture
def slack(monkeypatch):
    """Configure a webhook and return (messages sent, a fake send function)."""
    monkeypatch.setattr(config, "SLACK_WEBHOOK_URL", HOOK)
    sent = []
    return sent, (lambda text: sent.append(text) or True)


# ---- Slack alerts ---------------------------------------------------------

def test_slack_escape_defuses_markup_and_caps_length():
    """Slack markup, control characters and long text are neutralised."""
    out = hia.slack_escape("<!channel> <@U123> <http://evil|click> A&B\r\nnext")
    assert "<" not in out and ">" not in out and "\n" not in out and "&amp;" in out
    assert len(hia.slack_escape("x" * 1000)) == hia.MAX_TITLE_CHARS


def test_only_verified_high_impact_articles_alert(db, slack):
    """Only verified, directional articles at the impact threshold alert; their snippets are never sent."""
    sent, send = slack
    save([art("ok"),
          art("injected", "Injected headline", flagged_injection=1),
          art("lowconf", "Low confidence headline", confidence=0.4),
          art("lowmag", "Low impact headline", magnitude=3),
          art("nodir", "No direction headline", direction=None)])
    counts = hia.run_alerts(CYCLE, now=NOW, send=send)
    assert counts["sent"] == 1 and len(sent) == 1
    assert "Ukraine strikes Russian refinery" in sent[0]
    assert all(t not in sent[0] for t in ("Injected", "Low confidence", "Low impact", "No direction", "SNIPPET-TEXT"))
    assert [r["status"] for r in storage.recent_alerts()] == ["sent"]


def test_one_alert_per_story_ever(db, slack):
    """A story alerts once, through its best article, and never again when it grows."""
    sent, send = slack
    save([art("a", confidence=0.80), art("b", source="bloomberg.com", confidence=0.95), art("c", source="apnews.com")],
         clusters={"a": 7, "b": 7, "c": 7})
    assert hia.run_alerts(CYCLE, now=NOW, send=send)["sent"] == 1
    assert "bloomberg.com" in sent[0]                                   # the highest-confidence member represents it
    assert "reported by 3 outlets" in sent[0]
    later = "2026-10-26T12:20:00+00:00"                                  # the story grows in a later cycle
    save([art("d", source="ft.com", collected_at=later)], clusters={"d": 7})
    assert hia.run_alerts(later, now=NOW + timedelta(minutes=20), send=send)["sent"] == 0
    assert len(sent) == 1


def test_outlet_count_ignores_unverified_members(db, slack):
    """The 'reported by' count excludes unverified articles."""
    sent, send = slack
    save([art("a"), art("junk", source="spam.example", confidence=0.2), art("inj", source="inj.example", flagged_injection=1)],
         clusters={"a": 3, "junk": 3, "inj": 3})
    hia.run_alerts(CYCLE, now=NOW, send=send)
    assert "reported by 1 outlet" in sent[0] and "spam.example" not in sent[0]


def test_hourly_cap_suppresses_least_important_and_does_not_replay(db, slack, monkeypatch):
    """Over the hourly cap the least important alerts are suppressed and not replayed later."""
    sent, send = slack
    monkeypatch.setattr(config, "ALERT_MAX_PER_HOUR", 2)
    save([art("m5", "Story five", magnitude=5), art("m4a", "Story four A"), art("m4b", "Story four B", confidence=0.7),
          art("m4c", "Story four C", confidence=0.6)])
    counts = hia.run_alerts(CYCLE, now=NOW, send=send)
    assert (counts["sent"], counts["suppressed"]) == (2, 2)
    assert "Story five" in sent[0] and "Story four A" in sent[1]       # by magnitude, then confidence
    statuses = {r["title"]: r["status"] for r in storage.recent_alerts()}
    assert statuses["Story four B"] == statuses["Story four C"] == "suppressed_rate_limit"
    assert storage.recent_job_runs("slack_alerts", 1)[0]["status"] == "success"
    # an hour and a bit later the suppressed stories are not replayed (they are not new any more)
    assert hia.run_alerts(CYCLE, now=NOW + timedelta(hours=2), send=send)["sent"] == 0 and len(sent) == 2


def test_cap_frees_up_after_an_hour(db, slack, monkeypatch):
    """Alerts are allowed again once an hour has passed."""
    sent, send = slack
    monkeypatch.setattr(config, "ALERT_MAX_PER_HOUR", 1)
    save([art("a", "First story")])
    hia.run_alerts(CYCLE, now=NOW, send=send)
    later = "2026-10-26T13:30:00+00:00"
    save([art("b", "Second story", collected_at=later)])
    assert hia.run_alerts(later, now=NOW + timedelta(minutes=90), send=send)["sent"] == 1


def test_failed_send_is_logged_and_not_retried(db, monkeypatch):
    """A failed Slack post is recorded as failed, degrades the job and is not retried."""
    monkeypatch.setattr(config, "SLACK_WEBHOOK_URL", HOOK)
    save([art("a")])
    counts = hia.run_alerts(CYCLE, now=NOW, send=lambda text: False)
    assert counts["failed"] == 1
    assert storage.recent_alerts()[0]["status"] == "failed"
    assert storage.recent_job_runs("slack_alerts", 1)[0]["status"] == "degraded"
    assert hia.run_alerts(CYCLE, now=NOW, send=lambda text: True)["sent"] == 0


def test_no_webhook_sends_nothing(db):
    """Without a webhook nothing is sent and the story is recorded as not configured."""
    save([art("a")])
    called = []
    counts = hia.run_alerts(CYCLE, now=NOW, send=lambda t: called.append(t) or True)
    assert counts["not_configured"] == 1 and called == []
    assert storage.recent_alerts()[0]["status"] == "not_configured"


def test_send_slack_posts_json_and_reports_failure(monkeypatch):
    """send_slack posts the JSON text, returns False with no webhook and False on a network error."""
    posted = []

    class Resp:
        def raise_for_status(self):
            """A response that always succeeds."""
            pass

    monkeypatch.setattr(alerts.requests, "post", lambda url, json, timeout: posted.append((url, json)) or Resp())
    assert alerts.send_slack("hi") is False                              # no webhook configured
    monkeypatch.setattr(config, "SLACK_WEBHOOK_URL", HOOK)
    assert alerts.send_slack("hi") is True and posted == [(HOOK, {"text": "hi"})]

    def boom(*a, **k):
        """Fake requests.post that raises a connection error."""
        raise alerts.requests.ConnectionError("down")

    monkeypatch.setattr(alerts.requests, "post", boom)
    assert alerts.send_slack("hi") is False


# ---- brief email ----------------------------------------------------------

BODY = "# Morning Intelligence Brief, 26 October 2026\n\n## Headline view\n\nSECRET-BRIEF-BODY <script>x()</script> [1]\n\n- point **bold**"
KP = {"prices": {"BRENT": {"price": 80.1, "change_pct": 1.2, "as_of": "2026-10-26T11:59:00+00:00", "provider": "yahoo"}}}
SOURCES = [{"n": 1, "title": "Oil jumps on refinery strikes", "source": "reuters.com", "url": "https://reuters.com/a"}]


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        """Record the host, port and timeout and start with no calls."""
        self.host, self.port, self.timeout, self.calls, self.sent = host, port, timeout, [], []
        self.starttls_offered, self.fail_with = True, None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, *a):
        """Context manager exit (does not swallow errors)."""
        return False

    def ehlo(self):
        """Record the EHLO call."""
        self.calls.append("ehlo")

    def has_extn(self, name):
        """Whether the fake server offers STARTTLS."""
        return self.starttls_offered

    def starttls(self, context=None):
        """Record the STARTTLS call."""
        self.calls.append("starttls")

    def login(self, user, password):
        """Record the login call."""
        self.calls.append("login")

    def send_message(self, msg, to_addrs=None):
        """Record a sent message, or raise the configured failure."""
        if self.fail_with:
            raise self.fail_with
        self.calls.append("send")
        self.sent.append((msg, to_addrs))


@pytest.fixture
def mail(monkeypatch, tmp_path):
    """Enable email with test settings and replace smtplib with the fake server."""
    FakeSMTP.instances = []
    for name, value in {"EMAIL_ENABLED": "true", "SMTP_HOST": "smtp.example.test", "SMTP_PORT": "587",
                        "SMTP_USER": "user", "SMTP_PASSWORD": "s3cret-pw", "EMAIL_FROM": "desk@example.test",
                        "EMAIL_TO": "owner@example.test, second@example.test"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(brief_email.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(brief_email.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(config, "EMAIL_OUTBOX_DIR", tmp_path / "outbox")
    return FakeSMTP


def put_brief(status="published", day="2026-10-26", sources=SOURCES, key_points=KP, evaluation=None):
    """Store a brief with sources and key points for the email tests."""
    storage.save_brief(day, status, 0.95 if status == "published" else 0.6, BODY, evaluation or {"reasons": []},
                       key_points=key_points, sources=sources)


def text_of(msg):
    """The plain-text body of a message."""
    return msg.get_body(preferencelist=("plain",)).get_content()


def html_of(msg):
    """The HTML body of a message."""
    return msg.get_body(preferencelist=("html",)).get_content()


def test_published_brief_is_sent_once_over_starttls_to_all_recipients(db, mail):
    """A published brief is sent once to every recipient, with STARTTLS before login."""
    put_brief()
    assert send_brief_email.send_brief_email("2026-10-26") == "sent"
    assert send_brief_email.send_brief_email("2026-10-26") == "already_sent"
    assert len(mail.instances) == 1
    server = mail.instances[0]
    assert server.calls == ["ehlo", "starttls", "ehlo", "login", "send"]        # TLS before credentials
    msg, to = server.sent[0]
    assert to == ["owner@example.test", "second@example.test"]
    assert msg["Subject"] == "Morning Intelligence Brief 2026-10-26"
    assert storage.recent_job_runs("send_brief_email", 1)[0]["status"] == "success"
    assert storage.email_sent("2026-10-26", "brief")


def test_published_email_content(db, mail):
    """The email has status, time, prices, sources and escaped HTML, and no cost figures or unverified items."""
    storage.save_run_cost({"job_name": "x", "started_at": "2026-10-26T00:00:00+00:00", "finished_at": "", "status": "success",
                           "claude_usd": 0.987654, "apify_usd": 0, "serper_usd": 0, "total_usd": 0.987654,
                           "input_tokens": 0, "output_tokens": 0, "serper_credits": 0, "details": {}})
    storage.save_articles([art("lowconf", "LOW-CONFIDENCE-HEADLINE", confidence=0.2)])
    put_brief()
    msg = brief_email.build_brief_message(storage.brief_for_date("2026-10-26"), ["a@example.test"])
    plain, html_part = text_of(msg), html_of(msg)
    assert "Status: published (groundedness 0.95)" in plain and "Generated: " in plain
    assert "PRICE SNAPSHOT" in plain and "BRENT: 80.10, +1.20%" in plain
    assert "[1] Oil jumps on refinery strikes - reuters.com - https://reuters.com/a" in plain
    assert "SECRET-BRIEF-BODY" in plain
    assert "<script>" not in html_part and "&lt;script&gt;" in html_part and "<strong>bold</strong>" in html_part
    for part in (plain, html_part):
        assert "0.987654" not in part and "usd" not in part.lower()               # no cost details
        assert "LOW-CONFIDENCE-HEADLINE" not in part                               # no unverified items


def test_old_brief_without_sources_or_prices_says_so(db, mail):
    """An older brief says its sources and prices were not stored."""
    put_brief(sources=None, key_points=None)
    plain = text_of(brief_email.build_brief_message(storage.brief_for_date("2026-10-26"), ["a@example.test"]))
    assert "Sources were not stored for this brief." in plain and "No price snapshot was stored" in plain


def test_flagged_brief_sends_owner_only_notice_without_the_brief(db, mail):
    """A held brief sends only a notice to the owner, with no brief text."""
    put_brief("flagged_for_review", evaluation={"reasons": ["groundedness 0.60 below 0.90", "unmatched numbers: 123"]})
    assert send_brief_email.send_brief_email("2026-10-26") == "sent"
    msg, to = mail.instances[0].sent[0]
    assert to == ["owner@example.test"]                                          # first EMAIL_TO address, no one else
    assert msg["Subject"].startswith("[Needs review]")
    body = text_of(msg)
    assert "NOT sent to the distribution list" in body and "unmatched numbers: 123" in body
    assert "SECRET-BRIEF-BODY" not in body and "SECRET-BRIEF-BODY" not in html_of(msg)
    assert storage.email_sent("2026-10-26", "review_notice") and not storage.email_sent("2026-10-26", "brief")


def test_email_owner_setting_overrides_first_recipient(db, mail, monkeypatch):
    """EMAIL_OWNER decides who gets the notice."""
    monkeypatch.setenv("EMAIL_OWNER", "boss@example.test")
    put_brief("flagged_for_review")
    send_brief_email.send_brief_email("2026-10-26")
    assert mail.instances[0].sent[0][1] == ["boss@example.test"]


def test_disabled_or_no_brief_sends_nothing(db, mail, monkeypatch):
    """Disabled email, or a day with no brief, sends nothing."""
    put_brief()
    monkeypatch.setenv("EMAIL_ENABLED", "false")
    assert send_brief_email.send_brief_email("2026-10-26") == "disabled"
    monkeypatch.setenv("EMAIL_ENABLED", "true")
    assert send_brief_email.send_brief_email("2026-10-27") == "no_brief"
    assert mail.instances == []


def test_failure_is_retried_after_the_cooldown_but_not_before_and_never_leaks_the_password(db, mail, monkeypatch):
    """A failed send is logged without the password and not retried inside the cooldown."""
    put_brief()
    real_init = FakeSMTP.__init__

    def failing(self, *a, **k):
        """Make every fake SMTP connection fail with an error containing the password."""
        real_init(self, *a, **k)
        self.fail_with = smtplib.SMTPException("auth failed for s3cret-pw")

    monkeypatch.setattr(FakeSMTP, "__init__", failing)
    assert send_brief_email.send_brief_email("2026-10-26") == "failed"
    run = storage.recent_job_runs("send_brief_email", 1)[0]
    assert run["status"] == "failed" and "s3cret-pw" not in run["detail"]
    assert not storage.email_sent("2026-10-26", "brief")
    assert send_brief_email.send_brief_email("2026-10-26") == "not_claimed"      # inside the 30 minute cooldown


def test_server_without_starttls_is_refused_before_login(db, mail, monkeypatch):
    """A server that does not offer STARTTLS is refused before any login."""
    put_brief()
    real_init = FakeSMTP.__init__

    def no_tls(self, *a, **k):
        """Make every fake SMTP server withhold STARTTLS."""
        real_init(self, *a, **k)
        self.starttls_offered = False

    monkeypatch.setattr(FakeSMTP, "__init__", no_tls)
    assert send_brief_email.send_brief_email("2026-10-26") == "failed"
    assert "login" not in mail.instances[0].calls and "send" not in mail.instances[0].calls


def test_port_465_uses_implicit_tls_without_starttls(db, mail, monkeypatch):
    """Port 465 uses implicit TLS, so no STARTTLS step is run."""
    monkeypatch.setenv("SMTP_PORT", "465")
    put_brief()
    assert send_brief_email.send_brief_email("2026-10-26") == "sent"
    assert mail.instances[0].calls == ["login", "send"] and mail.instances[0].timeout == brief_email.SMTP_TIMEOUT_S


def test_missing_smtp_settings_fail_visibly(db, mail, monkeypatch):
    """A missing SMTP setting fails the send and names the setting in the job log."""
    monkeypatch.setenv("SMTP_HOST", "")
    put_brief()
    assert send_brief_email.send_brief_email("2026-10-26") == "failed"
    assert "SMTP_HOST" in storage.recent_job_runs("send_brief_email", 1)[0]["detail"]


def test_header_injection_is_neutralised(db, mail):
    """Line breaks in header values and recipients cannot add headers or recipients."""
    put_brief(day="2026-10-26")
    brief = {**storage.brief_for_date("2026-10-26"), "date": "2026-10-26\r\nBcc: evil@example.test"}
    msg = brief_email.build_brief_message(brief, ["a@example.test"])
    assert "\n" not in msg["Subject"] and "\r" not in msg["Subject"] and msg["Bcc"] is None
    addrs = brief_email.parse_addresses("a@example.test\r\nBcc: evil@example.test, b@example.test; not-an-address")
    assert "evil@example.test" not in addrs and "b@example.test" in addrs and all(" " not in a for a in addrs)
    assert brief_email.clean_header("hello\r\nBcc: x") == "hello Bcc: x"


def test_dry_run_writes_an_eml_and_changes_nothing(db, mail, monkeypatch):
    """A dry run writes an .eml file, sends nothing and marks nothing as sent."""
    monkeypatch.setenv("EMAIL_ENABLED", "false")                                  # a preview works while disabled
    put_brief()
    outcome = send_brief_email.send_brief_email("2026-10-26", dry_run=True)
    assert outcome.startswith("dry_run: ")
    path = config.EMAIL_OUTBOX_DIR / "2026-10-26_brief.eml"
    parsed = email.message_from_bytes(path.read_bytes())
    assert parsed["Subject"] == "Morning Intelligence Brief 2026-10-26" and parsed.is_multipart()
    assert mail.instances == [] and not storage.email_sent("2026-10-26", "brief")
    assert storage.recent_job_runs("send_brief_email", 5) == []


def test_run_evaluation_stores_the_numbered_sources(db, monkeypatch):
    """run_evaluation saves the numbered sources the brief cites."""
    storage.save_articles([art("a", "Oil rises on supply fears", collected_at=storage.utcnow())])
    monkeypatch.setattr(run_evaluation.data_sources, "refresh_eia_weekly", lambda: None)
    monkeypatch.setattr(run_evaluation.data_sources, "fetch_eia_data", lambda: {})
    monkeypatch.setattr(run_evaluation.sentiment_analysis, "current_sentiment",
                        lambda: {"label": "Bull", "score": 0.2, "n": 1, "counts": {}})
    monkeypatch.setattr(run_evaluation.rag_pipeline, "history_context", lambda q: "")
    monkeypatch.setattr(run_evaluation.llm_pipeline, "generate_brief", lambda *a, **k: "# t\n\nbody [1]")
    monkeypatch.setattr(run_evaluation.brief_evaluation, "evaluate_brief",
                        lambda *a: {"passed": True, "groundedness": 1.0, "reasons": []})
    assert run_evaluation.run_evaluation() == "published"
    stored = json.loads(storage.latest_brief()["sources_json"])
    assert stored == [{"n": 1, "title": "Oil rises on supply fears", "source": "reuters.com", "url": "a"}]
