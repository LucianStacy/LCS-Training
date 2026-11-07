"""Reports: each one is a table (with optional totals and a simple bar chart) that
shows on screen and downloads as a branded Excel workbook."""
from __future__ import annotations

import datetime as dt
import io
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select

from . import logic
from .config import BRAND
from .models import (MONTH_NAMES, SCHOOL_MONTHS, BillingInstallment, Coach, Lead, OnboardingChecklist, Quote,
                     ServiceDeliveryLog, TrainingContract, school_year_bounds)


@dataclass
class Col:
    key: str
    label: str
    fmt: str = "text"     # text | money | num | pct | date | int


@dataclass
class Result:
    title: str
    subtitle: str = ""
    cols: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    totals: Optional[dict] = None
    notes: list = field(default_factory=list)
    chart: Optional[dict] = None   # {"label_key", "series": [(key, label)]}
    summary: list = field(default_factory=list)  # [(label, value, fmt)]


@dataclass
class Report:
    key: str
    title: str
    description: str
    perm: str
    group: str
    fn: Callable
    params: list = field(default_factory=list)  # (name, label, kind, default) kind: int|date|select:a,b


def _party(db, o):
    return logic.party_name(db, o.party_type, o.party_id)


def _contracts(db, sy, include_cancelled=False):
    q = select(TrainingContract).where(TrainingContract.school_year == sy)
    rows = list(db.scalars(q))
    rows = [c for c in rows if include_cancelled or c.status != "Cancelled"]
    return sorted(rows, key=lambda c: _party(db, c).lower())


def _sum(rows, *keys):
    return {k: sum((r.get(k) or 0) for r in rows) for k in keys}


# ---------------------------------------------------------------------------
def r_renewals(db, sy, p):
    days = int(p.get("days") or 90)
    today = logic.today()
    nsy = f"{int(sy[:4]) + 1}-{str(int(sy[:4]) + 2)[2:]}"
    nxt = defaultdict(list)
    for q in db.scalars(select(Quote).where(Quote.school_year == nsy)):
        nxt[(q.party_type, q.party_id)].append(f"Quote {q.status}")
    for c in db.scalars(select(TrainingContract).where(TrainingContract.school_year == nsy)):
        nxt[(c.party_type, c.party_id)].append(f"Contract {c.status}")
    for ld in db.scalars(select(Lead).where(Lead.school_year == nsy)):
        nxt[(ld.party_type, ld.party_id)].append(f"Lead {ld.status}")
    rows = []
    for c in _contracts(db, sy):
        left = (c.contract_end_date - today).days
        if c.status not in ("Pending Assignment", "Active", "Renewal Pending", "Expired") or left > days:
            continue
        u = logic.contract_utilization(c, today)
        mc = c.main_contact
        rows.append({"client": _party(db, c), "status": c.status, "end": c.contract_end_date, "left": left,
                     "value": c.total_contract_value, "delivered": u.pct, "elapsed": u.elapsed,
                     "next": ", ".join(nxt.get((c.party_type, c.party_id), [])) or "Nothing yet",
                     "contact": mc.full_name if mc else "", "email": mc.email_id if mc else "",
                     "_link": f"/contracts/{c.id}"})
    rows.sort(key=lambda r: r["left"])
    res = Result(f"Renewals: {sy} contracts ending within {days} days", f"Next year's activity is for {nsy}.",
                 [Col("client", "Client"), Col("status", "Status"), Col("end", "Ends", "date"),
                  Col("left", "Days left", "int"), Col("value", "Value", "money"), Col("delivered", "Delivered", "pct"),
                  Col("elapsed", "Term gone", "pct"), Col("next", f"{nsy} so far"), Col("contact", "Main contact"),
                  Col("email", "Email")], rows, totals={"client": "Total", **_sum(rows, "value")})
    todo = [r for r in rows if r["next"] == "Nothing yet"]
    res.summary = [("Contracts", len(rows), "int"), ("Value up for renewal", sum(r["value"] for r in rows), "money"),
                   (f"No {nsy} activity yet", len(todo), "int"),
                   ("Value with no activity", sum(r["value"] for r in todo), "money")]
    return res


