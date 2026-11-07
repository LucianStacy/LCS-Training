"""Business rules: quote pricing, contract creation & amendments, billing
schedules, onboarding generation, utilization and coach capacity."""
from __future__ import annotations

import calendar
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func, select

from .models import (PARTY_DISTRICT, PARTY_SELPA, BillingInstallment, Coach, ConsultSession,
                     ContractAmendment, ContractPaperwork, ContractService, LcsDistrict, LcsSelpa,
                     OnboardingChecklist, OnboardingTask, PriceItem, Quote, ServiceDeliveryLog,
                     ServiceType, Setting, TaskTemplate, TrainingContract, ZoneRate, school_year_bounds)
from .seed import LEAD_STEPS, PAPERWORK_STEPS

CATS = ["consultation", "training", "travel", "zone", "other"]
CAT_LABEL = {"consultation": "Consultation", "training": "Training", "travel": "Travel",
             "zone": "Zone", "other": "Other"}
ACTIVE_CONTRACT = ("Pending Assignment", "Active", "Renewal Pending")


def today() -> dt.date:
    return dt.date.today()


def r2(x: float) -> float:
    return round((x or 0) + 0.0, 2)


# ---------------------------------------------------------------------------
# Parties
# ---------------------------------------------------------------------------
def get_party(db, party_type: Optional[str], party_id: Optional[int]):
    if not party_type or not party_id:
        return None
    model = LcsSelpa if party_type == PARTY_SELPA else LcsDistrict
    return db.get(model, party_id)


def party_name(db, party_type, party_id) -> str:
    p = get_party(db, party_type, party_id)
    return p.name if p else "(no client)"


def party_key(obj) -> str:
    return f"{obj.party_type}:{obj.id}"


def parse_party_key(key: str) -> tuple[Optional[str], Optional[int]]:
    if not key or ":" not in key:
        return None, None
    t, i = key.rsplit(":", 1)
    if t not in (PARTY_SELPA, PARTY_DISTRICT):
        return None, None
    return t, int(i)


def all_parties(db) -> list:
    rows = list(db.scalars(select(LcsDistrict))) + list(db.scalars(select(LcsSelpa)))
    return sorted(rows, key=lambda p: p.name.lower())


def get_setting(db, key, default=""):
    s = db.get(Setting, key)
    return s.value if s and s.value is not None else default


def set_setting(db, key, value):
    s = db.get(Setting, key)
    if s:
        s.value = value
    else:
        db.add(Setting(key=key, value=value))


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
def add_months(d: dt.date, n: int) -> dt.date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def month_count(start: dt.date, end: dt.date) -> int:
    """Number of billing months from start to end inclusive (by calendar month)."""
    if end < start:
        return 1
    return (end.year - start.year) * 12 + end.month - start.month + 1


def working_days(start: dt.date, end: dt.date) -> int:
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += dt.timedelta(days=1)
    return n


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------
def next_quote_number(db, d: dt.date) -> str:
    prefix = d.strftime("%Y%m%d")
    n = db.scalar(select(func.count()).select_from(Quote).where(Quote.quote_number.like(f"{prefix}-%"))) or 0
    while True:
        n += 1
        num = f"{prefix}-{n:02d}"
        if not db.scalar(select(Quote.id).where(Quote.quote_number == num)):
            return num


def zone_lookup(db, school_year: str, miles: Optional[float]):
    """Same rule as the worksheet's VLOOKUP(...,TRUE): the highest band at or below the mileage.
    Returns (ZoneRate|None, warning|None)."""
    if not miles:
        return None, None
    bands = list(db.scalars(select(ZoneRate).where(ZoneRate.school_year == school_year).order_by(ZoneRate.miles_from)))
    if not bands:
        return None, f"No zone table for {school_year}; zone charges are $0. Load one on the Price List screen."
    chosen = None
    for b in bands:
        if b.miles_from <= miles:
            chosen = b
    if chosen is None:
        return bands[0], (f"{miles:g} miles is below the first zone band ({bands[0].miles_from:g} mi). "
                          f"Used Zone {bands[0].zone_no}; the spreadsheet showed #N/A here, so confirm.")
    return chosen, None


