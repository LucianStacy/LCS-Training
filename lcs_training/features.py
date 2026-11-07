"""Needs attention, customer email (outbox, templates, settings) and reports."""
from __future__ import annotations

import base64
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select

from . import attention, auth, docs, logic, mail, reports
from .config import APP_DIR
from .models import (EMAIL_STATUSES, AttentionDismissal, BillingInstallment, ConsultPlan, Contact, EmailMessage,
                     EmailTemplate, Quote, ServiceDeliveryLog, TrainingContract)

router = APIRouter()


def _app():
    from . import app as app_module  # late import: app.py includes this router
    return app_module


def get_db():
    yield from _app().get_db()


def render(request, name, db, **ctx):
    return _app().render(request, name, db, **ctx)


def go(url, msg="", err=""):
    return _app().go(url, msg, err)


def _back(form, default):
    b = (form.get("back") or "").strip()
    return b if b.startswith("/") and not b.startswith("//") else default


def _can(request):
    return lambda p: auth.has_perm(request.state.user, p)


# ---------------------------------------------------------------------------
# Needs attention
# ---------------------------------------------------------------------------
@router.get("/attention", response_class=HTMLResponse)
def attention_page(request: Request, all: str = "", db=Depends(get_db)):
    sy = _app().current_year(db)
    sections = attention.collect(db, sy, _can(request), show_dismissed=bool(all))
    total = sum(1 for s in sections for i in s.items if i.severity != "done")
    high = sum(1 for s in sections for i in s.items if i.severity == "high")
    return render(request, "attention.html", db, sections=sections, total=total, high=high, show_all=bool(all))


@router.post("/attention/dismiss")
async def attention_dismiss(request: Request, db=Depends(get_db)):
    form = await request.form()
    keys = form.getlist("key")
    for k in keys:
        if k and not db.get(AttentionDismissal, k):
            db.add(AttentionDismissal(key=k[:200], dismissed_by=_app().user_name(request), note=form.get("note")))
    db.commit()
    return go(_back(form, "/attention"), f"Marked {len(keys)} item(s) as checked.")


@router.post("/attention/restore")
async def attention_restore(request: Request, db=Depends(get_db)):
    form = await request.form()
    d = db.get(AttentionDismissal, form.get("key") or "")
    if d:
        db.delete(d)
        db.commit()
    return go(_back(form, "/attention?all=1"), "Item put back on the list.")


@router.post("/contracts/{cid}/categories")
async def contract_categories(request: Request, cid: int, db=Depends(get_db)):
    """Split a contract's value into income categories (and its unissued installments with it)."""
    c = db.get(TrainingContract, cid) or _404()
    form = await request.form()
    ff = _app().ff
    cats = {k: ff(form, f"amount_{k}") or 0 for k in logic.CATS}
    total = round(sum(cats.values()), 2)
    back = _back(form, f"/contracts/{cid}?tab=billing")
    if abs(total - c.total_contract_value) > 0.01:
        return go(back, err=f"{logic.party_name(db, c.party_type, c.party_id)}: the categories add up to "
                            f"${total:,.2f} but the contract is ${c.total_contract_value:,.2f}.")
    for k, v in cats.items():
        setattr(c, f"amount_{k}", v)
    # re-split installments that haven't been invoiced yet; invoiced ones keep their history
    tv = c.total_contract_value or 1
    for i in c.billing_schedule:
        if i.status in ("Planned",):
            for k in logic.CATS:
                setattr(i, f"amount_{k}", round(i.amount * cats[k] / tv, 2))
            i.amount_other = round(i.amount_other + i.amount - sum(getattr(i, f"amount_{k}") for k in logic.CATS), 2)
    db.commit()
    return go(back, f"{logic.party_name(db, c.party_type, c.party_id)}: categories saved.")


def _404():
    raise HTTPException(404, "Not found")


# ---------------------------------------------------------------------------
# Email: outbox
# ---------------------------------------------------------------------------
def _quote_html(db, m):
    q = db.get(Quote, m.quote_id)
    t = _app().templates.get_template("quote_attachment.html")
    logo = _logo_data()
    css = (APP_DIR / "static" / "style.css").read_text(encoding="utf-8")
    html_ = t.render(q=q, party=logic.get_party(db, q.party_type, q.party_id), logo_src=logo, css=css)
    return f"LCS-Training Quote {q.quote_number}.html", "text/html", html_.encode("utf-8")