def r_delivery(db, sy, p):
    only = p.get("show") or "all"
    rows = []
    for c in _contracts(db, sy):
        u = logic.contract_utilization(c)
        for x in u.lines:
            ln = x["line"]
            q = ln.contracted_quantity or 0
            if only == "behind" and not (x["countable"] and x["pct"] is not None and x["pct"] < u.elapsed - 0.25):
                continue
            rows.append({"client": _party(db, c), "service": ln.service_type.service_name,
                         "unit": ln.service_type.delivery_unit,
                         "coach": ln.assigned_coach.coach_name if ln.assigned_coach else "(none)",
                         "contracted": q, "delivered": x["done"], "scheduled": x["scheduled"],
                         "remaining": max(q - x["done"] - x["scheduled"], 0), "pct": x["pct"],
                         "elapsed": u.elapsed, "hours": ln.committed_hours, "_link": f"/contracts/{c.id}?tab=services"})
    return Result(f"Delivery by service line, {sy}", "Delivered = completed sessions/audits; scheduled = booked but "
                  "not yet held; remaining = not yet booked.",
                  [Col("client", "Client"), Col("service", "Service"), Col("unit", "Unit"), Col("coach", "Coach"),
                   Col("contracted", "Contracted", "num"), Col("delivered", "Delivered", "num"),
                   Col("scheduled", "Scheduled", "num"), Col("remaining", "Not booked", "num"),
                   Col("pct", "% delivered", "pct"), Col("elapsed", "Term gone", "pct"),
                   Col("hours", "Coach hours", "num")], rows,
                  totals={"client": "Total", **_sum(rows, "hours")})


def r_revenue_month(db, sy, p):
    start, end = school_year_bounds(sy)
    insts = [i for c in _contracts(db, sy) for i in c.billing_schedule if i.status != "Cancelled"]
    by = defaultdict(lambda: defaultdict(float))
    for i in insts:
        k = (i.due_date.year, i.due_date.month)
        b = by[k]
        b["scheduled"] += i.amount
        b[i.status.lower()] += i.amount
        for cat in logic.CATS:
            b[cat] += getattr(i, f"amount_{cat}") or 0
    months = [((start.year if m >= 7 else end.year), m) for m in SCHOOL_MONTHS]
    months += sorted(k for k in by if k not in months)
    rows = []
    for y, m in months:
        b = by.get((y, m), {})
        rows.append({"month": f"{MONTH_NAMES[m]} {y}", **{k: b.get(k, 0) for k in
                     ["scheduled", "planned", "invoiced", "paid"] + logic.CATS}})
    cols = [Col("month", "Month"), Col("scheduled", "Scheduled", "money"), Col("planned", "Not yet invoiced", "money"),
            Col("invoiced", "Invoiced (unpaid)", "money"), Col("paid", "Paid", "money")]
    cols += [Col(k, logic.CAT_LABEL[k], "money") for k in logic.CATS]
    t = _sum(rows, *[c.key for c in cols[1:]])
    res = Result(f"Revenue by month, {sy}", "From the billing schedules of every contract (cancelled installments "
                 "left out). Categories split each installment the way the contract is split.", cols, rows,
                 totals={"month": "Total", **t},
                 chart={"label_key": "month", "series": [("paid", "Paid"), ("invoiced", "Invoiced"),
                                                         ("planned", "Not yet invoiced")]})
    res.summary = [("Scheduled", t["scheduled"], "money"), ("Paid", t["paid"], "money"),
                   ("Invoiced, unpaid", t["invoiced"], "money"), ("Not yet invoiced", t["planned"], "money")]
    return res