@dataclass
class QuoteResult:
    warnings: list[str] = field(default_factory=list)
    zone: Optional[ZoneRate] = None


def compute_quote(db, q: Quote) -> QuoteResult:
    res = QuoteResult()
    zone, warn = zone_lookup(db, q.school_year, q.one_way_mileage)
    res.zone = zone
    if warn:
        res.warnings.append(warn)
    group_sums: dict[str, float] = defaultdict(float)
    deferred = []
    for it in q.items:
        p = it.price_item
        if p is None:  # unlisted service: manual rate
            it.amount = r2((it.qty or 0) * (it.rate or 0))
            continue
        it.income_category = p.income_category
        m = p.method
        if m == "percent_of_group":
            deferred.append(it)
            continue
        if m == "unit":
            it.rate = p.rate
            amt = (it.qty or 0) * p.rate
            if p.bulk_min_qty and p.bulk_discount_pct and (it.qty or 0) >= p.bulk_min_qty:
                amt *= 1 - p.bulk_discount_pct
                res.warnings.append(f"{p.item_name}: {p.bulk_discount_pct:.0%} multi-participant discount applied "
                                    f"({it.qty:g} participants, {p.bulk_min_qty:g}+ qualifies).")
        elif m == "tiered":
            if not q.student_tier:
                res.warnings.append(f"{p.item_name}: pick the client's size tier to price this line.")
            it.rate = p.tier_rate(q.student_tier)
            amt = (it.qty or 0) * it.rate
        elif m == "two_factor":
            it.rate = p.rate
            amt = (it.qty or 0) * (it.factor or 0) * p.rate
            if it.qty and not it.factor:
                res.warnings.append(f"{p.item_name}: enter {p.factor_label or 'the second number'}.")
        elif m == "discount":
            it.rate = p.rate
            amt = -(it.qty or 0) * p.rate
        elif m in ("zone", "zone_additional"):
            if not zone:
                if it.qty:
                    res.warnings.append(f"{p.item_name}: enter one-way mileage to price the zone charge.")
                it.rate, amt = 0, 0
            else:
                it.rate = zone.zone_charge if m == "zone" else zone.additional_person_charge
                amt = (it.qty or 0) * it.rate
        else:
            it.rate, amt = p.rate, (it.qty or 0) * p.rate
        it.amount = r2(amt)
        if p.group_key:
            group_sums[p.group_key] += it.amount
    for it in deferred:
        p = it.price_item
        it.rate = p.rate
        it.amount = r2(-group_sums.get(p.group_key or "", 0) * p.rate * (1 if it.qty else 0))
        if it.qty and not group_sums.get(p.group_key or ""):
            res.warnings.append(f"{p.item_name}: no lines in group '{p.group_key}' to discount.")
    totals = defaultdict(float)
    for it in q.items:
        totals[(it.income_category or "Other").lower()] += it.amount
    for c in CATS:
        setattr(q, f"total_{c}", r2(totals.get(c, 0)))
    q.grand_total = r2(sum(totals.values()))
    return res


def accept_quote(db, q: Quote, user: str) -> TrainingContract:
    """Quote accepted -> new contract (or, for an amendment quote, an amendment to the existing one)."""
    q.status = "Accepted"
    if q.lead:
        q.lead.status = "Won"
        _mark_steps(q.lead.steps, ["Quote accepted"], today())
    if q.is_amendment and q.amends_contract_id:
        c = db.get(TrainingContract, q.amends_contract_id)
        eff = today()
        for line in _service_lines_from_quote(db, q):
            line.effective_date = eff
            c.services.append(line)
        for cat in CATS:
            setattr(c, f"amount_{cat}", r2(getattr(c, f"amount_{cat}") + getattr(q, f"total_{cat}")))
        c.total_contract_value = r2(c.total_contract_value + q.grand_total)
        c.amendments.append(ContractAmendment(
            amendment_date=eff, changed_by=user, value_change=q.grand_total,
            description=f"Amendment quote {q.quote_number} accepted: "
                        + "; ".join(f"{i.description or (i.price_item and i.price_item.item_name)} x{i.qty:g}"
                                    for i in q.items if i.qty),
            reason="Amendment quote"))
        generate_billing_schedule(db, c, keep_issued=True)
        return c

    start, end = school_year_bounds(q.school_year)
    first_bill = max(start, dt.date(today().year, today().month, 1))
    if first_bill > end:
        first_bill = start
    c = TrainingContract(
        party_type=q.party_type, party_id=q.party_id, quote_id=q.id, school_year=q.school_year,
        contract_start_date=start, contract_end_date=end, status="Draft",
        total_contract_value=q.grand_total, billing_frequency="Monthly",
        billing_start_date=first_bill, billing_end_date=dt.date(end.year, end.month, 1),
        **{f"amount_{cat}": getattr(q, f"total_{cat}") for cat in CATS})
    c.number_of_installments = month_count(c.billing_start_date, c.billing_end_date)
    c.services = _service_lines_from_quote(db, q)
    for ln in c.services:
        ln.effective_date = start
    c.paperwork = [ContractPaperwork(step=s, sort_order=i) for i, s in enumerate(PAPERWORK_STEPS)]
    _mark_steps(c.paperwork, ["Quote signed & returned"], today())
    db.add(c)
    db.flush()
    generate_billing_schedule(db, c)
    return c


