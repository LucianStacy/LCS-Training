"""Customer email: editable templates, reminder drafts, an outbox, and SMTP sending.

How it works
* Reminder rules (invoice coming due, invoice overdue, renewal, training reminder,
  welcome) create **drafts** in the outbox. Each event gets one draft only
  (``dedupe_key``), so re-checking never duplicates anything.
* Nothing goes out until someone presses Send, unless a template is switched to
  "send automatically".
* **Test mode** (on by default) sends every message to one internal address
  instead of the customer, with the real recipients shown in the subject.
* Reminders only cover events from the day email was first switched on, so the
  backlog from an import doesn't turn into a flood of drafts.
"""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, getaddresses, make_msgid
from typing import Callable, Optional

from sqlalchemy import select

from . import logic
from .config import APP_DIR, COMPANY, FOOTER_LINE
from .models import (BillingInstallment, ConsultPlan, Contact, EmailMessage, EmailTemplate, Quote, ServiceDeliveryLog, SessionLocal, TrainingContract)

ADDR_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$")

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
SIGN = "\n\nThank you,\n{sender_name}\nLCS-Training\n{company_phone}"

DEFAULT_TEMPLATES = [
    dict(key="quote", name="Quote for the client", days=None, auto_draft=False, subject="LCS-Training quote {quote_number} for {client}",
         body="Hello {contact_first},\n\nThank you for meeting with us. Here is the quote we discussed for "
              "{school_year}:\n\n{quote_items}\n\nTotal: {quote_total}\nThis quote is valid until {quote_valid_until}."
              "\n\nThe full quote is attached; open it in your browser to print or save it as a PDF. Let us know if "
              "you'd like to adjust anything." + SIGN),
    dict(key="welcome", name="Welcome: contract received", days=None, subject="Welcome to LCS-Training, {client}",
         body="Hello {contact_first},\n\nWe've received your fully executed contract for {school_year} "
              "({contract_start} to {contract_end}). Thank you!\n\nHere is what you've contracted for:\n"
              "{services}\n\nNext, we'll assign your coach and reach out to schedule a planning meeting." + SIGN),
    dict(key="invoice_upcoming", name="Invoice coming due", days=14,
         subject="Upcoming LCS-Training invoice: {amount} due {due_date}",
         body="Hello {contact_first},\n\nThis is a reminder that the next installment on your {school_year} "
              "contract is due on {due_date}.\n\nAmount: {amount}\nP.O. number: {po_number}\n\n"
              "If anything has changed with your billing contact or P.O., just reply to this email." + SIGN),
    dict(key="invoice_overdue", name="Invoice past due", days=30,
         subject="Past due: LCS-Training invoice {invoice_ref} ({amount})",
         body="Hello {contact_first},\n\nOur records show that invoice {invoice_ref} for {amount}, due "
              "{due_date}, is still open.\n\nIf it has already been paid, thank you, and please disregard this "
              "note. Otherwise, could you let us know when we can expect payment?" + SIGN),
    dict(key="renewal", name="Renewal reminder", days=60,
         subject="Planning for {next_school_year} with LCS-Training",
         body="Hello {contact_first},\n\nYour {school_year} contract with LCS-Training ends on {contract_end}. "
              "So far we've delivered {delivered_summary}.\n\nWe'd love to talk about next year. Would you like "
              "to set up a short meeting to review this year and plan {next_school_year}?" + SIGN),
    dict(key="training_reminder", name="Training / audit reminder", days=3,
         subject="Reminder: {training_subject} on {training_date}",
         body="Hello {contact_first},\n\nThis is a reminder of your upcoming session:\n\n{training_subject}\n"
              "Date: {training_date} {training_time}\nFormat: {mode}\nCoach: {coach}\n\nPlease make sure "
              "participants have their logins ready. Let us know if you need to reschedule." + SIGN),
    dict(key="onboarding_followup", name="Onboarding follow-up (missing items)", days=7,
         subject="Still needed to set up your LCS-Training services, {client}",
         body="Hello {contact_first},\n\nThank you again for choosing LCS-Training for {school_year}. To finish "
              "setting up your services, we still need:\n\n{missing_items}\n\nYou can reply to this email with "
              "the information or attach the files." + SIGN),
    dict(key="consult_plan", name="Consult plan for the client", days=None, auto_draft=False,
         subject="Your LCS-Training consult plan, {client}",
         body="Hello {contact_first},\n\nAttached is the consult plan from our pre-consult meeting, with the "
              "goals, objectives and session schedule we discussed.\n\nPlease review it and let us know if "
              "anything should change." + SIGN),
    dict(key="general", name="General message", days=None, auto_draft=False, subject="LCS-Training: {client}",
         body="Hello {contact_first},\n\n" + SIGN.lstrip("\n")),
]