def r_receivables(db, sy, p):
    today = logic.today()
    rows = []
    for c in _contracts(db, sy):
        for i in c.billing_schedule:
            if i.status != "Invoiced":
                continue
            late = (today - i.due_date).days
            bucket = ("Not due yet" if late <= 0 else "1-30 days" if late <= 30 else "31-60 days" if late <= 60
                      else "61-90 days" if late <= 90 else "Over 90 days")
            rows.append({"client": _party(db, c), "ref": i.invoice_ref or "", "due": i.due_date, "amount": i.amount,
                         "late": max(late, 0), "bucket": bucket, "po": c.po_number or "",
                         "email": c.invoice_email or "", "_link": f"/contracts/{c.id}?tab=billing"})
    rows.sort(key=lambda r: -r["late"])
    buckets = Counter()
    for r in rows:
        buckets[r["bucket"]] += r["amount"]
    res = Result(f"Unpaid invoices, {sy}", "Installments marked Invoiced but not Paid, oldest first.",
                 [Col("client", "Client"), Col("ref", "Invoice #"), Col("due", "Due", "date"),
                  Col("amount", "Amount", "money"), Col("late", "Days past due", "int"), Col("bucket", "Age"),
                  Col("po", "P.O."), Col("email", "Invoices go to")], rows,
                 totals={"client": "Total", **_sum(rows, "amount")})
    res.summary = [(b, buckets[b], "money") for b in ["Not due yet", "1-30 days", "31-60 days", "61-90 days",
                                                      "Over 90 days"]]
    return res


def r_revenue_client(db, sy, p):
    rows = []
    for c in _contracts(db, sy):
        billed = sum(i.amount for i in c.billing_schedule if i.status in ("Invoiced", "Paid"))
        paid = sum(i.amount for i in c.billing_schedule if i.status == "Paid")
        rows.append({"client": _party(db, c), "status": c.status,
                     **{k: getattr(c, f"amount_{k}") or 0 for k in logic.CATS},
                     "total": c.total_contract_value, "billed": billed, "paid": paid,
                     "remaining": c.total_contract_value - billed, "_link": f"/contracts/{c.id}?tab=billing"})
    rows.sort(key=lambda r: -r["total"])
    cols = [Col("client", "Client"), Col("status", "Status")] + [Col(k, logic.CAT_LABEL[k], "money") for k in logic.CATS]
    cols += [Col("total", "Contract value", "money"), Col("billed", "Invoiced", "money"), Col("paid", "Paid", "money"),
             Col("remaining", "Not yet invoiced", "money")]
    t = _sum(rows, *[c.key for c in cols[2:]])
    res = Result(f"Revenue by client and category, {sy}", "", cols, rows, totals={"client": "Total", **t})
    res.summary = [(logic.CAT_LABEL[k], t[k], "money") for k in logic.CATS]
    if t["other"] > 0.5 * (t["total"] or 1):
        res.notes.append("Most revenue is still under Other (imported contracts have no category split). Split them "
                         "from the Needs attention page.")
    return res


def r_coach_workload(db, sy, p):
    rows, summary = [], []
    for ch in db.scalars(select(Coach).where(Coach.active).order_by(Coach.coach_name)):
        r = logic.coach_capacity(db, ch, sy)
        for m in r.months:
            rows.append({"coach": ch.coach_name, "month": f"{MONTH_NAMES[m['month']]} {m['year']}",
                         "capacity": m["capacity"], "off": m["unavailable"], "net": m["net"],
                         "delivered": m["delivered"], "used": (m["delivered"] / m["net"]) if m["net"] else None,
                         "_link": f"/coaches/{ch.id}"})
        summary.append((f"{ch.coach_name}: headroom (h)", r.headroom, "num"))
    res = Result(f"Coach workload by month, {sy}", "Capacity less time off, against hours actually delivered "
                 "(completed sessions). Annual headroom compares capacity with contracted workload.",
                 [Col("coach", "Coach"), Col("month", "Month"), Col("capacity", "Capacity", "num"),
                  Col("off", "Time off", "num"), Col("net", "Available", "num"), Col("delivered", "Delivered", "num"),
                  Col("used", "% used", "pct")], rows)
    res.summary = summary
    return res


