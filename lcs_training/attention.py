"""The Needs attention page: every loose end in the data, in one list.

Each check returns Items. An item has a stable ``key`` so someone can mark it
"checked, it's fine" (AttentionDismissal) and it stays hidden until the
underlying data changes enough to produce a different key.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select

from . import logic
from .models import (AttentionDismissal, BillingInstallment, Coach, ConsultPlan, Contact, ContractService,
                     EmailMessage, LcsDistrict, LcsSelpa, Lead, OnboardingChecklist, OnboardingTask, PriceItem,
                     Quote, ServiceDeliveryLog, ServiceType, TrainingContract)

EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}$")


@dataclass
class Item:
    key: str
    title: str
    detail: str = ""
    link: str = ""
    link_label: str = "Open"
    severity: str = "warn"          # "high" | "warn" | "info"
    fix: Optional[dict] = None      # inline quick-fix form, see template
    dismissable: bool = True


@dataclass
class Section:
    key: str
    title: str
    help: str
    perm: str = "view_ops"
    items: list = field(default_factory=list)
    dismissed: int = 0


def _money(v):
    return f"${v:,.2f}"


def _contract_label(db, c):
    return f"{logic.party_name(db, c.party_type, c.party_id)} {c.school_year}"


def _sig(*vals) -> str:
    """Short fingerprint of the values an item depends on, so a dismissal expires if they change."""
    return "|".join("" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v)) for v in vals)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def check_import_matches(db, sy) -> Section:
    sec = Section("matches", "Client names matched during import",
                  "The importer matched these spreadsheet names to an existing client. Confirm each one, or open the "
                  "client and use Merge into another client to fix a wrong match.", perm="edit_crm")
    raw = logic.get_setting(db, "import_matches")
    if not raw:
        last = logic.get_setting(db, "last_import")
        raw = json.dumps(json.loads(last).get("matched", [])) if last else "[]"
    by_name = {p.name: p for p in logic.all_parties(db)}
    seen = set()
    for name, tab, to, rule in json.loads(raw):
        if (name, to) in seen:
            continue
        seen.add((name, to))
        p = by_name.get(to)
        sec.items.append(Item(
            key=f"match:{name}=>{to}", title=f"“{name}” was treated as {to}",
            detail=f"Found in {tab}. Reason: {rule}.",
            link=logic_party_url(p) if p else "", link_label="Open client", severity="info"))
    return sec


def logic_party_url(p):
    return f"/clients/{'selpa' if isinstance(p, LcsSelpa) else 'district'}/{p.id}"


def check_contract_money(db, sy, contracts) -> Section:
    sec = Section("money", "Contract amounts",
                  "Contract values that are missing, not split into income categories, or don't match the "
                  "billing schedule.", perm="view_money")
    for c in contracts:
        if c.status == "Cancelled":
            continue
        label = _contract_label(db, c)
        url = f"/contracts/{c.id}?tab=billing"
        total = c.total_contract_value or 0
        cats = {k: getattr(c, f"amount_{k}") or 0 for k in logic.CATS}
        if total <= 0:
            sec.items.append(Item(f"zero:{c.id}", f"{label}: contract value is $0",
                                  "Enter the contract value and billing settings on the Billing tab.",
                                  url, "Open billing", "high", dismissable=True))
            continue
        if abs(sum(cats.values()) - total) > 0.01:
            sec.items.append(Item(f"catsum:{c.id}:{_sig(total, sum(cats.values()))}",
                                  f"{label}: categories add up to {_money(sum(cats.values()))}, "
                                  f"not {_money(total)}", "", url, "Open billing", "high",
                                  fix={"kind": "split", "contract": c.id, "total": total, "cats": cats}))
        elif cats["other"] >= total - 0.01:
            sec.items.append(Item(f"other:{c.id}:{_sig(total)}",
                                  f"{label}: all {_money(total)} is under Other",
                                  "Split it into Consultation, Training, Travel and Zone so revenue reports and "
                                  "ERPNext income accounts are right.", url, "Open billing", "warn",
                                  fix={"kind": "split", "contract": c.id, "total": total, "cats": cats}))
        live = [i for i in c.billing_schedule if i.status != "Cancelled"]
        if not live:
            sec.items.append(Item(f"nosched:{c.id}", f"{label}: no billing schedule",
                                  "Choose billing settings and generate the schedule.", url, "Open billing"))
        elif abs(sum(i.amount for i in live) - total) > 0.01:
            s = sum(i.amount for i in live)
            sec.items.append(Item(f"schedsum:{c.id}:{_sig(total, s)}",
                                  f"{label}: billing schedule totals {_money(s)}, contract is {_money(total)}",
                                  "Regenerate the schedule (invoiced installments are kept).", url, "Open billing",
                                  "high"))
    return sec


def check_billing(db, sy, contracts, today) -> Section:
    sec = Section("billing", "Invoices", "Installments that should have been invoiced, or invoiced ones that "
                  "are still unpaid 30 days after the due date.", perm="view_money")
    for c in contracts:
        for i in c.billing_schedule:
            label = _contract_label(db, c)
            if i.status == "Planned" and i.due_date < today:
                sec.items.append(Item(f"notinvoiced:{i.id}", f"{label}: {_money(i.amount)} due "
                                      f"{i.due_date:%m/%d/%Y} not marked invoiced",
                                      "Send the invoice and mark it Invoiced.",
                                      f"/contracts/{c.id}?tab=billing", "Open billing", "high", dismissable=False))
            elif i.status == "Invoiced" and (today - i.due_date).days > 30:
                sec.items.append(Item(f"unpaid:{i.id}", f"{label}: {_money(i.amount)} invoiced, unpaid "
                                      f"{(today - i.due_date).days} days",
                                      f"Invoice {i.invoice_ref or '(no number)'} was due {i.due_date:%m/%d/%Y}.",
                                      f"/contracts/{c.id}?tab=billing", "Open billing", "warn"))
    return sec


def check_contract_workflow(db, sy, contracts, today) -> Section:
    sec = Section("workflow", "Contract workflow", "Contracts stuck between steps, or whose status no longer "
                  "matches their dates.")
    for c in contracts:
        label = _contract_label(db, c)
        url = f"/contracts/{c.id}"
        if c.status == "Draft" and c.contract_start_date <= today:
            done = sum(1 for p in c.paperwork if p.status in ("Done", "N/A"))
            sec.items.append(Item(f"draft:{c.id}", f"{label}: still Draft, started {c.contract_start_date:%m/%d/%Y}",
                                  f"Paperwork {done} of {len(c.paperwork)} done. Mark the fully executed contract "
                                  "received to move it on.", url + "?tab=paperwork", "Open paperwork", "high"))
        if c.status in ("Pending Assignment", "Active"):
            missing = [s for s in c.current_services if not s.assigned_coach_id]
            if missing:
                sec.items.append(Item(f"nocoach:{c.id}:{len(missing)}",
                                      f"{label}: {len(missing)} service line(s) without a coach",
                                      ", ".join(s.service_type.service_name for s in missing),
                                      url + "?tab=services", "Assign coaches", "high"))
        if c.status in logic.ACTIVE_CONTRACT and c.contract_end_date < today:
            sec.items.append(Item(f"ended:{c.id}", f"{label}: ended {c.contract_end_date:%m/%d/%Y} but is "
                                  f"still {c.status}", "Set it to Expired, or extend the end date.", url,
                                  severity="warn"))
        if not c.main_contact_id and c.status != "Cancelled":
            sec.items.append(Item(f"nomain:{c.id}", f"{label}: no main contact",
                                  "Reminder emails go to the main contact.", url, severity="warn"))
        if c.status != "Cancelled" and not (c.invoice_email or "").strip():
            sec.items.append(Item(f"noinvemail:{c.id}", f"{label}: no “send invoices to” email",
                                  "Invoice reminders need it.", url, severity="info"))
        if c.status in ("Pending Assignment", "Active", "Renewal Pending") and not c.current_services:
            sec.items.append(Item(f"nolines:{c.id}", f"{label}: no service lines",
                                  "Add the contracted services so delivery and coach capacity are tracked.",
                                  url + "?tab=services", "Open services", "high"))
        for s in c.current_services:
            if not s.hours_per_unit and s.service_type.kind in ("Consult", "Training", "Audit"):
                sec.items.append(Item(f"nohours:{s.id}", f"{label}: {s.service_type.service_name} has 0 coach "
                                      "hours per unit", "Capacity can't count this line.", url + "?tab=services",
                                      "Open services", "info"))
    return sec


def check_renewals(db, sy, contracts, today) -> Section:
    sec = Section("renewals", "Renewals and delivery", "Contracts in the 60-day renewal window with nothing "
                  "started for next year, and contracts behind on delivery.", perm="view_sales")
    next_sy = f"{int(sy[:4]) + 1}-{str(int(sy[:4]) + 2)[2:]}"
    nxt = defaultdict(list)
    for q in db.scalars(select(Quote).where(Quote.school_year == next_sy)):
        nxt[(q.party_type, q.party_id)].append(f"quote {q.quote_number} ({q.status})")
    for c2 in db.scalars(select(TrainingContract).where(TrainingContract.school_year == next_sy)):
        nxt[(c2.party_type, c2.party_id)].append(f"contract ({c2.status})")
    for ld in db.scalars(select(Lead).where(Lead.school_year == next_sy)):
        nxt[(ld.party_type, ld.party_id)].append(f"lead ({ld.status})")
    for c in contracts:
        if c.status not in logic.ACTIVE_CONTRACT:
            continue
        label = _contract_label(db, c)
        days = (c.contract_end_date - today).days
        if 0 <= days <= 60 and not nxt.get((c.party_type, c.party_id)):
            sec.items.append(Item(f"renew:{c.id}", f"{label}: ends in {days} days, no {next_sy} quote yet",
                                  "Start a renewal quote or lead.", f"/contracts/{c.id}", "Open contract", "high"))
        u = logic.contract_utilization(c, today)
        if u.risk.startswith("Behind"):
            sec.items.append(Item(f"behind:{c.id}:{int((u.pct or 0) * 20)}",
                                  f"{label}: {u.pct * 100:.0f}% delivered, {u.elapsed * 100:.0f}% of the term gone",
                                  "Schedule the remaining sessions so the client sees the value before renewal.",
                                  f"/contracts/{c.id}?tab=delivery", "Open delivery", "warn"))
    return sec


def check_delivery(db, sy, today) -> Section:
    sec = Section("delivery", "Trainings, audits and consult plans",
                  "Sessions dated in the past that are still Scheduled, and consult plans that don't match "
                  "their contracted hours.")
    for d in db.scalars(select(ServiceDeliveryLog).where(ServiceDeliveryLog.status.in_(("Scheduled", "Planned")),
                                                         ServiceDeliveryLog.date_performed < today)):
        if d.contract.school_year != sy:
            continue
        sec.items.append(Item(f"past:{d.id}", f"{_contract_label(db, d.contract)}: "
                              f"{d.subject or (d.contract_service.service_type.service_name if d.contract_service else 'session')} "
                              f"on {d.date_performed:%m/%d/%Y} is still {d.status}",
                              "Mark it Completed (so it counts as delivered) or Cancelled.",
                              f"/deliveries/{d.id}", "Open", "warn", dismissable=False))
    for p in db.scalars(select(ConsultPlan).join(TrainingContract).where(TrainingContract.school_year == sy)):
        h = logic.plan_hours(p)
        label = _contract_label(db, p.contract)
        if h["over"]:
            sec.items.append(Item(f"overplan:{p.id}:{_sig(h['booked'])}",
                                  f"{label}: consult plan has {-h['unbooked']:g} more hours than contracted",
                                  f"{h['booked']:g} planned of {h['contracted']:g} contracted.",
                                  f"/plans/{p.id}", "Open plan", "high"))
        if p.status == "Draft" and p.consult_date and p.consult_date < today:
            sec.items.append(Item(f"plandraft:{p.id}", f"{label}: consult plan still Draft after the "
                                  f"{p.consult_date:%m/%d/%Y} consult", "Finish it and send it to the client.",
                                  f"/plans/{p.id}", "Open plan", "warn"))
    return sec


def check_onboarding(db, sy, today) -> Section:
    sec = Section("onboarding", "Onboarding", "Checklists with tasks still open 30 days after the contract "
                  "was signed.")
    for cl in db.scalars(select(OnboardingChecklist).join(TrainingContract).where(TrainingContract.school_year == sy)):
        if cl.contract.status in ("Cancelled", "Expired"):
            continue
        open_ = [t for t in cl.tasks if t.status in ("Not Started", "In Progress")]
        age = (today - cl.generated_date).days
        if open_ and age > 30:
            sec.items.append(Item(f"onb:{cl.id}:{len(open_)}", f"{_contract_label(db, cl.contract)}: "
                                  f"{len(open_)} onboarding task(s) open after {age} days",
                                  "First open: " + open_[0].step, f"/contracts/{cl.contract_id}?tab=onboarding",
                                  "Open checklist", "warn"))
    return sec


def check_sales(db, sy, today) -> Section:
    sec = Section("sales", "Quotes and leads", "Quotes past their valid-until date, drafts never sent, and open "
                  "leads with no activity for 30 days.", perm="view_sales")
    for q in db.scalars(select(Quote).where(Quote.status.in_(("Draft", "Sent")))):
        name = logic.party_name(db, q.party_type, q.party_id)
        if q.status == "Sent" and q.valid_until and q.valid_until < today:
            sec.items.append(Item(f"qexp:{q.id}", f"Quote {q.quote_number} ({name}) expired "
                                  f"{q.valid_until:%m/%d/%Y}", "Follow up, or mark it Expired or Declined.",
                                  f"/quotes/{q.id}", "Open quote", "warn"))
        elif q.status == "Draft" and (today - q.quote_date).days > 14:
            sec.items.append(Item(f"qdraft:{q.id}", f"Quote {q.quote_number} ({name}) has been a draft for "
                                  f"{(today - q.quote_date).days} days", "", f"/quotes/{q.id}", "Open quote", "info"))
    for ld in db.scalars(select(Lead).where(Lead.status.in_(("Open", "Quoted")), Lead.school_year >= sy)):
        dates = [s.step_date for s in ld.steps if s.step_date] + [d for d in [ld.initial_query_date] if d]
        last = max(dates) if dates else None
        if last and (today - last).days > 30:
            name = logic.party_name(db, ld.party_type, ld.party_id) if ld.party_id else (ld.contact_text or "Lead")
            sec.items.append(Item(f"lead:{ld.id}:{last.isoformat()}", f"Lead {name}: no activity since "
                                  f"{last:%m/%d/%Y}", "Next step: " + next((s.step for s in ld.steps
                                                                           if s.status != "Done"), "—"),
                                  f"/leads/{ld.id}", "Open lead", "info"))
    return sec


def check_reference(db, sy) -> Section:
    sec = Section("reference", "Price list and setup", "Prices not yet confirmed for the year and service types "
                  "with no coach-hour assumption.", perm="view_sales")
    n = len(list(db.scalars(select(PriceItem.id).where(PriceItem.school_year == sy, PriceItem.needs_review,
                                                       PriceItem.active))))
    if n:
        sec.items.append(Item(f"prices:{sy}:{n}", f"{n} {sy} price(s) still marked needs review",
                              "Confirm them before sending quotes.", "/prices", "Open price list", "high",
                              dismissable=False))
    if not db.scalar(select(PriceItem.id).where(PriceItem.school_year == sy).limit(1)):
        sec.items.append(Item(f"noprices:{sy}", f"No price list for {sy}",
                              "Import the Quote Pricing Worksheet or copy last year's prices.", "/prices",
                              "Open price list", "high", dismissable=False))
    for st in db.scalars(select(ServiceType).where(ServiceType.active, ServiceType.kind.in_(
            ("Consult", "Training", "Audit")))):
        if not st.default_hours_per_unit:
            sec.items.append(Item(f"sthours:{st.id}", f"Service type {st.service_name} has no coach hours per unit",
                                  "", "/setup", "Open setup", "info"))
    return sec


def check_contacts(db, sy, contracts) -> Section:
    sec = Section("contacts", "Contacts", "Email addresses that look wrong, duplicates, and clients with a "
                  "contract but no one to email.", perm="edit_crm")
    by_email = defaultdict(list)
    for ct in db.scalars(select(Contact)):
        e = (ct.email_id or "").strip()
        if e and not EMAIL_RE.match(e):
            sec.items.append(Item(f"bademail:{ct.id}:{e}", f"{ct.full_name}: “{e}” doesn't look like an email "
                                  "address", "", f"/contacts?q={ct.full_name}", "Find contact", "warn"))
        if e:
            by_email[e.lower()].append(ct)
    for e, cts in by_email.items():
        if len(cts) > 1:
            sec.items.append(Item(f"dupemail:{e}:{len(cts)}", f"{len(cts)} contacts share {e}",
                                  ", ".join(c.full_name for c in cts), f"/contacts?q={e}", "Find contacts", "info"))
    have_email = {(ct.party_type, ct.party_id) for ct in db.scalars(select(Contact))
                  if (ct.email_id or "").strip() and not ct.no_email}
    done = set()
    for c in contracts:
        k = (c.party_type, c.party_id)
        if c.status != "Cancelled" and k not in have_email and k not in done:
            done.add(k)
            p = logic.get_party(db, *k)
            sec.items.append(Item(f"noemail:{c.party_type}:{c.party_id}", f"{p.name if p else '?'}: no contact "
                                  "with an email address", "Customer emails can't be sent.",
                                  logic_party_url(p) if p else "", "Open client", "warn"))
    return sec


def check_coaches(db, sy) -> Section:
    sec = Section("coaches", "Coach capacity", "Coaches whose workload for the year is more than their "
                  "capacity, and committed hours with no coach.")
    for ch in db.scalars(select(Coach).where(Coach.active)):
        r = logic.coach_capacity(db, ch, sy)
        if not r.annual_net:
            sec.items.append(Item(f"nocap:{ch.id}:{sy}", f"{ch.coach_name}: no monthly capacity set for {sy}",
                                  "", f"/coaches/{ch.id}", "Open coach", "warn"))
        elif r.headroom < 0:
            sec.items.append(Item(f"over:{ch.id}:{sy}:{int(-r.headroom // 25)}",
                                  f"{ch.coach_name}: {-r.headroom:,.0f} hours over capacity",
                                  f"Workload {r.load:,.0f} h vs capacity {r.annual_net:,.0f} h (includes prep time).",
                                  f"/coaches/{ch.id}", "Open coach", "warn"))
    pooled, lines = logic.pooled_demand(db, sy)
    if pooled:
        sec.items.append(Item(f"pooled:{sy}:{int(pooled)}", f"{pooled:,.0f} committed hours have no coach",
                              f"{len(lines)} service line(s).", "/coaches", "Open coaches", "high",
                              dismissable=False))
    return sec


def check_email(db) -> Section:
    sec = Section("email", "Customer email", "Reminder drafts waiting for review, and messages that failed "
                  "to send.", perm="email")
    drafts = list(db.scalars(select(EmailMessage.id).where(EmailMessage.status == "Draft")))
    failed = list(db.scalars(select(EmailMessage.id).where(EmailMessage.status == "Failed")))
    if drafts:
        sec.items.append(Item("drafts", f"{len(drafts)} email draft(s) waiting", "Review and send them.",
                              "/emails", "Open outbox", "warn", dismissable=False))
    if failed:
        sec.items.append(Item("failed", f"{len(failed)} email(s) failed to send", "Check the error and retry.",
                              "/emails?status=Failed", "Open outbox", "high", dismissable=False))
    from . import mail
    if not mail.smtp_ready(db):
        sec.items.append(Item("nosmtp", "Email sending isn't set up", "Enter your mail server under Email "
                              "settings.", "/emails/settings", "Email settings", "info", dismissable=False))
    return sec


# ---------------------------------------------------------------------------
def collect(db, sy: str, can, show_dismissed: bool = False) -> list[Section]:
    today = logic.today()
    contracts = list(db.scalars(select(TrainingContract).where(TrainingContract.school_year == sy)))
    sections = [
        check_contract_money(db, sy, contracts),
        check_billing(db, sy, contracts, today),
        check_contract_workflow(db, sy, contracts, today),
        check_renewals(db, sy, contracts, today),
        check_delivery(db, sy, today),
        check_onboarding(db, sy, today),
        check_coaches(db, sy),
        check_sales(db, sy, today),
        check_contacts(db, sy, contracts),
        check_reference(db, sy),
        check_email(db),
        check_import_matches(db, sy),
    ]
    dismissed = {d.key for d in db.scalars(select(AttentionDismissal))}
    out = []
    for s in sections:
        if not can(s.perm):
            continue
        keep = []
        for it in s.items:
            if it.dismissable and it.key in dismissed:
                s.dismissed += 1
                if not show_dismissed:
                    continue
                it.severity = "done"
            keep.append(it)
        rank = {"high": 0, "warn": 1, "info": 2, "done": 3}
        s.items = sorted(keep, key=lambda i: rank[i.severity])
        out.append(s)
    return out


def count(db, sy, can) -> int:
    return sum(sum(1 for i in s.items if i.severity != "done") for s in collect(db, sy, can))