def _plan_docx(db, m):
    p = db.get(ConsultPlan, m.plan_id)
    client = logic.party_name(db, p.contract.party_type, p.contract.party_id)
    coach = p.contract_service.assigned_coach.coach_name if p.contract_service and p.contract_service.assigned_coach else ""
    data = docs.consult_plan_docx(p, client, coach, logic.plan_hours(p))
    fname = re.sub(r"[^A-Za-z0-9 _-]", "", f"Pre-Consult Plan - {client}").strip() + ".docx"
    return fname, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", data


mail.ATTACHERS.update(quote_html=_quote_html, plan_docx=_plan_docx)
ATTACH_LABEL = {"quote_html": "the quote (opens in a browser, prints to PDF)", "plan_docx": "the consult plan (Word)"}


@router.get("/emails", response_class=HTMLResponse)
def outbox(request: Request, status: str = "Draft", db=Depends(get_db)):
    status = status if status in EMAIL_STATUSES else "Draft"
    rows = list(db.scalars(select(EmailMessage).where(EmailMessage.status == status).order_by(
        EmailMessage.sent_at.desc() if status == "Sent" else EmailMessage.created_at.desc()).limit(500)))
    counts = {s: len(list(db.scalars(select(EmailMessage.id).where(EmailMessage.status == s)))) for s in EMAIL_STATUSES}
    tpl_names = {t.key: t.name for t in db.scalars(select(EmailTemplate))}
    return render(request, "emails.html", db, rows=rows, status=status, counts=counts, tpl_names=tpl_names,
                  settings=mail.settings(db), ready=mail.smtp_ready(db), started=bool(logic.get_setting(db, "mail_since")),
                  parties=logic.all_parties(db))


@router.post("/emails/check")
async def outbox_check(request: Request, db=Depends(get_db)):
    if not logic.get_setting(db, "mail_since"):
        logic.set_setting(db, "mail_since", logic.today().isoformat())
        db.commit()
    n = mail.generate_reminders(db)
    return go("/emails", f"{n} new reminder draft(s)." if n else "No new reminders are due.")


@router.post("/emails/send-selected")
async def outbox_send_selected(request: Request, db=Depends(get_db)):
    form = await request.form()
    ids = [int(i) for i in form.getlist("id") if i.isdigit()]
    action = form.get("action") or "send"
    ok, errors = 0, []
    for i in ids:
        m = db.get(EmailMessage, i)
        if not m or m.status not in ("Draft", "Failed"):
            continue
        if action == "discard":
            m.status = "Discarded"
            ok += 1
            continue
        err = mail.send(db, m, _app().user_name(request))
        if err:
            errors.append(f"{m.subject}: {err}")
        else:
            ok += 1
    db.commit()
    verb = "discarded" if action == "discard" else "sent"
    return go("/emails" + ("?status=Failed" if errors and action != "discard" else ""),
              f"{ok} email(s) {verb}." if ok else "", "; ".join(errors[:3]))


@router.post("/emails/new")
async def email_new(request: Request, db=Depends(get_db)):
    """Start a draft from a quote, plan, contract, installment, delivery or client page."""
    form = await request.form()
    kind, oid, tpl = form.get("kind"), int(form.get("id") or 0), form.get("template") or ""
    who = _app().user_name(request)
    m = None
    if kind == "quote" and (q := db.get(Quote, oid)):
        m = mail.draft_quote(db, q, who)
    elif kind == "plan" and (p := db.get(ConsultPlan, oid)):
        m = mail.draft_plan(db, p, who)
    elif kind == "installment" and (i := db.get(BillingInstallment, oid)):
        m = mail.draft_installment(db, i, tpl if tpl in ("invoice_upcoming", "invoice_overdue") else "invoice_upcoming",
                                   who, dedupe=False)
    elif kind == "delivery" and (d := db.get(ServiceDeliveryLog, oid)):
        m = mail.draft_delivery(db, d, who, dedupe=False)
    elif kind == "contract" and (c := db.get(TrainingContract, oid)) and tpl == "onboarding_followup":
        from . import onboarding
        cl = logic.checklist_for(db, c)
        missing = onboarding.missing_items(cl) if cl else []
        if not missing:
            return go(_back(form, f"/contracts/{oid}?tab=onboarding"), err="Nothing is missing from the client.")
        m = mail.draft_onboarding_followup(db, c, missing, user=who)
    elif kind == "contract" and (c := db.get(TrainingContract, oid)):
        m = mail.draft_contract(db, c, tpl or "general", who, dedupe=False) if tpl not in ("", "general") else \
            mail.draft_general(db, c.party_type, c.party_id, c, user=who)
    elif kind == "client":
        pt, pid = logic.parse_party_key(form.get("party") or "")
        if pt:
            m = mail.draft_general(db, pt, pid, None, user=who)
    if m is None:
        return go(_back(form, "/emails"), err="Couldn't start that email (is the template there?).")
    db.commit()
    return go(f"/emails/{m.id}", "Draft created. Check it, then send.")