PLACEHOLDERS = {
    "Everywhere": ["client", "contact_first", "contact_name", "school_year", "next_school_year", "sender_name",
                   "company_phone"],
    "Contracts": ["contract_start", "contract_end", "contract_value", "po_number", "services", "delivered_summary"],
    "Invoices": ["due_date", "amount", "invoice_ref"],
    "Quotes": ["quote_number", "quote_total", "quote_valid_until", "quote_items"],
    "Trainings": ["training_subject", "training_date", "training_time", "mode", "coach"],
    "Onboarding": ["missing_items"],
}


def seed_templates(db) -> None:
    have = {t.key for t in db.scalars(select(EmailTemplate))}
    for i, t in enumerate(DEFAULT_TEMPLATES):
        if t["key"] not in have:
            db.add(EmailTemplate(sort_order=i, auto_draft=t.get("auto_draft", True), **{
                k: v for k, v in t.items() if k != "auto_draft"}))
    db.commit()


def template(db, key) -> Optional[EmailTemplate]:
    return db.scalar(select(EmailTemplate).where(EmailTemplate.key == key))


class _Safe(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def fill(text: str, ctx: dict) -> str:
    return (text or "").format_map(_Safe({k: ("" if v is None else v) for k, v in ctx.items()}))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
SETTING_KEYS = ["smtp_host", "smtp_port", "smtp_security", "smtp_user", "from_name", "from_email", "reply_to",
                "bcc", "test_mode", "test_to", "sender_name"]


def settings(db) -> dict:
    s = {k: logic.get_setting(db, f"mail_{k}") for k in SETTING_KEYS}
    s["smtp_port"] = s["smtp_port"] or "587"
    s["smtp_security"] = s["smtp_security"] or "starttls"
    s["test_mode"] = s["test_mode"] != "off"          # on unless switched off
    s["from_name"] = s["from_name"] or COMPANY["name"]
    s["has_password"] = bool(_password(db))
    return s


def _password(db) -> str:
    return os.environ.get("LCS_SMTP_PASSWORD") or logic.get_setting(db, "mail_smtp_password")


def save_settings(db, form: dict) -> None:
    for k in SETTING_KEYS:
        if k == "test_mode":
            logic.set_setting(db, "mail_test_mode", "on" if form.get("test_mode") else "off")
        elif k in form:
            logic.set_setting(db, f"mail_{k}", (form.get(k) or "").strip())
    if (form.get("smtp_password") or "").strip():
        logic.set_setting(db, "mail_smtp_password", form["smtp_password"].strip())
    if form.get("clear_password"):
        logic.set_setting(db, "mail_smtp_password", "")
    if not logic.get_setting(db, "mail_since"):
        logic.set_setting(db, "mail_since", logic.today().isoformat())


def smtp_ready(db) -> bool:
    s = settings(db)
    return bool(s["smtp_host"] and s["from_email"] and (not s["test_mode"] or s["test_to"]))


def since(db) -> dt.date:
    v = logic.get_setting(db, "mail_since")
    return dt.date.fromisoformat(v) if v else logic.today()


# ---------------------------------------------------------------------------
# Context and recipients
# ---------------------------------------------------------------------------
def _first(name: str) -> str:
    return (name or "").strip().split(" ")[0] if name else "there"


def party_contacts(db, party_type, party_id) -> list[Contact]:
    return [c for c in db.scalars(select(Contact).where(Contact.party_type == party_type, Contact.party_id == party_id)
                                  .order_by(Contact.full_name)) if (c.email_id or "").strip()]


def _addr(c: Contact) -> str:
    return formataddr((c.full_name, c.email_id.strip()))


def recipients_for(db, party_type, party_id, contract: Optional[TrainingContract] = None,
                   billing: bool = False) -> tuple[str, Optional[Contact]]:
    """Who a message to this client should go to: (To line, the contact we greet)."""
    if contract is not None:
        main = contract.main_contact
        if billing and (contract.invoice_email or "").strip():
            return contract.invoice_email.strip(), main
        if main and (main.email_id or "").strip() and not main.no_email:
            return _addr(main), main
    p = logic.get_party(db, party_type, party_id)
    prim = getattr(p, "primary_contact", None)
    if prim and (prim.email_id or "").strip() and not prim.no_email:
        return _addr(prim), prim
    for c in party_contacts(db, party_type, party_id):
        if not c.no_email:
            return _addr(c), c
    return "", None


def _next_sy(sy: str) -> str:
    a = int(sy[:4]) + 1
    return f"{a}-{str(a + 1)[2:]}"


def base_ctx(db, party_type, party_id, sy: str, contact: Optional[Contact], user: str = "") -> dict:
    s = settings(db)
    return {"client": logic.party_name(db, party_type, party_id), "contact_first": _first(contact.full_name) if contact else "there",
            "contact_name": contact.full_name if contact else "", "school_year": sy, "next_school_year": _next_sy(sy),
            "sender_name": s["sender_name"] or user or "The LCS-Training team",
            "company_phone": COMPANY["phones"][0]}


def _money(v):
    return f"${(v or 0):,.2f}"


def contract_ctx(c: TrainingContract) -> dict:
    lines = c.current_services
    u = logic.contract_utilization(c)
    done = [f"{x['done']:g} of {x['line'].contracted_quantity:g} {x['line'].service_type.service_name}"
            for x in u.lines if x["line"].contracted_quantity and x["done"]]
    return {"contract_start": f"{c.contract_start_date:%B %d, %Y}", "contract_end": f"{c.contract_end_date:%B %d, %Y}",
            "contract_value": _money(c.total_contract_value), "po_number": c.po_number or "(none on file)",
            "services": "\n".join(f"  • {s.service_type.service_name}: {s.contracted_quantity:g} "
                                  f"{s.service_type.delivery_unit}{'s' if s.contracted_quantity != 1 else ''}"
                                  for s in lines) or "  • (see contract)",
            "delivered_summary": ", ".join(done) if done else "the services in your contract"}


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------
def _new(db, tpl_key, ctx, to, *, dedupe=None, cc="", attach=None, **links) -> Optional[EmailMessage]:
    if dedupe and db.scalar(select(EmailMessage.id).where(EmailMessage.dedupe_key == dedupe)):
        return None
    t = template(db, tpl_key)
    if t is None:
        return None
    m = EmailMessage(template_key=tpl_key, dedupe_key=dedupe, to_addrs=to, cc_addrs=cc or None,
                     subject=fill(t.subject, ctx), body=fill(t.body, ctx), attach=attach, **links)
    db.add(m)
    db.flush()
    return m


def draft_quote(db, q: Quote, user="") -> EmailMessage:
    to, ct = recipients_for(db, q.party_type, q.party_id)
    ctx = base_ctx(db, q.party_type, q.party_id, q.school_year, ct, user)
    items = [it for it in q.items if abs(it.amount or 0) > 0.004]
    ctx.update(quote_number=q.quote_number, quote_total=_money(q.grand_total),
               quote_valid_until=f"{q.valid_until:%B %d, %Y}" if q.valid_until else "(see quote)",
               quote_items="\n".join(f"  • {it.description or (it.price_item.item_name if it.price_item else 'Item')}"
                                     f": {_money(it.amount)}" for it in items))
    return _new(db, "quote", ctx, to, attach="quote_html", party_type=q.party_type, party_id=q.party_id,
                quote_id=q.id)


def draft_plan(db, p: ConsultPlan, user="") -> EmailMessage:
    c = p.contract
    to, ct = recipients_for(db, c.party_type, c.party_id, c)
    ctx = base_ctx(db, c.party_type, c.party_id, c.school_year, ct, user) | contract_ctx(c)
    return _new(db, "consult_plan", ctx, to, attach="plan_docx", party_type=c.party_type,
                party_id=c.party_id, contract_id=c.id, plan_id=p.id)


def draft_general(db, party_type, party_id, contract: Optional[TrainingContract], tpl_key="general", user=""):
    sy = contract.school_year if contract else logic.get_setting(db, "school_year") or logic.today().strftime("%Y")
    to, ct = recipients_for(db, party_type, party_id, contract)
    ctx = base_ctx(db, party_type, party_id, sy, ct, user) | (contract_ctx(contract) if contract else {})
    return _new(db, tpl_key, ctx, to, party_type=party_type, party_id=party_id,
                contract_id=contract.id if contract else None)


def draft_installment(db, i: BillingInstallment, tpl_key: str, user="", dedupe=True) -> Optional[EmailMessage]:
    c = i.contract
    to, ct = recipients_for(db, c.party_type, c.party_id, c, billing=True)
    cc = c.main_contact.email_id if (c.main_contact and c.main_contact.email_id and c.invoice_email
                                     and c.main_contact.email_id.lower() not in c.invoice_email.lower()) else ""
    ctx = base_ctx(db, c.party_type, c.party_id, c.school_year, ct, user) | contract_ctx(c)
    ctx.update(due_date=f"{i.due_date:%B %d, %Y}", amount=_money(i.amount), invoice_ref=i.invoice_ref or "")
    return _new(db, tpl_key, ctx, to, cc=cc, dedupe=f"{tpl_key}:inst:{i.id}" if dedupe else None,
                party_type=c.party_type, party_id=c.party_id, contract_id=c.id, installment_id=i.id)


def draft_delivery(db, d: ServiceDeliveryLog, user="", dedupe=True) -> Optional[EmailMessage]:
    c = d.contract
    to, ct = recipients_for(db, c.party_type, c.party_id, c)
    ctx = base_ctx(db, c.party_type, c.party_id, c.school_year, ct, user) | contract_ctx(c)
    name = d.subject or (d.contract_service.service_type.service_name if d.contract_service else "Training")
    ctx.update(training_subject=name, training_date=f"{d.date_performed:%A, %B %d, %Y}" if d.date_performed else "",
               training_time=d.time_text or "", mode=d.mode or "To be confirmed",
               coach=d.coach.coach_name if d.coach else "To be confirmed")
    return _new(db, "training_reminder", ctx, to,
                dedupe=f"training_reminder:del:{d.id}:{d.date_performed}" if dedupe else None,
                party_type=c.party_type, party_id=c.party_id, contract_id=c.id, delivery_id=d.id)


def draft_contract(db, c: TrainingContract, tpl_key: str, user="", dedupe=True) -> Optional[EmailMessage]:
    to, ct = recipients_for(db, c.party_type, c.party_id, c)
    ctx = base_ctx(db, c.party_type, c.party_id, c.school_year, ct, user) | contract_ctx(c)
    return _new(db, tpl_key, ctx, to, dedupe=f"{tpl_key}:contract:{c.id}" if dedupe else None,
                party_type=c.party_type, party_id=c.party_id, contract_id=c.id)


def draft_onboarding_followup(db, c: TrainingContract, missing, rnd=None, user="") -> Optional[EmailMessage]:
    from . import onboarding
    to, ct = recipients_for(db, c.party_type, c.party_id, c)
    ctx = base_ctx(db, c.party_type, c.party_id, c.school_year, ct, user) | contract_ctx(c)
    ctx["missing_items"] = "\n".join(f"  • {onboarding.item_label(t)}" for t in missing)
    return _new(db, "onboarding_followup", ctx, to,
                dedupe=f"onboarding_followup:contract:{c.id}:{rnd}" if rnd is not None else None,
                party_type=c.party_type, party_id=c.party_id, contract_id=c.id)


def generate_reminders(db) -> int:
    """Create drafts for every reminder that is due. Safe to run any number of times."""
    today, start = logic.today(), since(db)
    tpls = {t.key: t for t in db.scalars(select(EmailTemplate)) if t.auto_draft}
    made = 0

    def days(k, default):
        return tpls[k].days if tpls[k].days is not None else default

    if "invoice_upcoming" in tpls:
        horizon = today + dt.timedelta(days=days("invoice_upcoming", 14))
        for i in db.scalars(select(BillingInstallment).where(BillingInstallment.status == "Planned",
                                                             BillingInstallment.due_date >= today,
                                                             BillingInstallment.due_date <= horizon)):
            if i.contract.status in logic.ACTIVE_CONTRACT and draft_installment(db, i, "invoice_upcoming"):
                made += 1
    if "invoice_overdue" in tpls:
        n = days("invoice_overdue", 30)
        for i in db.scalars(select(BillingInstallment).where(BillingInstallment.status == "Invoiced")):
            became_overdue = i.due_date + dt.timedelta(days=n)
            if start <= became_overdue <= today and draft_installment(db, i, "invoice_overdue"):
                made += 1
    if "renewal" in tpls:
        n = days("renewal", 60)
        for c in db.scalars(select(TrainingContract).where(TrainingContract.status.in_(logic.ACTIVE_CONTRACT))):
            left = (c.contract_end_date - today).days
            if 0 <= left <= n and c.contract_end_date - dt.timedelta(days=n) >= start - dt.timedelta(days=7):
                if draft_contract(db, c, "renewal"):
                    made += 1
    if "training_reminder" in tpls:
        horizon = today + dt.timedelta(days=days("training_reminder", 3))
        for d in db.scalars(select(ServiceDeliveryLog).where(ServiceDeliveryLog.status == "Scheduled",
                                                             ServiceDeliveryLog.date_performed >= today,
                                                             ServiceDeliveryLog.date_performed <= horizon)):
            if draft_delivery(db, d):
                made += 1
    if "onboarding_followup" in tpls:
        from . import onboarding
        every = days("onboarding_followup", 7)
        for c, missing, rnd in onboarding.followups_due(db, every, start, today):
            waiting = db.scalar(select(EmailMessage.id).where(
                EmailMessage.contract_id == c.id, EmailMessage.template_key == "onboarding_followup",
                EmailMessage.status.in_(("Draft", "Failed"))))
            if not waiting and draft_onboarding_followup(db, c, missing, rnd):
                made += 1
    db.commit()
    return made


def welcome_on_signing(db, c: TrainingContract) -> bool:
    """Called when a contract leaves Draft in the app (fully executed contract received)."""
    t = template(db, "welcome")
    if not t or not t.auto_draft or not logic.get_setting(db, "mail_since"):
        return False
    return draft_contract(db, c, "welcome") is not None


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
# app.py registers functions that build attachments: name -> fn(db, message) -> (filename, mime, bytes)
ATTACHERS: dict[str, Callable] = {}


def split_addrs(s: str) -> list[str]:
    return [a for _, a in getaddresses([(s or "").replace(";", ",")]) if a]


def bad_addrs(s: str) -> list[str]:
    return [a for a in split_addrs(s) if not ADDR_RE.match(a)]


def html_body(text: str) -> str:
    paras = "".join(f"<p style=\"margin:0 0 12px\">{html.escape(p).replace(chr(10), '<br>')}</p>"
                    for p in text.strip().split("\n\n"))
    return f"""<!doctype html><html><body style="margin:0;background:#F4F4F6">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F4F4F6;padding:20px 0">
<tr><td align="center"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"
 style="max-width:600px;background:#ffffff;border-top:4px solid #74439A;border-radius:6px">
<tr><td style="padding:18px 28px 6px"><img src="cid:lcslogo" alt="LCS-Training" height="44" style="display:block"></td></tr>
<tr><td style="padding:10px 28px 18px;font-family:Calibri,Arial,sans-serif;font-size:15px;line-height:1.5;color:#2D2D2D">{paras}</td></tr>
<tr><td style="padding:12px 28px;border-top:1px solid #e2e0e6;font-family:Calibri,Arial,sans-serif;font-size:12px;color:#6E6E6E">{html.escape(FOOTER_LINE)}</td></tr>
</table></td></tr></table></body></html>"""


def build_mime(db, m: EmailMessage) -> tuple[MimeMessage, list[str]]:
    s = settings(db)
    to, cc = split_addrs(m.to_addrs), split_addrs(m.cc_addrs or "")
    bcc = split_addrs(s["bcc"])
    subject = m.subject
    if s["test_mode"]:
        subject = f"[TEST to {', '.join(to + cc) or 'nobody'}] {subject}"
        to, cc, bcc = split_addrs(s["test_to"]), [], []
    msg = MimeMessage()
    msg["From"] = formataddr((s["from_name"], s["from_email"]))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if s["reply_to"]:
        msg["Reply-To"] = s["reply_to"]
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=(s["from_email"].split("@")[-1] or "lcs-training.com"))
    msg.set_content(m.body + f"\n\n--\n{FOOTER_LINE}\n")
    msg.add_alternative(html_body(m.body), subtype="html")
    logo = APP_DIR / "static" / "logo.png"
    if logo.exists():
        msg.get_payload()[1].add_related(logo.read_bytes(), "image", "png", cid="<lcslogo>")
    if m.attach and m.attach in ATTACHERS:
        fname, mime, data = ATTACHERS[m.attach](db, m)
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=fname)
    return msg, to + cc + bcc


