"""Build and send the morning brief by email.

Published briefs go to EMAIL_TO; a brief held for review produces only a short "needs review" notice to the owner.
Messages contain the brief, its numbered verified sources, a price snapshot and the generation time. They never
contain cost figures, unverified or low-confidence articles, or secrets.
"""
import html
import json
import logging
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from zoneinfo import ZoneInfo

import config

log = logging.getLogger("brief_email")
SMTP_TIMEOUT_S = 30
MAX_SUBJECT_CHARS = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_ADDRESS = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]+$")
DISCLAIMER = "Generated automatically from verified sources. Not investment advice; check against primary sources."


class EmailError(Exception):
    """Configuration or delivery problem. The message never contains the SMTP password."""


# ---- addresses and headers ------------------------------------------------

def clean_header(value, limit=MAX_SUBJECT_CHARS):
    """Header values cannot carry line breaks (header injection): replace every control character with a space."""
    return _CONTROL.sub(" ", str(value or "")).strip()[:limit]


def parse_addresses(raw):
    """Valid addresses from a comma- or semicolon-separated string; invalid ones are dropped."""
    out = []
    for part in re.split(r"[;,]", raw or ""):
        addr = parseaddr(clean_header(part))[1]
        if addr and _ADDRESS.match(addr) and addr not in out:
            out.append(addr)
    return out


def recipients():
    """Valid addresses in EMAIL_TO."""
    return parse_addresses(config.get("EMAIL_TO"))


def owner_address():
    """EMAIL_OWNER if set, else the first EMAIL_TO address."""
    owner = parse_addresses(config.get("EMAIL_OWNER"))
    return owner[0] if owner else (recipients() or [None])[0]


def sender_address():
    """The validated EMAIL_FROM address, or None."""
    addrs = parse_addresses(config.get("EMAIL_FROM"))
    return addrs[0] if addrs else None


# ---- content --------------------------------------------------------------

def _local_time(iso):
    """An ISO time as 'YYYY-MM-DD HH:MM UTC (HH:MM <desk timezone>)'."""
    utc = datetime.fromisoformat(iso).astimezone(timezone.utc)
    desk = utc.astimezone(ZoneInfo(config.DESK_TIMEZONE))
    return f"{utc:%Y-%m-%d %H:%M} UTC ({desk:%H:%M} {config.DESK_TIMEZONE})"


def _price_lines(key_points):
    """One text line per indicator from the brief's stored price snapshot."""
    prices = (key_points or {}).get("prices") or {}
    lines = []
    for key, p in prices.items():
        change = f", {p['change_pct']:+.2f}%" if p.get("change_pct") is not None else ""
        lines.append(f"{key}: {p['price']:,.2f}{change} (quote {p['as_of'][:16].replace('T', ' ')} UTC, {p.get('provider')})")
    return lines


def _source_lines(sources):
    """One '[n] title - outlet - url' line per numbered source."""
    return [f"[{s['n']}] {s['title']} - {s['source']}" + (f" - {s['url']}" if s.get("url") else "") for s in sources]