def _service_lines_from_quote(db, q: Quote) -> list[ContractService]:
    lines: dict[int, ContractService] = {}
    out = []
    for it in q.items:
        p = it.price_item
        if not it.qty or not p or not p.service_type_id or p.method in ("discount", "percent_of_group", "zone",
                                                                        "zone_additional"):
            continue
        qty = it.qty * (it.factor or 1) if p.method == "two_factor" else it.qty
        hours = p.hours_for(q.student_tier)
        if hours is None:
            hours = p.service_type.default_hours_per_unit if p.service_type else 0
        desc = p.item_name + (f" ({p.variant})" if p.variant else "")
        key = p.service_type_id
        if key in lines and lines[key].hours_per_unit == hours:
            lines[key].contracted_quantity += qty
            lines[key].amount = r2((lines[key].amount or 0) + it.amount)
            lines[key].description += f"; {desc}"
            continue
        ln = ContractService(service_type_id=p.service_type_id, price_item_id=p.id, description=desc,
                             contracted_quantity=qty, hours_per_unit=hours, amount=it.amount,
                             frequency="per contract term", effective_date=today())
        lines[key] = ln
        out.append(ln)
    return out


def _mark_steps(steps, names, when):
    for s in steps:
        if s.step in names and s.status != "Done":
            s.status, s.step_date = "Done", when


# ---------------------------------------------------------------------------
# Billing schedule  (Blueprint 2.3: compute installments, amendment reset)
# ---------------------------------------------------------------------------
def reconcile_categories(c: TrainingContract) -> None:
    """Make category amounts add up to the contract value; any difference goes to Other."""
    diff = r2(c.total_contract_value - sum(getattr(c, f"amount_{k}") or 0 for k in CATS))
    if diff:
        c.amount_other = r2((c.amount_other or 0) + diff)