def _smtp(db):
    s = settings(db)
    host, port = s["smtp_host"], int(s["smtp_port"] or 587)
    ctx = ssl.create_default_context()
    if s["smtp_security"] == "ssl":
        conn = smtplib.SMTP_SSL(host, port, timeout=30, context=ctx)
    else:
        conn = smtplib.SMTP(host, port, timeout=30)
        if s["smtp_security"] == "starttls":
            conn.starttls(context=ctx)
    if s["smtp_user"]:
        conn.login(s["smtp_user"], _password(db))
    return conn


def send(db, m: EmailMessage, user: str = "") -> Optional[str]:
    """Send one message. Returns an error string, or None on success."""
    if m.status == "Sent":
        return "Already sent."
    if not smtp_ready(db):
        return "Email sending isn't set up yet (Email settings)."
    if not split_addrs(m.to_addrs):
        return "Add at least one recipient."
    bad = bad_addrs(m.to_addrs) + bad_addrs(m.cc_addrs or "")
    if bad:
        return "These don't look like email addresses: " + ", ".join(bad)
    try:
        msg, rcpts = build_mime(db, m)
        with _smtp(db) as conn:
            conn.send_message(msg, to_addrs=rcpts)
    except Exception as e:  # report every failure on the message itself
        m.status, m.error = "Failed", f"{type(e).__name__}: {e}"[:1000]
        db.commit()
        return m.error
    m.status, m.error, m.sent_at, m.sent_by = "Sent", None, dt.datetime.now(), user or "automatic"
    m.sent_to = ", ".join(rcpts)
    _after_send(db, m)
    db.commit()
    return None