def md_to_html(text):
    """Tiny, safe markdown subset (headings, bullets, bold, paragraphs). Everything is HTML-escaped first, so
    scraped or model text can never inject markup."""
    out, in_list = [], False
    for raw in text.splitlines():
        line = html.escape(raw.rstrip())
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        if bullet:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{bullet.group(1)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{heading.group(2)}</h{level}>")
        elif line.strip():
            out.append(f"<p>{line}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def build_brief_message(brief, to_addrs, sender=None):
    """The full email for a published brief (a storage.briefs row)."""
    key_points = json.loads(brief["key_points_json"]) if brief.get("key_points_json") else None
    sources = json.loads(brief["sources_json"]) if brief.get("sources_json") else None
    prices = _price_lines(key_points)
    status = f"published (groundedness {brief['groundedness']:.2f})"
    generated = _local_time(brief["created_at"])
    source_lines = _source_lines(sources) if sources else ["Sources were not stored for this brief."]
    price_lines = prices or ["No price snapshot was stored for this brief."]

    plain = "\n".join([f"Status: {status}", f"Generated: {generated}", "", brief["text"], "",
                       "PRICE SNAPSHOT (latest quotes when the brief was generated)", *price_lines, "",
                       "SOURCES", *source_lines, "", DISCLAIMER])
    html_body = (
        f"<p><strong>Status:</strong> {html.escape(status)}<br><strong>Generated:</strong> {html.escape(generated)}</p>\n"
        f"{md_to_html(brief['text'])}\n<h3>Price snapshot</h3>\n"
        f"<p><small>Latest quotes when the brief was generated</small></p>\n"
        f"<ul>{''.join(f'<li>{html.escape(line)}</li>' for line in price_lines)}</ul>\n<h3>Sources</h3>\n"
        f"<ol style=\"list-style:none;padding-left:0\">{''.join(f'<li>{html.escape(line)}</li>' for line in source_lines)}</ol>\n"
        f"<p><small>{html.escape(DISCLAIMER)}</small></p>")
    return _message(f"Morning Intelligence Brief {brief['date']}", to_addrs, plain, html_body, sender)


def build_review_notice(brief, owner, sender=None):
    """Short notice to the owner for a brief held for review. No brief text and no article content."""
    evaluation = json.loads(brief["eval_json"] or "{}")
    reasons = [clean_header(r, 200) for r in evaluation.get("reasons", [])[:5]] or ["no reason recorded"]
    score = brief["groundedness"]
    plain = "\n".join([f"The brief for {brief['date']} failed its checks and was NOT sent to the distribution list.", "",
                       f"Groundedness: {score:.2f}" if score is not None else "Groundedness: n/a",
                       f"Generated: {_local_time(brief['created_at'])}", "", "Reasons:", *[f"- {r}" for r in reasons], "",
                       "Review it on the dashboard before anything is published."])
    html_body = (f"<p>The brief for {html.escape(brief['date'])} failed its checks and was <strong>not</strong> sent to "
                 f"the distribution list.</p><p>Groundedness: {html.escape(f'{score:.2f}' if score is not None else 'n/a')}"
                 f"<br>Generated: {html.escape(_local_time(brief['created_at']))}</p><p>Reasons:</p>"
                 f"<ul>{''.join(f'<li>{html.escape(r)}</li>' for r in reasons)}</ul>"
                 f"<p>Review it on the dashboard before anything is published.</p>")
    return _message(f"[Needs review] Morning brief {brief['date']} was held", [owner], plain, html_body, sender)


def _message(subject, to_addrs, plain, html_body, sender=None):
    """Assemble an EmailMessage with a sanitized subject, a plain-text body and an HTML alternative."""
    sender = sender or sender_address()
    if not sender or not to_addrs:
        raise EmailError("EMAIL_FROM and at least one valid recipient are required")
    msg = EmailMessage()
    msg["Subject"] = clean_header(subject)
    msg["From"] = formataddr(("Oil & Gas Morning Intelligence", sender))
    msg["To"] = ", ".join(to_addrs)
    msg["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg.set_content(plain)
    msg.add_alternative(f"<html><body style=\"font-family:sans-serif\">{html_body}</body></html>", subtype="html")
    return msg


# ---- delivery -------------------------------------------------------------

def check_smtp_settings():
    """Raise EmailError if a required SMTP/addressing setting is missing."""
    missing = [n for n in ("SMTP_HOST", "EMAIL_FROM", "EMAIL_TO") if not config.get(n)]
    if missing:
        raise EmailError(f"Missing settings in .env: {', '.join(missing)}")
    if config.get("SMTP_USER") and not config.get("SMTP_PASSWORD"):
        raise EmailError("SMTP_USER is set but SMTP_PASSWORD is empty")


def smtp_send(msg, to_addrs):
    """Send over TLS only: implicit TLS on port 465, otherwise STARTTLS, and refuse to log in or send if the
    server does not offer it."""
    host, port = config.get("SMTP_HOST"), int(config.get("SMTP_PORT") or 587)
    user, password = config.get("SMTP_USER"), config.get("SMTP_PASSWORD")
    context = ssl.create_default_context()
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_S, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_S)
        with server:
            if port != 465:
                server.ehlo()
                if not server.has_extn("starttls"):
                    raise EmailError("the SMTP server does not offer STARTTLS; nothing was sent")
                server.starttls(context=context)
                server.ehlo()
            if user:
                server.login(user, password)
            server.send_message(msg, to_addrs=to_addrs)
    except EmailError:
        raise
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        text = str(exc).replace(password, "***") if password else str(exc)
        raise EmailError(f"{type(exc).__name__}: {text}") from None


def write_eml(msg, outbox_dir, name):
    """Write a message to <outbox_dir>/<name>.eml (dry-run preview) and return the path."""
    outbox_dir.mkdir(parents=True, exist_ok=True)
    path = outbox_dir / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}.eml"
    path.write_bytes(bytes(msg))
    return path
