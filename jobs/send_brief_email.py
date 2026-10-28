"""Email today's brief once per day.

    python -m jobs.send_brief_email --dry-run [--day YYYY-MM-DD]   # write an .eml to the outbox, send nothing

The scheduler calls send_brief_email() after every collection cycle and after the daily brief; it does nothing
unless EMAIL_ENABLED=true and today's brief exists and has not been mailed yet.
"""
import argparse
import logging

import brief_email
import config
import cost_tracker
import demo_mode
import storage

log = logging.getLogger("send_brief_email")


@demo_mode.block_in_demo("jobs.send_brief_email.send_brief_email")
def send_brief_email(day=None, dry_run=False):
    """Returns a short outcome string: no_brief, disabled, already_sent, not_claimed, sent, failed, dry_run."""
    day = day or storage.utcnow()[:10]
    brief = storage.brief_for_date(day)
    if not brief:
        return "no_brief"
    published = brief["status"] == "published"
    kind = "brief" if published else "review_notice"

    if dry_run:
        # Placeholders stand in for unset addresses so a preview works before email is configured.
        to = brief_email.recipients() or ["recipient@example.invalid"]
        owner = brief_email.owner_address() or "owner@example.invalid"
        sender = brief_email.sender_address() or "desk@example.invalid"
        try:
            msg = (brief_email.build_brief_message(brief, to, sender) if published
                   else brief_email.build_review_notice(brief, owner, sender))
        except brief_email.EmailError as exc:
            return f"failed: {exc}"
        path = brief_email.write_eml(msg, config.EMAIL_OUTBOX_DIR, f"{day}_{kind}")
        log.info("Dry run: wrote %s", path)
        return f"dry_run: {path}"

    if not config.email_enabled():
        return "disabled"
    if storage.email_sent(day, kind):
        return "already_sent"
    # The claim stops two runs sending at once and caps retries after a failure; email_log is the "sent" marker.
    claim_name = "brief_email" if published else "brief_email_review"
    if not storage.claim_daily_run(claim_name, day, config.EMAIL_MAX_ATTEMPTS, config.EMAIL_RETRY_COOLDOWN_MIN):
        return "not_claimed"
    try:
        brief_email.check_smtp_settings()
        if published:
            to = brief_email.recipients()
            msg = brief_email.build_brief_message(brief, to)
        else:
            owner = brief_email.owner_address()
            to = [owner] if owner else []
            msg = brief_email.build_review_notice(brief, owner)
        brief_email.smtp_send(msg, to)
    except brief_email.EmailError as exc:
        storage.log_job("send_brief_email", "failed", str(exc))
        return "failed"
    storage.record_email_sent(day, kind, len(to))
    storage.log_job("send_brief_email", "success", f"{kind} for {day} sent to {len(to)} recipient(s)")
    return "sent"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="write an .eml file instead of sending")
    parser.add_argument("--day", help="brief date, default today (UTC)")
    args = parser.parse_args()
    with cost_tracker.cost_run("send_brief_email"):
        print(send_brief_email(args.day, args.dry_run))