@router.get("/emails/settings", response_class=HTMLResponse)
def email_settings(request: Request, db=Depends(get_db)):
    return render(request, "email_settings.html", db, s=mail.settings(db), ready=mail.smtp_ready(db),
                  env_password=bool(__import__("os").environ.get("LCS_SMTP_PASSWORD")))


@router.post("/emails/settings")
async def email_settings_save(request: Request, db=Depends(get_db)):
    form = await request.form()
    mail.save_settings(db, dict(form))
    s = mail.settings(db)
    problems = mail.bad_addrs(s["from_email"]) + mail.bad_addrs(s["test_to"]) + mail.bad_addrs(s["bcc"]) + \
        mail.bad_addrs(s["reply_to"])
    db.commit()
    return go("/emails/settings", "Email settings saved.",
              ("Check these addresses: " + ", ".join(problems)) if problems else "")


@router.post("/emails/settings/test")
async def email_settings_test(request: Request, db=Depends(get_db)):
    form = await request.form()
    to = (form.get("to") or "").strip()
    if not mail.ADDR_RE.match(to):
        return go("/emails/settings", err="Enter the address to send the test to.")
    err = mail.test_message(db, to)
    return go("/emails/settings", "" if err else f"Test email sent to {to}.", f"Test failed: {err}" if err else "")


@router.get("/emails/templates", response_class=HTMLResponse)
def email_templates(request: Request, db=Depends(get_db)):
    tpls = list(db.scalars(select(EmailTemplate).order_by(EmailTemplate.sort_order)))
    return render(request, "email_templates.html", db, tpls=tpls, placeholders=mail.PLACEHOLDERS,
                  defaults={t["key"]: t for t in mail.DEFAULT_TEMPLATES})


@router.post("/emails/templates/{tid}")
async def email_template_save(request: Request, tid: int, db=Depends(get_db)):
    t = db.get(EmailTemplate, tid) or _404()
    form = await request.form()
    if form.get("reset"):
        d = next((x for x in mail.DEFAULT_TEMPLATES if x["key"] == t.key), None)
        if d:
            t.subject, t.body, t.days = d["subject"], d["body"], d["days"]
        db.commit()
        return go(f"/emails/templates#t{tid}", f"{t.name} reset to the original wording.")
    t.name = (form.get("name") or t.name).strip()
    t.subject = (form.get("subject") or "").strip() or t.subject
    t.body = (form.get("body") or "").replace("\r\n", "\n").strip() or t.body
    days = (form.get("days") or "").strip()
    t.days = int(days) if days.isdigit() else t.days
    t.auto_draft = bool(form.get("auto_draft"))
    t.auto_send = bool(form.get("auto_send")) and t.auto_draft
    db.commit()
    return go(f"/emails/templates#t{tid}", f"{t.name} saved.")


def _links(db, m: EmailMessage) -> list[tuple[str, str]]:
    out = []
    if m.party_type and m.party_id:
        out.append((logic.party_name(db, m.party_type, m.party_id), _app().party_url(m.party_type, m.party_id)))
    if m.contract_id:
        out.append(("contract", f"/contracts/{m.contract_id}"))
    if m.installment_id:
        out.append(("billing", f"/contracts/{m.contract_id}?tab=billing"))
    if m.quote_id and (q := db.get(Quote, m.quote_id)):
        out.append((f"quote {q.quote_number}", f"/quotes/{q.id}"))
    if m.plan_id:
        out.append(("consult plan", f"/plans/{m.plan_id}"))
    if m.delivery_id:
        out.append(("session", f"/deliveries/{m.delivery_id}"))
    return out