def _after_send(db, m: EmailMessage) -> None:
    """Keep the rest of the app in step with what was emailed."""
    today = logic.today()
    if m.quote_id:
        q = db.get(Quote, m.quote_id)
        if q and q.status == "Draft":
            q.status = "Sent"
        if q and q.lead:
            logic._mark_steps(q.lead.steps, ["Quotes written & sent"], today)
    if m.plan_id:
        p = db.get(ConsultPlan, m.plan_id)
        if p and p.status == "Draft":
            p.status, p.plan_sent_date = "Sent", today
    from . import onboarding
    onboarding.sync_contract_id(db, m.contract_id)


def test_message(db, to: str) -> Optional[str]:
    s = settings(db)
    if not (s["smtp_host"] and s["from_email"]):
        return "Enter the mail server and From address first."
    m = EmailMessage(subject="LCS-Training test email", to_addrs=to,
                     body="This is a test from the LCS-Training app. If you can read this, email is set up.")
    try:
        msg, _ = build_mime(db, m)
        del msg["To"]
        msg["To"] = to
        msg.replace_header("Subject", m.subject)
        with _smtp(db) as conn:
            conn.send_message(msg, to_addrs=[to])
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return None


# ---------------------------------------------------------------------------
# Background: check for reminders every hour and send auto-send templates
# ---------------------------------------------------------------------------
def run_cycle() -> tuple[int, int]:
    with SessionLocal() as db:
        from . import onboarding
        onboarding.sync_all(db)
        if not logic.get_setting(db, "mail_since"):
            return 0, 0  # email hasn't been switched on yet
        made = generate_reminders(db)
        sent = 0
        auto = {t.key for t in db.scalars(select(EmailTemplate).where(EmailTemplate.auto_send))}
        if auto and smtp_ready(db):
            for m in db.scalars(select(EmailMessage).where(EmailMessage.status == "Draft",
                                                           EmailMessage.template_key.in_(auto))):
                if send(db, m, "automatic") is None:
                    sent += 1
        return made, sent


def start_scheduler(every_seconds: int = 3600) -> None:
    if os.environ.get("LCS_NO_SCHEDULER"):
        return

    def loop():
        time.sleep(20)
        while True:
            try:
                run_cycle()
            except Exception as e:  # never let the loop die
                print("Email reminder check failed:", e)
            time.sleep(every_seconds)

    threading.Thread(target=loop, daemon=True, name="lcs-mail").start()