def r_pipeline(db, sy, p):
    rows = []
    for q in db.scalars(select(Quote).where(Quote.school_year == sy).order_by(Quote.quote_date.desc())):
        rows.append({"number": q.quote_number, "client": _party(db, q), "date": q.quote_date,
                     "kind": "Amendment" if q.is_amendment else "New", "status": q.status, "total": q.grand_total,
                     "valid": q.valid_until, "_link": f"/quotes/{q.id}"})
    by = defaultdict(lambda: [0, 0.0])
    for r in rows:
        by[r["status"]][0] += 1
        by[r["status"]][1] += r["total"] or 0
    decided = by["Accepted"][0] + by["Declined"][0] + by["Expired"][0]
    res = Result(f"Quotes, {sy}", "Every quote written for the year.",
                 [Col("number", "Quote"), Col("client", "Client"), Col("date", "Date", "date"), Col("kind", "Type"),
                  Col("status", "Status"), Col("total", "Total", "money"), Col("valid", "Valid until", "date")], rows,
                 totals={"number": "Total", **_sum(rows, "total")})
    res.summary = [(f"{s} ({by[s][0]})", by[s][1], "money") for s in ["Draft", "Sent", "Accepted", "Declined", "Expired"]
                   if by[s][0]]
    if decided:
        res.summary.append(("Win rate (decided quotes)", by["Accepted"][0] / decided, "pct"))
    leads = Counter(ld.status for ld in db.scalars(select(Lead).where(Lead.school_year == sy)))
    if leads:
        res.notes.append("Leads: " + ", ".join(f"{k} {v}" for k, v in leads.most_common()))
    return res


def r_deliveries(db, sy, p):
    start, end = school_year_bounds(sy)
    d0 = _date(p.get("start")) or start
    d1 = _date(p.get("end")) or end
    status = p.get("status") or "Completed"
    rows = []
    q = select(ServiceDeliveryLog).where(ServiceDeliveryLog.date_performed >= d0, ServiceDeliveryLog.date_performed <= d1)
    if status != "All":
        q = q.where(ServiceDeliveryLog.status == status)
    for d in db.scalars(q.order_by(ServiceDeliveryLog.date_performed)):
        rows.append({"date": d.date_performed, "client": _party(db, d.contract),
                     "service": d.contract_service.service_type.service_name if d.contract_service else "",
                     "subject": d.subject or "", "coach": d.coach.coach_name if d.coach else "",
                     "mode": d.mode or "", "status": d.status, "hours": d.duration or 0, "units": d.units or 0,
                     "_link": f"/deliveries/{d.id}"})
    by_service = Counter()
    for r in rows:
        by_service[r["service"] or "(no line)"] += r["units"]
    res = Result(f"Trainings, audits and consults: {status.lower()}, {d0:%m/%d/%Y} to {d1:%m/%d/%Y}", "",
                 [Col("date", "Date", "date"), Col("client", "Client"), Col("service", "Service"),
                  Col("subject", "Subject"), Col("coach", "Coach"), Col("mode", "Mode"), Col("status", "Status"),
                  Col("hours", "Hours", "num"), Col("units", "Units", "num")], rows,
                 totals={"date": "Total", **_sum(rows, "hours", "units")})
    res.summary = [(k, v, "num") for k, v in by_service.most_common(6)]
    return res


def r_onboarding(db, sy, p):
    today = logic.today()
    rows = []
    for cl in db.scalars(select(OnboardingChecklist).join(TrainingContract).where(TrainingContract.school_year == sy)):
        c = cl.contract
        done = sum(1 for t in cl.tasks if t.status in ("Done", "N/A"))
        open_ = [t for t in cl.tasks if t.status not in ("Done", "N/A")]
        rows.append({"client": _party(db, c), "status": c.status, "started": cl.generated_date,
                     "age": (today - cl.generated_date).days, "done": done, "total": len(cl.tasks),
                     "pct": done / len(cl.tasks) if cl.tasks else None,
                     "next": open_[0].step if open_ else "All done", "_link": f"/contracts/{c.id}?tab=onboarding"})
    rows.sort(key=lambda r: (r["pct"] if r["pct"] is not None else 1, -r["age"]))
    return Result(f"Onboarding progress, {sy}", "", [
        Col("client", "Client"), Col("status", "Contract status"), Col("started", "Checklist started", "date"),
        Col("age", "Days since", "int"), Col("done", "Done", "int"), Col("total", "Tasks", "int"),
        Col("pct", "% done", "pct"), Col("next", "Next open task")], rows)


def _date(v):
    try:
        return dt.date.fromisoformat(v) if v else None
    except ValueError:
        return None