@router.get("/emails/{mid}", response_class=HTMLResponse)
def email_detail(request: Request, mid: int, db=Depends(get_db)):
    m = db.get(EmailMessage, mid) or _404()
    contacts = []
    if m.party_type and m.party_id:
        contacts = mail.party_contacts(db, m.party_type, m.party_id)
    t = mail.template(db, m.template_key) if m.template_key else None
    return render(request, "email.html", db, e=m, links=_links(db, m), contacts=contacts, tpl=t,
                  attach_label=ATTACH_LABEL.get(m.attach or ""), settings=mail.settings(db),
                  ready=mail.smtp_ready(db), preview=mail.html_body(m.body).replace("cid:lcslogo", _logo_data()))


def _logo_data():
    return "data:image/png;base64," + base64.b64encode((APP_DIR / "static" / "logo.png").read_bytes()).decode()


@router.post("/emails/{mid}")
async def email_save(request: Request, mid: int, db=Depends(get_db)):
    m = db.get(EmailMessage, mid) or _404()
    form = await request.form()
    action = form.get("action") or "save"
    if action == "restore":
        m.status = "Draft"
        db.commit()
        return go(f"/emails/{mid}", "Moved back to drafts.")
    if action == "copy":
        n = EmailMessage(template_key=m.template_key, party_type=m.party_type, party_id=m.party_id,
                         contract_id=m.contract_id, quote_id=m.quote_id, installment_id=m.installment_id,
                         delivery_id=m.delivery_id, plan_id=m.plan_id, to_addrs=m.to_addrs, cc_addrs=m.cc_addrs,
                         subject=m.subject, body=m.body, attach=m.attach)
        db.add(n)
        db.commit()
        return go(f"/emails/{n.id}", "Copied into a new draft.")
    if m.status == "Sent":
        return go(f"/emails/{mid}", err="This email was already sent. Copy it to send again.")
    m.to_addrs = (form.get("to_addrs") or "").strip()
    m.cc_addrs = (form.get("cc_addrs") or "").strip() or None
    m.subject = (form.get("subject") or "").strip() or m.subject
    m.body = (form.get("body") or "").replace("\r\n", "\n").strip()
    if m.attach and not form.get("keep_attach"):
        m.attach = None
    if action == "discard":
        m.status = "Discarded"
        db.commit()
        return go("/emails", "Draft discarded.")
    if action == "send":
        err = mail.send(db, m, _app().user_name(request))
        if err:
            db.commit()
            return go(f"/emails/{mid}", err=f"Not sent: {err}")
        return go("/emails?status=Sent", "Email sent" + (" (test mode: it went to the test address)."
                                                          if mail.settings(db)["test_mode"] else "."))
    if m.status == "Failed":
        m.status = "Draft"
    db.commit()
    bad = mail.bad_addrs(m.to_addrs) + mail.bad_addrs(m.cc_addrs or "")
    return go(f"/emails/{mid}", "Draft saved.", ("Check: " + ", ".join(bad)) if bad else "")


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
@router.get("/reports", response_class=HTMLResponse)
def reports_index(request: Request, db=Depends(get_db)):
    can = _can(request)
    groups = {}
    for r in reports.REPORTS:
        if can(r.perm):
            groups.setdefault(r.group, []).append(r)
    return render(request, "reports.html", db, groups=groups)


@router.get("/reports/{key}")
def report_view(request: Request, key: str, format: str = "", db=Depends(get_db)):
    r = reports.BY_KEY.get(key) or _404()
    if not auth.has_perm(request.state.user, r.perm):
        return _app()._denied(request, request.state.user)
    sy = _app().current_year(db)
    params = {name: request.query_params.get(name, default) for name, _, _, default in r.params}
    res = r.fn(db, sy, params)
    if format == "xlsx":
        data = reports.to_xlsx(res, sy)
        fname = f"LCS-Training {r.title} {sy}.xlsx".replace("/", "-")
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    chart = None
    if res.chart and res.rows:
        mx = max((sum(row.get(k) or 0 for k, _ in res.chart["series"]) for row in res.rows), default=0) or 1
        chart = {"max": mx, "series": res.chart["series"], "label_key": res.chart["label_key"]}
    from urllib.parse import urlencode
    qs = urlencode({k: v for k, v in params.items() if v})
    return render(request, "report.html", db, r=r, res=res, params=params, chart=chart,
                  xlsx=f"/reports/{key}?{qs + '&' if qs else ''}format=xlsx")