def schedule_dates(c: TrainingContract) -> list[dt.date]:
    start = c.billing_start_date or c.contract_start_date
    end = c.billing_end_date or c.contract_end_date
    months = month_count(start, end)
    if c.billing_frequency == "Annual-Upfront":
        return [start]
    if c.billing_frequency == "Quarterly":
        n = c.number_of_installments or -(-months // 3)
        return [add_months(start, 3 * i) for i in range(n)]
    n = c.number_of_installments or months
    return [add_months(start, i) for i in range(n)]


def generate_billing_schedule(db, c: TrainingContract, keep_issued: bool = True) -> list[str]:
    """(Re)build Planned installments. Invoiced/Paid rows are kept and the remaining balance is spread
    over the remaining dates - the Blueprint's amendment 'billing reset'."""
    msgs = []
    reconcile_categories(c)
    if c.billing_frequency == "Custom":
        return ["Custom billing: edit the installments by hand; nothing was regenerated."]
    kept = [i for i in c.billing_schedule if keep_issued and i.status in ("Invoiced", "Paid")]
    for i in list(c.billing_schedule):
        if i not in kept:
            c.billing_schedule.remove(i)
            db.delete(i)
    last_kept = max((i.due_date for i in kept), default=None)
    dates = [d for d in schedule_dates(c) if last_kept is None or d > last_kept]
    remaining = r2(c.total_contract_value - sum(i.amount for i in kept))
    rem_cat = {k: r2((getattr(c, f"amount_{k}") or 0) - sum(getattr(i, f"amount_{k}") or 0 for i in kept))
               for k in CATS}
    if not dates:
        if abs(remaining) < 0.005:
            return msgs
        dates = [add_months(dt.date(today().year, today().month, 1), 1)]
        msgs.append("All scheduled dates were already invoiced; the remaining balance was put on one extra installment.")
    n = len(dates)
    each = r2(remaining / n)
    cat_each = {k: r2(v / n) for k, v in rem_cat.items()}
    for idx, d in enumerate(dates):
        last = idx == n - 1
        inst = BillingInstallment(due_date=d, status="Planned",
                                  amount=r2(remaining - each * (n - 1)) if last else each)
        for k in CATS:
            setattr(inst, f"amount_{k}", r2(rem_cat[k] - cat_each[k] * (n - 1)) if last else cat_each[k])
        c.billing_schedule.append(inst)
    if kept:
        msgs.append(f"Kept {len(kept)} invoiced/paid installment(s); spread ${remaining:,.2f} over {n} remaining.")
    return msgs


def apply_value_change(db, c: TrainingContract, new_total: float, category: str, user: str, reason: str,
                       description: str, when: dt.date) -> None:
    delta = r2(new_total - c.total_contract_value)
    c.total_contract_value = r2(new_total)
    setattr(c, f"amount_{category}", r2((getattr(c, f"amount_{category}") or 0) + delta))
    c.amendments.append(ContractAmendment(amendment_date=when, description=description or
                                          f"Contract value changed by ${delta:,.2f}", changed_by=user,
                                          reason=reason, value_change=delta))
    generate_billing_schedule(db, c, keep_issued=True)


def amend_service_quantity(db, line: ContractService, new_qty: float, when: dt.date, user: str,
                           reason: str) -> ContractService:
    """Effective-dated change: close the old row and add a replacement (history is never overwritten)."""
    c = line.contract
    line.end_date = when
    new = ContractService(service_type_id=line.service_type_id, price_item_id=line.price_item_id,
                          description=line.description, contracted_quantity=new_qty, frequency=line.frequency,
                          hours_per_unit=line.hours_per_unit, amount=line.amount, effective_date=when,
                          assigned_coach_id=line.assigned_coach_id, notes=line.notes)
    c.services.append(new)
    db.flush()
    for d in list(line.deliveries):  # keep delivery history attached to the live row
        d.contract_service_id = new.id
    c.amendments.append(ContractAmendment(
        amendment_date=when, changed_by=user, reason=reason,
        description=f"{line.service_type.service_name}: quantity {line.contracted_quantity:g} -> {new_qty:g}"))
    return new


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------
def generate_onboarding(db, c: TrainingContract) -> int:
    cl = db.scalar(select(OnboardingChecklist).where(OnboardingChecklist.contract_id == c.id))
    if not cl:
        cl = OnboardingChecklist(contract_id=c.id, generated_date=today(), status="In Progress")
        db.add(cl)
        db.flush()
    have = {t.template_item_id for t in cl.tasks}
    svc_ids = {s.service_type_id for s in c.current_services}
    added = 0
    order = len(cl.tasks)
    for tpl in db.scalars(select(TaskTemplate).order_by(TaskTemplate.sort_order)):
        if tpl.service_type_id is not None and tpl.service_type_id not in svc_ids:
            continue
        for item in tpl.items:
            if item.id in have:
                continue
            cl.tasks.append(OnboardingTask(step=item.task, stage=item.stage, template_item_id=item.id,
                                           service_type_id=tpl.service_type_id, sort_order=order))
            order += 1
            added += 1
    refresh_onboarding_status(cl)
    return added


def refresh_onboarding_status(cl: OnboardingChecklist) -> None:
    if cl.tasks and all(t.status in ("Done", "N/A") for t in cl.tasks):
        cl.status = "Complete"
    else:
        cl.status = "In Progress"


def checklist_for(db, c: TrainingContract) -> Optional[OnboardingChecklist]:
    return db.scalar(select(OnboardingChecklist).where(OnboardingChecklist.contract_id == c.id))


def on_status_change(db, c: TrainingContract, old: str) -> list[str]:
    msgs = []
    if c.status in ACTIVE_CONTRACT and old == "Draft":
        n = generate_onboarding(db, c)
        if n:
            msgs.append(f"Onboarding checklist generated with {n} task(s) for the contracted services.")
        from . import mail  # the welcome email is drafted on this event only (never for imported contracts)
        if mail.welcome_on_signing(db, c):
            msgs.append("A welcome email is waiting in the outbox.")
    msgs += maybe_activate(db, c)
    return msgs


def maybe_activate(db, c: TrainingContract) -> list[str]:
    """Blueprint 2.3: once every service line has a coach, Pending Assignment -> Active."""
    cur = c.current_services
    if c.status == "Pending Assignment" and cur and all(s.assigned_coach_id for s in cur):
        c.status = "Active"
        cl = checklist_for(db, c)
        if cl:
            _mark_steps(cl.tasks, ["Primary coach assigned"], today())
        return ["Every service line has a coach, so the contract is now Active."]
    return []


# ---------------------------------------------------------------------------
# Utilization & renewal risk
# ---------------------------------------------------------------------------
def delivered_units(line: ContractService) -> float:
    total = 0.0
    for d in line.deliveries:
        if d.status == "Completed":
            total += (d.duration or 0) if line.service_type.delivery_unit == "hour" else (d.units or 0)
    return total


def scheduled_units(line: ContractService) -> float:
    return sum((d.duration or 0) if line.service_type.delivery_unit == "hour" else (d.units or 0)
               for d in line.deliveries if d.status in ("Scheduled", "Planned"))


@dataclass
class Utilization:
    lines: list
    pct: Optional[float]
    elapsed: float
    risk: str


def contract_utilization(c: TrainingContract, on: Optional[dt.date] = None) -> Utilization:
    on = on or today()
    lines, pcts = [], []
    for s in c.current_services:
        done = delivered_units(s)
        sched = scheduled_units(s)
        pct = (done / s.contracted_quantity) if s.contracted_quantity else None
        # Only services delivered steadily through the year count toward "behind on delivery";
        # audits, data meetings, subscriptions and programs follow their own calendar.
        countable = (s.service_type.kind in ("Consult", "Training") and s.contracted_quantity >= 2
                     and s.service_type.delivery_unit in ("hour", "session"))
        if countable and pct is not None:
            pcts.append(min(pct, 1.5))
        lines.append({"line": s, "done": done, "scheduled": sched, "pct": pct, "countable": bool(countable)})
    span = (c.contract_end_date - c.contract_start_date).days or 1
    elapsed = min(max((on - c.contract_start_date).days / span, 0), 1)
    pct = sum(pcts) / len(pcts) if pcts else None
    risk = ""
    if c.status in ACTIVE_CONTRACT:
        if (c.contract_end_date - on).days <= 60:
            risk = "Renewal window"
        if pct is not None and elapsed > 0.25 and pct < elapsed - 0.25:
            risk = "Behind on delivery" + (" / renewal window" if risk else "")
    return Utilization(lines, pct, elapsed, risk)


# ---------------------------------------------------------------------------
# Coach capacity  (Blueprint 1.5)
# ---------------------------------------------------------------------------
@dataclass
class CapacityRow:
    coach: Coach
    months: list = field(default_factory=list)  # dicts: month, capacity, unavailable, net, delivered
    annual_net: float = 0
    committed: float = 0
    delivered: float = 0
    load: float = 0
    headroom: float = 0
    remaining_net: float = 0
    remaining_load: float = 0
    remaining_headroom: float = 0
    lines: list = field(default_factory=list)


def coach_capacity(db, coach: Coach, school_year: str, on: Optional[dt.date] = None) -> CapacityRow:
    on = on or today()
    sy_start, sy_end = school_year_bounds(school_year)
    row = CapacityRow(coach)
    caps = {c.month: c.hours for c in coach.capacity if c.school_year == school_year}
    delivered_by_month = defaultdict(float)
    logs = db.scalars(select(ServiceDeliveryLog).where(ServiceDeliveryLog.coach_id == coach.id,
                                                       ServiceDeliveryLog.status == "Completed"))
    for lg in logs:
        if lg.date_performed and sy_start <= lg.date_performed <= sy_end:
            delivered_by_month[lg.date_performed.month] += lg.duration or 0
    from .models import SCHOOL_MONTHS
    for m in SCHOOL_MONTHS:
        y = sy_start.year if m >= 7 else sy_end.year
        m_start, m_end = dt.date(y, m, 1), dt.date(y, m, calendar.monthrange(y, m)[1])
        cap = caps.get(m, 0) or 0
        wd = working_days(m_start, m_end) or 1
        off = 0
        for u in coach.unavailability:
            s, e = max(u.start_date, m_start), min(u.end_date, m_end)
            if s <= e:
                off += working_days(s, e)
        unavailable = r2(cap * off / wd)
        net = r2(cap - unavailable)
        row.months.append({"month": m, "year": y, "capacity": cap, "unavailable": unavailable, "net": net,
                           "delivered": r2(delivered_by_month.get(m, 0)), "past": m_end < on})
        row.annual_net += net
        if m_end >= on:
            row.remaining_net += net if m_start > on else net * (working_days(on, m_end) / wd)
    lines = db.scalars(select(ContractService).join(TrainingContract).where(
        ContractService.assigned_coach_id == coach.id, ContractService.end_date.is_(None),
        TrainingContract.school_year == school_year, TrainingContract.status.in_(ACTIVE_CONTRACT)))
    for ln in lines:
        committed = ln.committed_hours
        delivered = delivered_units(ln) * (ln.hours_per_unit or 0)
        load = max(committed, delivered)  # coaching often runs past what was contracted
        row.committed += committed
        row.delivered += delivered
        row.load += load
        row.remaining_load += max(load - delivered, 0)
        row.lines.append({"line": ln, "committed": committed, "delivered": delivered, "load": load})
    row.annual_net = r2(row.annual_net)
    row.remaining_net = r2(row.remaining_net)
    row.headroom = r2(row.annual_net - row.load)
    row.remaining_headroom = r2(row.remaining_net - row.remaining_load)
    return row


def pooled_demand(db, school_year: str) -> tuple[float, list]:
    """Committed hours on service lines that do not have a coach yet."""
    lines = list(db.scalars(select(ContractService).join(TrainingContract).where(
        ContractService.assigned_coach_id.is_(None), ContractService.end_date.is_(None),
        TrainingContract.school_year == school_year, TrainingContract.status.in_(("Draft",) + ACTIVE_CONTRACT))))
    return r2(sum(ln.committed_hours for ln in lines)), lines


# ---------------------------------------------------------------------------
# Consult plans
# ---------------------------------------------------------------------------
def plan_hours(plan) -> dict:
    booked = sum(s.duration or 0 for s in plan.sessions)
    held = sum(s.duration or 0 for s in plan.sessions if s.delivery_log and s.delivery_log.status == "Completed")
    contracted = plan.contract_service.contracted_quantity if plan.contract_service else 0
    return {"booked": r2(booked), "held": r2(held), "contracted": r2(contracted),
            "unbooked": r2(contracted - booked), "over": booked > contracted + 1e-9}


def mark_session_held(db, s: ConsultSession, coach_id: Optional[int], when: Optional[dt.date]) -> ServiceDeliveryLog:
    plan = s.plan
    line = plan.contract_service
    lg = s.delivery_log or ServiceDeliveryLog(contract_id=plan.contract_id)
    lg.contract_service_id = line.id if line else None
    lg.coach_id = coach_id or (line.assigned_coach_id if line else None)
    lg.status = "Completed"
    lg.date_performed = when or s.session_date or today()
    lg.duration = s.duration
    lg.units = s.duration or 0
    lg.mode = s.mode
    lg.subject = f"Consult session {s.session_no}: {s.topic or ''}".strip()
    db.add(lg)
    db.flush()
    s.delivery_log_id = lg.id
    return lg


def service_type_by_name(db, name: str) -> Optional[ServiceType]:
    return db.scalar(select(ServiceType).where(ServiceType.service_name == name))


def new_lead_steps():
    from .models import LeadStep
    return [LeadStep(step=s, sort_order=i) for i, s in enumerate(LEAD_STEPS)]