REPORTS = [
    Report("renewals", "Renewals", "Contracts ending soon, what's been delivered, and whether next year has started.",
           "view_sales", "Sales", r_renewals, [("days", "Ending within (days)", "int", "90")]),
    Report("pipeline", "Quotes and win rate", "Every quote for the year by status, with totals and win rate.",
           "view_sales", "Sales", r_pipeline),
    Report("revenue-month", "Revenue by month", "Scheduled, invoiced and paid by month, split by income category.",
           "view_money", "Money", r_revenue_month),
    Report("revenue-client", "Revenue by client", "Each contract's value by category, with invoiced and paid.",
           "view_money", "Money", r_revenue_client),
    Report("receivables", "Unpaid invoices", "Invoiced but unpaid installments, by age.", "view_money", "Money",
           r_receivables),
    Report("delivery", "Delivery by service line", "Contracted vs delivered vs scheduled for every service line.",
           "view_ops", "Delivery", r_delivery, [("show", "Show", "select:all,behind", "all")]),
    Report("deliveries", "Trainings, audits and consults", "Every session in a date range, with hours.",
           "view_ops", "Delivery", r_deliveries, [("start", "From", "date", ""), ("end", "To", "date", ""),
                                                  ("status", "Status", "select:Completed,Scheduled,Cancelled,All",
                                                   "Completed")]),
    Report("onboarding", "Onboarding progress", "Checklist progress for every contract.", "view_ops", "Delivery",
           r_onboarding),
    Report("coach-workload", "Coach workload by month", "Available hours vs delivered hours for each coach.",
           "view_ops", "Delivery", r_coach_workload),
]
BY_KEY = {r.key: r for r in REPORTS}


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
FMT = {"money": '"$"#,##0.00;[Red]-"$"#,##0.00', "num": "#,##0.##", "pct": "0%", "date": "mm/dd/yyyy", "int": "0"}


def to_xlsx(res: Result, school_year: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    purple = PatternFill("solid", fgColor=BRAND["purple"])
    tint = PatternFill("solid", fgColor="F1EBF6")
    ws.append([res.title])
    ws["A1"].font = Font(name="Montserrat", bold=True, size=14, color=BRAND["purple"])
    ws.append([f"LCS-Training · {school_year} · run {logic.today():%m/%d/%Y}" + (f" · {res.subtitle}" if res.subtitle else "")])
    ws["A2"].font = Font(name="Calibri", italic=True, color=BRAND["gray"])
    r0 = 3
    if res.summary:
        for label, val, fmt in res.summary:
            ws.append([label, val])
            ws.cell(ws.max_row, 2).number_format = FMT.get(fmt, "General")
            ws.cell(ws.max_row, 1).font = Font(bold=True)
        r0 = ws.max_row + 1
    ws.append([])
    hdr = r0 + 1
    ws.append([c.label for c in res.cols])
    for i in range(1, len(res.cols) + 1):
        cell = ws.cell(hdr, i)
        cell.font = Font(name="Calibri", bold=True, color="FFFFFF")
        cell.fill = purple
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in res.rows:
        ws.append([row.get(c.key) for c in res.cols])
        for i, c in enumerate(res.cols, 1):
            if c.fmt in FMT:
                ws.cell(ws.max_row, i).number_format = FMT[c.fmt]
    last_data = ws.max_row
    if res.totals and res.rows:
        ws.append([res.totals.get(c.key, None) for c in res.cols])
        for i, c in enumerate(res.cols, 1):
            cell = ws.cell(ws.max_row, i)
            cell.font, cell.fill = Font(bold=True), tint
            if c.fmt in FMT:
                cell.number_format = FMT[c.fmt]
    for i, c in enumerate(res.cols, 1):
        width = max([len(c.label)] + [len(_txt(r.get(c.key), c.fmt)) for r in res.rows[:500]] + [8])
        ws.column_dimensions[get_column_letter(i)].width = min(width + 2, 50)
    ws.freeze_panes = ws.cell(hdr + 1, 1)
    if res.rows:
        ws.auto_filter.ref = f"A{hdr}:{get_column_letter(len(res.cols))}{last_data}"
    for n in res.notes:
        ws.append([])
        ws.append([n])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _txt(v, fmt):
    if v is None:
        return ""
    if fmt == "money":
        return f"${v:,.2f}"
    if fmt == "date":
        return "00/00/0000"
    return str(v)
