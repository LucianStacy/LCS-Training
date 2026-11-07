"""LCS-Training web app (FastAPI + Jinja). Runs locally; see README for how to start it."""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter, defaultdict
from typing import Optional
from urllib.parse import quote_plus

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select

from . import auth, docs, export, importer, logic
from .config import APP_DIR, COMPANY, FOOTER_LINE, UPLOAD_DIR
from .models import ROLES, AuditLog, User
from .models import *  # noqa: F401,F403  (template globals need the vocabularies)
from .models import (BILLING_FREQUENCIES, CLIENT_KINDS, CONTRACT_STATUSES, DELIVERY_MODES, DELIVERY_STATUSES,
                     INCOME_CATEGORIES, INSTALLMENT_STATUSES, LEAD_STATUSES, MONTH_NAMES, PARTY_DISTRICT,
                     PARTY_SELPA, PRICE_METHODS, QUOTE_STATUSES, SCHOOL_MONTHS, SERVICE_KINDS, STEP_STATUSES,
                     STUDENT_TIERS, TIER_HELP, UNAVAILABILITY_TYPES, DELIVERY_UNITS, BillingInstallment, Coach,
                     CoachMonthCapacity, CoachUnavailability, ConsultPlan, ConsultSession, Contact, ContactTag,
                     ContactTagItem, ContractPaperwork, ContractService, LcsDistrict, LcsSelpa, Lead,
                     OnboardingChecklist, OnboardingTask, PriceItem, Quote, QuoteItem, ServiceDeliveryLog,
                     ServiceType, SessionLocal, TaskTemplate, TaskTemplateItem, TrainingContract, ZoneRate,
                     init_db, school_year_bounds, school_year_for)
from .seed import PAPERWORK_STEPS, seed_reference_data

app = FastAPI(title="LCS-Training")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
app.include_router(__import__("lcs_training.accounts", fromlist=["router"]).router)
app.include_router(__import__("lcs_training.features", fromlist=["router"]).router)
templates = Jinja2Templates(directory=APP_DIR / "templates")


@app.on_event("startup")
def _startup():
    from . import mail
    init_db()
    with SessionLocal() as db:
        seed_reference_data(db)
        mail.seed_templates(db)
    mail.start_scheduler()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Access control: every request passes through here (policy lives in auth.RULES)
# ---------------------------------------------------------------------------
@app.middleware("http")
async def access_control(request: Request, call_next):
    path = request.url.path
    request.state.user = None
    if auth.PUBLIC.match(path):
        return await call_next(request)
    with SessionLocal() as db:
        if not db.scalar(select(User.id).limit(1)):
            return RedirectResponse("/first-run", status_code=303)
        user = auth.user_for_token(db, request.cookies.get(auth.COOKIE))
        if user:
            db.expunge(user)
    if user is None:
        nxt = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote_plus(nxt)}", status_code=303)
    request.state.user = user
    is_post = request.method not in ("GET", "HEAD", "OPTIONS")
    if is_post and not auth.same_origin(request):
        return _denied(request, user, "This form came from another website, so it was refused.")
    if user.must_change_password and not auth.ANY_USER.match(path) and path != "/logout":
        return RedirectResponse("/account?msg=" + quote_plus("Choose a new password to continue."), status_code=303)
    if not auth.ANY_USER.match(path):
        perm = auth.required_perm(request.method, path)
        if not auth.has_perm(user, perm):
            return _denied(request, user)
    response = await call_next(request)
    if is_post:
        with SessionLocal() as db:
            auth.log(db, user, f"{request.method} {path}", response.status_code)
    return response


def _denied(request, user, reason=None):
    with SessionLocal() as db:
        auth.log(db, user, f"DENIED {request.method} {request.url.path}", 403)
    return templates.TemplateResponse(request, "denied.html", {
        "request": request, "user": user, "reason": reason, "can": lambda p: auth.has_perm(user, p),
        "years": [], "school_year": "", "msg": None, "err": None}, status_code=403)


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------
def money(v, cents=True):
    if v is None:
        return ""
    return f"-${abs(v):,.2f}" if v < 0 else (f"${v:,.2f}" if cents else f"${v:,.0f}")


def fdate(d, fmt="%m/%d/%Y"):
    return d.strftime(fmt) if d else ""


def num(v):
    if v is None:
        return ""
    return f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def pct(v):
    return "" if v is None else f"{v * 100:.0f}%"


def party_url(ptype, pid):
    return f"/clients/{'selpa' if ptype == PARTY_SELPA else 'district'}/{pid}"


templates.env.filters.update(money=money, fdate=fdate, num=num, pct=pct)
templates.env.globals.update(
    party_url=party_url, STEP_STATUSES=STEP_STATUSES, CONTRACT_STATUSES=CONTRACT_STATUSES,
    BILLING_FREQUENCIES=BILLING_FREQUENCIES, INSTALLMENT_STATUSES=INSTALLMENT_STATUSES,
    QUOTE_STATUSES=QUOTE_STATUSES, LEAD_STATUSES=LEAD_STATUSES, STUDENT_TIERS=STUDENT_TIERS, TIER_HELP=TIER_HELP,
    INCOME_CATEGORIES=INCOME_CATEGORIES, PRICE_METHODS=PRICE_METHODS, DELIVERY_STATUSES=DELIVERY_STATUSES,
    DELIVERY_MODES=DELIVERY_MODES, UNAVAILABILITY_TYPES=UNAVAILABILITY_TYPES, CLIENT_KINDS=CLIENT_KINDS,
    SCHOOL_MONTHS=SCHOOL_MONTHS, MONTH_NAMES=MONTH_NAMES, SERVICE_KINDS=SERVICE_KINDS, DELIVERY_UNITS=DELIVERY_UNITS,
    CATS=logic.CATS, CAT_LABEL=logic.CAT_LABEL, COMPANY=COMPANY, FOOTER_LINE=FOOTER_LINE,
    PARTY_SELPA=PARTY_SELPA, PARTY_DISTRICT=PARTY_DISTRICT)


def current_year(db) -> str:
    return logic.get_setting(db, "school_year") or school_year_for(logic.today())


def render(request: Request, name: str, db, **ctx):
    years = sorted({current_year(db), school_year_for(logic.today())}
                   | {y for (y,) in db.execute(select(TrainingContract.school_year).distinct())}
                   | {y for (y,) in db.execute(select(PriceItem.school_year).distinct())})
    ctx.update(request=request, msg=request.query_params.get("msg"), err=request.query_params.get("err"),
               school_year=current_year(db), years=years, today=logic.today(),
               pname=lambda o: logic.party_name(db, o.party_type, o.party_id),
               user=request.state.user, can=lambda p: auth.has_perm(request.state.user, p))
    return templates.TemplateResponse(request, name, ctx)


def go(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote_plus(msg)}"
        sep = "&"
    if err:
        url += f"{sep}err={quote_plus(err)}"
    return RedirectResponse(url, status_code=303)


def fd(form, key) -> Optional[dt.date]:
    v = (form.get(key) or "").strip()
    if not v:
        return None
    try:
        return dt.date.fromisoformat(v)
    except ValueError:
        return None


def ff(form, key) -> Optional[float]:
    v = (form.get(key) or "").replace(",", "").replace("$", "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def fs(form, key) -> Optional[str]:
    v = (form.get(key) or "").strip()
    return v or None


def get_or_404(db, model, oid):
    obj = db.get(model, oid)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} {oid} not found")
    return obj


def save_steps(form, steps, prefix="step"):
    for s in steps:
        st = form.get(f"{prefix}_{s.id}_status")
        if st is None:
            continue
        s.status = st
        s.step_date = fd(form, f"{prefix}_{s.id}_date")
        s.note = fs(form, f"{prefix}_{s.id}_note")
        if s.status == "Done" and not s.step_date:
            s.step_date = logic.today()


def user_name(request):
    u = request.state.user
    return u.full_name if u else "LCS-Training staff"


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db=Depends(get_db)):
    sy = current_year(db)
    contracts = list(db.scalars(select(TrainingContract).where(TrainingContract.school_year == sy)))
    by_status = Counter(c.status for c in contracts)
    total_value = sum(c.total_contract_value for c in contracts if c.status != "Cancelled")
    risks = []
    for c in contracts:
        u = logic.contract_utilization(c)
        if u.risk:
            risks.append((c, u))
    today = logic.today()
    soon = today + dt.timedelta(days=45)
    due = list(db.scalars(select(BillingInstallment).join(TrainingContract).where(
        TrainingContract.school_year == sy, BillingInstallment.status == "Planned",
        BillingInstallment.due_date <= soon).order_by(BillingInstallment.due_date)))
    coaches = [logic.coach_capacity(db, c, sy) for c in db.scalars(select(Coach).where(Coach.active))]
    pooled, pooled_lines = logic.pooled_demand(db, sy)
    open_tasks = list(db.scalars(select(OnboardingTask).join(OnboardingChecklist).join(TrainingContract).where(
        TrainingContract.school_year == sy, OnboardingTask.status.in_(("Not Started", "In Progress")))))
    tasks_by_contract = Counter(t.checklist.contract_id for t in open_tasks)
    upcoming = list(db.scalars(select(ServiceDeliveryLog).where(
        ServiceDeliveryLog.status == "Scheduled", ServiceDeliveryLog.date_performed >= today,
        ServiceDeliveryLog.date_performed <= today + dt.timedelta(days=21)).order_by(ServiceDeliveryLog.date_performed)))
    leads = Counter(l.status for l in db.scalars(select(Lead).where(Lead.school_year == sy)))
    open_quotes = list(db.scalars(select(Quote).where(Quote.status.in_(("Draft", "Sent")))))
    plans = list(db.scalars(select(ConsultPlan).join(TrainingContract).where(TrainingContract.school_year == sy)))
    unbooked = sum(max(logic.plan_hours(p)["unbooked"], 0) for p in plans)
    review_prices = db.scalar(select(PriceItem.id).where(PriceItem.school_year == sy, PriceItem.needs_review).limit(1))
    empty = not contracts and not db.scalar(select(LcsDistrict.id).limit(1))
    from . import attention
    can = lambda p: auth.has_perm(request.state.user, p)  # noqa: E731
    att = attention.collect(db, sy, can)
    attention_count = sum(len(s.items) for s in att)
    attention_high = sum(1 for s in att for i in s.items if i.severity == "high")
    return render(request, "dashboard.html", db, contracts=contracts, by_status=by_status, total_value=total_value,
                  risks=risks, due=due, coaches=coaches, pooled=pooled, pooled_lines=pooled_lines,
                  tasks_by_contract=tasks_by_contract, open_task_count=len(open_tasks), upcoming=upcoming,
                  leads=leads, open_quotes=open_quotes, unbooked=unbooked, plans=plans, review_prices=review_prices,
                  empty=empty, contract_map={c.id: c for c in contracts}, attention_count=attention_count,
                  attention_high=attention_high)


@app.post("/year")
async def set_year(request: Request, db=Depends(get_db)):
    form = await request.form()
    y = fs(form, "school_year")
    if y and re.fullmatch(r"\d{4}-\d{2}", y):
        logic.set_setting(db, "school_year", y)
        db.commit()
    return go(request.headers.get("referer") or "/")


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
def _party_model(kind):
    if kind not in ("district", "selpa"):
        raise HTTPException(404)
    return LcsSelpa if kind == "selpa" else LcsDistrict


@app.get("/clients", response_class=HTMLResponse)
def clients(request: Request, q: str = "", db=Depends(get_db)):
    sy = current_year(db)
    parties = logic.all_parties(db)
    if q:
        ql = q.lower()
        parties = [p for p in parties if ql in p.name.lower() or ql in (p.aliases or "").lower()]
    contracts = defaultdict(list)
    for c in db.scalars(select(TrainingContract).where(TrainingContract.school_year == sy)):
        contracts[(c.party_type, c.party_id)].append(c)
    return render(request, "clients.html", db, parties=parties, q=q, contracts=contracts)


@app.post("/clients/new")
async def client_new(request: Request, db=Depends(get_db)):
    form = await request.form()
    name = fs(form, "name")
    if not name:
        return go("/clients", err="Enter a name.")
    if form.get("kind") == "SELPA":
        if db.scalar(select(LcsSelpa).where(LcsSelpa.selpa_name == name)):
            return go("/clients", err="That SELPA already exists.")
        p = LcsSelpa(selpa_name=name, region=fs(form, "region"))
    else:
        if db.scalar(select(LcsDistrict).where(LcsDistrict.district_name == name)):
            return go("/clients", err="That client already exists.")
        p = LcsDistrict(district_name=name, client_kind=form.get("kind") or "District", region=fs(form, "region"))
    db.add(p)
    db.commit()
    return go(party_url(p.party_type, p.id), "Client added.")


@app.get("/clients/{kind}/{pid}", response_class=HTMLResponse)
def client_detail(request: Request, kind: str, pid: int, db=Depends(get_db)):
    p = get_or_404(db, _party_model(kind), pid)
    pt = p.party_type
    contacts = list(db.scalars(select(Contact).where(Contact.party_type == pt, Contact.party_id == pid)
                               .order_by(Contact.full_name)))
    contracts = list(db.scalars(select(TrainingContract).where(TrainingContract.party_type == pt,
                                                               TrainingContract.party_id == pid)
                                .order_by(TrainingContract.school_year.desc())))
    quotes = list(db.scalars(select(Quote).where(Quote.party_type == pt, Quote.party_id == pid)
                             .order_by(Quote.quote_date.desc())))
    leads = list(db.scalars(select(Lead).where(Lead.party_type == pt, Lead.party_id == pid)))
    quoted = [q.grand_total for q in quotes]
    stats = {"count": len(quotes), "amendments": sum(1 for q in quotes if q.is_amendment),
             "high": max(quoted) if quoted else None, "low": min(quoted) if quoted else None,
             "total": sum(quoted), "won": sum(1 for q in quotes if q.status == "Accepted"),
             "accepted": sum(q.grand_total for q in quotes if q.status == "Accepted")}
    selpas = list(db.scalars(select(LcsSelpa).order_by(LcsSelpa.selpa_name)))
    return render(request, "client.html", db, p=p, kind=kind, contacts=contacts, contracts=contracts, quotes=quotes,
                  leads=leads, stats=stats, selpas=selpas, others=[o for o in logic.all_parties(db) if o is not p],
                  tags=list(db.scalars(select(ContactTag).order_by(ContactTag.sort_order))))


@app.post("/clients/{kind}/{pid}")
async def client_save(request: Request, kind: str, pid: int, db=Depends(get_db)):
    p = get_or_404(db, _party_model(kind), pid)
    form = await request.form()
    name = fs(form, "name")
    if name:
        if kind == "selpa":
            p.selpa_name = name
        else:
            p.district_name = name
            p.client_kind = form.get("client_kind") or p.client_kind
            p.selpa_id = int(form["selpa_id"]) if form.get("selpa_id") else None
            p.primary_contact_id = int(form["primary_contact_id"]) if form.get("primary_contact_id") else None
    p.region, p.student_tier = fs(form, "region"), fs(form, "student_tier")
    p.aliases, p.notes = fs(form, "aliases"), fs(form, "notes")
    db.commit()
    return go(party_url(p.party_type, p.id), "Saved.")


@app.post("/clients/{kind}/{pid}/merge")
async def client_merge(request: Request, kind: str, pid: int, db=Depends(get_db)):
    """Move everything from this client onto another one and delete this one."""
    src = get_or_404(db, _party_model(kind), pid)
    form = await request.form()
    ttype, tid = logic.parse_party_key(form.get("target") or "")
    target = logic.get_party(db, ttype, tid)
    if not target or target is src:
        return go(party_url(src.party_type, src.id), err="Pick a different client to merge into.")
    for model in (Contact, TrainingContract, Quote, Lead):
        for row in db.scalars(select(model).where(model.party_type == src.party_type, model.party_id == src.id)):
            row.party_type, row.party_id = target.party_type, target.id
    if isinstance(src, LcsSelpa):
        for d in list(src.member_districts):
            d.selpa = target if isinstance(target, LcsSelpa) else None
    names = {a for a in (target.aliases or "").splitlines() if a} | {src.name} | {
        a for a in (src.aliases or "").splitlines() if a}
    target.aliases = "\n".join(sorted(names - {target.name}))
    if src.notes:
        target.notes = ((target.notes or "") + "\n" + src.notes).strip()
    db.delete(src)
    db.commit()
    return go(party_url(target.party_type, target.id), f"Merged '{src.name}' into this client.")


# ---------------------------------------------------------------------------
# Contacts & tags
# ---------------------------------------------------------------------------
@app.get("/contacts", response_class=HTMLResponse)
def contacts(request: Request, tag: str = "", q: str = "", db=Depends(get_db)):
    stmt = select(Contact).order_by(Contact.full_name)
    if tag:
        stmt = stmt.join(ContactTagItem).join(ContactTag).where(ContactTag.tag_name == tag)
    if q:
        stmt = stmt.where(or_(Contact.full_name.ilike(f"%{q}%"), Contact.email_id.ilike(f"%{q}%")))
    rows = list(db.scalars(stmt).unique())
    tags = list(db.scalars(select(ContactTag).order_by(ContactTag.sort_order)))
    emails = sorted({c.email_id for c in rows if c.email_id})
    return render(request, "contacts.html", db, rows=rows, tags=tags, tag=tag, q=q, emails=emails,
                  parties=logic.all_parties(db))


@app.post("/contacts/save")
async def contact_save(request: Request, db=Depends(get_db)):
    form = await request.form()
    cid = form.get("id")
    c = get_or_404(db, Contact, int(cid)) if cid else Contact(full_name="")
    c.full_name = fs(form, "full_name") or "(unnamed)"
    c.email_id, c.phone, c.designation = fs(form, "email_id"), fs(form, "phone"), fs(form, "designation")
    c.notes = fs(form, "notes")
    c.no_email = bool(form.get("no_email"))
    c.party_type, c.party_id = logic.parse_party_key(form.get("party") or "")
    wanted = {int(t) for t in form.getlist("tags")}
    for ti in list(c.tags):
        if ti.tag_id not in wanted:
            c.tags.remove(ti)
    have = {ti.tag_id for ti in c.tags}
    for t in wanted - have:
        c.tags.append(ContactTagItem(tag_id=t))
    db.add(c)
    db.commit()
    back = form.get("back") or "/contacts"
    return go(back, "Contact saved.")


@app.post("/contacts/{cid}/delete")
async def contact_delete(request: Request, cid: int, db=Depends(get_db)):
    c = get_or_404(db, Contact, cid)
    for model, col in ((TrainingContract, "main_contact_id"), (TrainingContract, "contracts_contact_id"),
                       (LcsDistrict, "primary_contact_id")):
        for row in db.scalars(select(model).where(getattr(model, col) == cid)):
            setattr(row, col, None)
    db.delete(c)
    db.commit()
    form = await request.form()
    return go(form.get("back") or "/contacts", "Contact deleted.")


@app.get("/contacts/emails.txt", response_class=PlainTextResponse)
def contact_emails(tag: str = "", db=Depends(get_db)):
    stmt = select(Contact.email_id).where(Contact.email_id.is_not(None))
    if tag:
        stmt = stmt.join(ContactTagItem).join(ContactTag).where(ContactTag.tag_name == tag)
    return "; ".join(sorted({e for (e,) in db.execute(stmt)}))


@app.post("/tags/new")
async def tag_new(request: Request, db=Depends(get_db)):
    form = await request.form()
    name = fs(form, "tag_name")
    if name and not db.scalar(select(ContactTag).where(ContactTag.tag_name == name)):
        db.add(ContactTag(tag_name=name, description=fs(form, "description"), sort_order=99))
        db.commit()
    return go("/contacts", "Tag added.")


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------
@app.get("/leads", response_class=HTMLResponse)
def leads(request: Request, status: str = "", db=Depends(get_db)):
    sy = current_year(db)
    stmt = select(Lead).where(Lead.school_year == sy)
    if status:
        stmt = stmt.where(Lead.status == status)
    rows = sorted(db.scalars(stmt), key=lambda l: logic.party_name(db, l.party_type, l.party_id).lower())
    return render(request, "leads.html", db, rows=rows, status=status, parties=logic.all_parties(db))


@app.post("/leads/new")
async def lead_new(request: Request, db=Depends(get_db)):
    form = await request.form()
    pt, pid = logic.parse_party_key(form.get("party") or "")
    if not pid:
        return go("/leads", err="Pick a client (add new clients on the Clients page first).")
    lead = Lead(party_type=pt, party_id=pid, school_year=current_year(db), source=fs(form, "source"),
                contact_text=fs(form, "contact_text"), initial_query_date=fd(form, "initial_query_date") or logic.today())
    lead.steps = logic.new_lead_steps()
    db.add(lead)
    db.commit()
    return go(f"/leads/{lead.id}", "Lead added.")


@app.get("/leads/{lid}", response_class=HTMLResponse)
def lead_detail(request: Request, lid: int, db=Depends(get_db)):
    lead = get_or_404(db, Lead, lid)
    quotes = list(db.scalars(select(Quote).where(Quote.lead_id == lid).order_by(Quote.quote_date)))
    return render(request, "lead.html", db, lead=lead, quotes=quotes)


@app.post("/leads/{lid}")
async def lead_save(request: Request, lid: int, db=Depends(get_db)):
    lead = get_or_404(db, Lead, lid)
    form = await request.form()
    lead.status = form.get("status") or lead.status
    lead.source, lead.contact_text, lead.notes = fs(form, "source"), fs(form, "contact_text"), fs(form, "notes")
    lead.initial_query_date = fd(form, "initial_query_date")
    save_steps(form, lead.steps)
    db.commit()
    return go(f"/leads/{lid}", "Lead saved.")


# ---------------------------------------------------------------------------
# Price list
# ---------------------------------------------------------------------------
@app.get("/prices", response_class=HTMLResponse)
def prices(request: Request, db=Depends(get_db)):
    sy = current_year(db)
    items = list(db.scalars(select(PriceItem).where(PriceItem.school_year == sy).order_by(PriceItem.sort_order)))
    zones = list(db.scalars(select(ZoneRate).where(ZoneRate.school_year == sy).order_by(ZoneRate.miles_from)))
    other_years = sorted({y for (y,) in db.execute(select(PriceItem.school_year).distinct())} - {sy})
    return render(request, "prices.html", db, items=items, zones=zones, other_years=other_years,
                  service_types=list(db.scalars(select(ServiceType).order_by(ServiceType.sort_order))))


@app.post("/prices/save")
async def prices_save(request: Request, db=Depends(get_db)):
    form = await request.form()
    sy = current_year(db)
    for p in db.scalars(select(PriceItem).where(PriceItem.school_year == sy)):
        k = f"p{p.id}_"
        if form.get(k + "item_name") is None:
            continue
        p.item_name = fs(form, k + "item_name") or p.item_name
        p.variant, p.description = fs(form, k + "variant"), fs(form, k + "description")
        p.income_category = form.get(k + "income_category") or p.income_category
        p.method = form.get(k + "method") or p.method
        for f_ in ("rate", "rate_small", "rate_medium", "rate_large", "rate_xl"):
            setattr(p, f_, ff(form, k + f_) or 0)
        for f_ in ("hours_per_unit", "hours_small", "hours_medium", "hours_large", "hours_xl", "bulk_min_qty"):
            setattr(p, f_, ff(form, k + f_))
        bd = ff(form, k + "bulk_discount_pct")
        p.bulk_discount_pct = bd / 100 if bd and bd > 1 else bd
        p.group_key, p.qty_label, p.factor_label = fs(form, k + "group_key"), fs(form, k + "qty_label"), fs(
            form, k + "factor_label")
        p.service_type_id = int(form[k + "service_type_id"]) if form.get(k + "service_type_id") else None
        p.active = form.get(k + "active") == "on"
        if form.get(k + "reviewed") == "on":
            p.needs_review = False
    db.commit()
    return go("/prices", "Price list saved.")


@app.post("/prices/new")
async def price_new(request: Request, db=Depends(get_db)):
    form = await request.form()
    sy = current_year(db)
    name = fs(form, "item_name")
    if not name:
        return go("/prices", err="Enter a name.")
    order = (db.scalar(select(PriceItem.sort_order).where(PriceItem.school_year == sy)
                       .order_by(PriceItem.sort_order.desc()).limit(1)) or 0) + 1
    p = PriceItem(school_year=sy, item_name=name, variant=fs(form, "variant"), item_code=importer._slug(name),
                  income_category=form.get("income_category") or "Consultation", method=form.get("method") or "unit",
                  rate=ff(form, "rate") or 0, sort_order=order, section=form.get("income_category") or "Consultation")
    db.add(p)
    db.commit()
    return go("/prices", "Price line added.")


@app.post("/prices/copy")
async def prices_copy(request: Request, db=Depends(get_db)):
    form = await request.form()
    src = form.get("from_year")
    sy = current_year(db)
    if db.scalar(select(PriceItem.id).where(PriceItem.school_year == sy).limit(1)):
        return go("/prices", err=f"{sy} already has a price list.")
    cols = [c.key for c in PriceItem.__table__.columns if c.key not in ("id", "school_year")]
    for p in db.scalars(select(PriceItem).where(PriceItem.school_year == src)):
        db.add(PriceItem(school_year=sy, **{c: getattr(p, c) for c in cols}, ))
    for z in db.scalars(select(ZoneRate).where(ZoneRate.school_year == src)):
        db.add(ZoneRate(school_year=sy, miles_from=z.miles_from, zone_no=z.zone_no, zone_charge=z.zone_charge,
                        additional_person_charge=z.additional_person_charge))
    db.flush()
    for p in db.scalars(select(PriceItem).where(PriceItem.school_year == sy)):
        p.needs_review = True
    db.commit()
    return go("/prices", f"Copied the {src} price list into {sy}. Review each price.")


@app.post("/zones/save")
async def zones_save(request: Request, db=Depends(get_db)):
    form = await request.form()
    for z in db.scalars(select(ZoneRate).where(ZoneRate.school_year == current_year(db))):
        z.miles_from = ff(form, f"z{z.id}_miles") or z.miles_from
        z.zone_charge = ff(form, f"z{z.id}_charge") or 0
        z.additional_person_charge = ff(form, f"z{z.id}_add") or 0
    db.commit()
    return go("/prices#zones", "Zone table saved.")


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------
@app.get("/quotes", response_class=HTMLResponse)
def quotes(request: Request, status: str = "", db=Depends(get_db)):
    stmt = select(Quote).order_by(Quote.quote_date.desc(), Quote.quote_number.desc())
    if status:
        stmt = stmt.where(Quote.status == status)
    return render(request, "quotes.html", db, rows=list(db.scalars(stmt)), status=status,
                  parties=logic.all_parties(db))


@app.post("/quotes/new")
async def quote_new(request: Request, db=Depends(get_db)):
    form = await request.form()
    pt, pid = logic.parse_party_key(form.get("party") or "")
    lead_id = int(form["lead_id"]) if form.get("lead_id") else None
    amends = int(form["amends_contract_id"]) if form.get("amends_contract_id") else None
    if amends:
        c = get_or_404(db, TrainingContract, amends)
        pt, pid = c.party_type, c.party_id
    if lead_id and not pid:
        lead = get_or_404(db, Lead, lead_id)
        pt, pid = lead.party_type, lead.party_id
    if not pid:
        return go("/quotes", err="Pick a client.")
    sy = current_year(db)
    if not db.scalar(select(PriceItem.id).where(PriceItem.school_year == sy).limit(1)):
        return go("/prices", err=f"There is no {sy} price list yet. Import or copy one first.")
    party = logic.get_party(db, pt, pid)
    q = Quote(quote_number=logic.next_quote_number(db, logic.today()), party_type=pt, party_id=pid, lead_id=lead_id,
              school_year=sy, quote_date=logic.today(), valid_until=logic.today() + dt.timedelta(days=60),
              student_tier=party.student_tier, is_amendment=bool(amends), amends_contract_id=amends)
    if lead_id is None and not amends:
        lead = db.scalar(select(Lead).where(Lead.party_type == pt, Lead.party_id == pid, Lead.school_year == sy))
        q.lead_id = lead.id if lead else None
    db.add(q)
    db.commit()
    return go(f"/quotes/{q.id}")


def _quote_ctx(db, q):
    items = list(db.scalars(select(PriceItem).where(PriceItem.school_year == q.school_year, PriceItem.active)
                            .order_by(PriceItem.sort_order)))
    lines = {it.price_item_id: it for it in q.items if it.price_item_id}
    sections = defaultdict(list)
    for p in items:
        sections[p.income_category].append(p)
    res = logic.compute_quote(db, q)
    return dict(q=q, sections=[(c, sections[c]) for c in INCOME_CATEGORIES if sections.get(c)], lines=lines,
                custom=[it for it in q.items if not it.price_item_id], warnings=res.warnings, zone=res.zone,
                party=logic.get_party(db, q.party_type, q.party_id), needs_review=any(p.needs_review for p in items))


@app.get("/quotes/{qid}", response_class=HTMLResponse)
def quote_edit(request: Request, qid: int, db=Depends(get_db)):
    q = get_or_404(db, Quote, qid)
    ctx = _quote_ctx(db, q)  # recomputed for display only; the session closes without committing
    return render(request, "quote.html", db, **ctx)


@app.post("/quotes/{qid}")
async def quote_save(request: Request, qid: int, db=Depends(get_db)):
    q = get_or_404(db, Quote, qid)
    if q.status == "Accepted":
        return go(f"/quotes/{qid}", err="An accepted quote can't be changed. Write an amendment quote instead.")
    form = await request.form()
    q.student_tier = fs(form, "student_tier")
    q.one_way_mileage = ff(form, "one_way_mileage")
    q.quote_date = fd(form, "quote_date") or q.quote_date
    q.valid_until = fd(form, "valid_until")
    q.client_details, q.internal_notes = fs(form, "client_details"), fs(form, "internal_notes")
    existing = {it.price_item_id: it for it in q.items if it.price_item_id}
    for p in db.scalars(select(PriceItem).where(PriceItem.school_year == q.school_year)):
        qty, factor = ff(form, f"qty_{p.id}"), ff(form, f"factor_{p.id}")
        it = existing.get(p.id)
        if qty:
            if not it:
                it = QuoteItem(price_item_id=p.id, sort_order=p.sort_order)
                q.items.append(it)
            it.qty, it.factor = qty, factor
            it.description = p.item_name + (f" - {p.variant}" if p.variant else "")
            it.price_item = p
        elif it:
            q.items.remove(it)
    for it in [i for i in q.items if not i.price_item_id]:
        k = f"c{it.id}_"
        if form.get(k + "description") is None:
            continue
        if not ff(form, k + "qty"):
            q.items.remove(it)
            continue
        it.description, it.qty, it.rate = fs(form, k + "description"), ff(form, k + "qty"), ff(form, k + "rate") or 0
        it.income_category = form.get(k + "income_category") or "Other"
    if fs(form, "new_description") and ff(form, "new_qty"):
        q.items.append(QuoteItem(description=fs(form, "new_description"), qty=ff(form, "new_qty"),
                                 rate=ff(form, "new_rate") or 0, income_category=form.get("new_category") or "Other",
                                 sort_order=999))
    db.flush()
    db.refresh(q)
    logic.compute_quote(db, q)
    if form.get("action") == "send":
        q.status = "Sent"
        if q.lead:
            q.lead.status = "Quoted" if q.lead.status == "Open" else q.lead.status
            logic._mark_steps(q.lead.steps, ["Quotes written & sent"], logic.today())
    db.commit()
    return go(f"/quotes/{qid}", "Quote saved." if form.get("action") != "send" else "Quote marked as sent.")


@app.post("/quotes/{qid}/status")
async def quote_status(request: Request, qid: int, db=Depends(get_db)):
    q = get_or_404(db, Quote, qid)
    form = await request.form()
    st = form.get("status")
    if st == "Accepted":
        if q.status == "Accepted":
            return go(f"/quotes/{qid}", err="Already accepted.")
        logic.compute_quote(db, q)
        c = logic.accept_quote(db, q, user_name(request))
        db.commit()
        if q.is_amendment:
            return go(f"/contracts/{c.id}?tab=amendments", f"Amendment quote applied to the contract; billing was reset.")
        return go(f"/contracts/{c.id}", "Quote accepted: contract, service lines and billing schedule created.")
    if st in QUOTE_STATUSES:
        q.status = st
        if st == "Declined" and q.lead and q.lead.status in ("Open", "Quoted"):
            q.lead.status = "Lost"
        db.commit()
    return go(f"/quotes/{qid}", f"Quote marked {st}.")


@app.post("/quotes/{qid}/delete")
def quote_delete(qid: int, db=Depends(get_db)):
    q = get_or_404(db, Quote, qid)
    if q.status == "Accepted":
        return go(f"/quotes/{qid}", err="Accepted quotes are kept for the record.")
    db.delete(q)
    db.commit()
    return go("/quotes", "Quote deleted.")


@app.get("/quotes/{qid}/print", response_class=HTMLResponse)
def quote_print(request: Request, qid: int, db=Depends(get_db)):
    q = get_or_404(db, Quote, qid)
    ctx = _quote_ctx(db, q)
    return render(request, "quote_print.html", db, **ctx)


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------
@app.get("/contracts", response_class=HTMLResponse)
def contracts(request: Request, status: str = "", db=Depends(get_db)):
    sy = current_year(db)
    stmt = select(TrainingContract).where(TrainingContract.school_year == sy)
    if status:
        stmt = stmt.where(TrainingContract.status == status)
    rows = sorted(db.scalars(stmt), key=lambda c: logic.party_name(db, c.party_type, c.party_id).lower())
    util = {c.id: logic.contract_utilization(c) for c in rows}
    return render(request, "contracts.html", db, rows=rows, status=status, util=util, parties=logic.all_parties(db))


@app.post("/contracts/new")
async def contract_new(request: Request, db=Depends(get_db)):
    form = await request.form()
    pt, pid = logic.parse_party_key(form.get("party") or "")
    if not pid:
        return go("/contracts", err="Pick a client.")
    sy = current_year(db)
    start, end = school_year_bounds(sy)
    c = TrainingContract(party_type=pt, party_id=pid, school_year=sy, contract_start_date=start,
                         contract_end_date=end, status="Draft", total_contract_value=ff(form, "value") or 0,
                         billing_frequency="Monthly", billing_start_date=start, billing_end_date=dt.date(end.year, 6, 1))
    c.amount_other = c.total_contract_value
    c.paperwork = [ContractPaperwork(step=s, sort_order=i) for i, s in enumerate(PAPERWORK_STEPS)]
    db.add(c)
    db.commit()
    return go(f"/contracts/{c.id}", "Contract created. Add its services and billing below.")


@app.get("/contracts/{cid}", response_class=HTMLResponse)
def contract_detail(request: Request, cid: int, tab: str = "overview", db=Depends(get_db)):
    c = get_or_404(db, TrainingContract, cid)
    sy = c.school_year
    coach_rows = {r.coach.id: r for r in (logic.coach_capacity(db, ch, sy) for ch in
                                          db.scalars(select(Coach).where(Coach.active)))}
    contacts = list(db.scalars(select(Contact).where(Contact.party_type == c.party_type,
                                                     Contact.party_id == c.party_id).order_by(Contact.full_name)))
    deliveries = list(db.scalars(select(ServiceDeliveryLog).where(ServiceDeliveryLog.contract_id == cid)
                                 .order_by(ServiceDeliveryLog.date_performed.is_(None), ServiceDeliveryLog.date_performed)))
    plans = list(db.scalars(select(ConsultPlan).where(ConsultPlan.contract_id == cid)))
    quote = db.get(Quote, c.quote_id) if c.quote_id else None
    amend_quotes = list(db.scalars(select(Quote).where(Quote.amends_contract_id == cid)))
    billed = sum(i.amount for i in c.billing_schedule if i.status in ("Invoiced", "Paid"))
    scheduled = sum(i.amount for i in c.billing_schedule if i.status != "Cancelled")
    return render(request, "contract.html", db, c=c, tab=tab, util=logic.contract_utilization(c),
                  checklist=logic.checklist_for(db, c), coaches=coach_rows, contacts=contacts, deliveries=deliveries,
                  plans=plans, plan_hours={p.id: logic.plan_hours(p) for p in plans}, quote=quote,
                  amend_quotes=amend_quotes, billed=billed, scheduled=scheduled,
                  service_types=list(db.scalars(select(ServiceType).where(ServiceType.active)
                                                .order_by(ServiceType.sort_order))),
                  party=logic.get_party(db, c.party_type, c.party_id))


@app.post("/contracts/{cid}/edit")
async def contract_edit(request: Request, cid: int, db=Depends(get_db)):
    c = get_or_404(db, TrainingContract, cid)
    form = await request.form()
    c.contract_start_date = fd(form, "contract_start_date") or c.contract_start_date
    c.contract_end_date = fd(form, "contract_end_date") or c.contract_end_date
    c.po_number, c.invoice_email = fs(form, "po_number"), fs(form, "invoice_email")
    c.notes, c.invoice_notes = fs(form, "notes"), fs(form, "invoice_notes")
    c.main_contact_id = int(form["main_contact_id"]) if form.get("main_contact_id") else None
    c.contracts_contact_id = int(form["contracts_contact_id"]) if form.get("contracts_contact_id") else None
    db.commit()
    return go(f"/contracts/{cid}", "Contract saved.")


@app.post("/contracts/{cid}/billing-settings")
async def contract_billing_settings(request: Request, cid: int, db=Depends(get_db)):
    c = get_or_404(db, TrainingContract, cid)
    form = await request.form()
    new_total = ff(form, "total_contract_value")
    cats = {k: ff(form, f"amount_{k}") or 0 for k in logic.CATS}
    if new_total is not None and abs(new_total - c.total_contract_value) > 0.004 and c.billing_schedule and any(
            i.status in ("Invoiced", "Paid") for i in c.billing_schedule):
        return go(f"/contracts/{cid}?tab=billing",
                  err="This contract has invoiced installments. Change its value with an amendment instead, "
                      "so the history is kept.")
    if new_total is not None:
        c.total_contract_value = new_total
    for k, v in cats.items():
        setattr(c, f"amount_{k}", v)
    c.billing_frequency = form.get("billing_frequency") or c.billing_frequency
    c.billing_start_date, c.billing_end_date = fd(form, "billing_start_date"), fd(form, "billing_end_date")
    c.number_of_installments = int(ff(form, "number_of_installments")) if ff(form, "number_of_installments") else None
    diff = round(c.total_contract_value - sum(cats.values()), 2)
    msgs = []
    if abs(diff) > 0.004:
        msgs.append(f"The categories were ${abs(diff):,.2f} {'short of' if diff > 0 else 'over'} the contract "
                    f"value; the difference was put in Other.")
    if form.get("regenerate"):
        msgs += logic.generate_billing_schedule(db, c, keep_issued=True)
    else:
        logic.reconcile_categories(c)
    db.commit()
    return go(f"/contracts/{cid}?tab=billing", " ".join(msgs) or "Billing settings saved.")


@app.post("/contracts/{cid}/status")
async def contract_status(request: Request, cid: int, db=Depends(get_db)):
    c = get_or_404(db, TrainingContract, cid)
    form = await request.form()
    st = form.get("status")
    if st not in CONTRACT_STATUSES:
        return go(f"/contracts/{cid}", err="Unknown status.")
    old, c.status = c.status, st
    msgs = logic.on_status_change(db, c, old)
    db.commit()
    return go(f"/contracts/{cid}", " ".join([f"Status set to {c.status}."] + msgs))


@app.post("/contracts/{cid}/paperwork")
async def contract_paperwork(request: Request, cid: int, db=Depends(get_db)):
    c = get_or_404(db, TrainingContract, cid)
    form = await request.form()
    save_steps(form, c.paperwork)
    msgs = []
    executed = next((p for p in c.paperwork if p.step.startswith("Completed (fully")), None)
    if executed and executed.status == "Done" and c.status == "Draft":
        c.status = "Pending Assignment"
        msgs = logic.on_status_change(db, c, "Draft")
        msgs.insert(0, "Fully executed contract received, so the contract moved to Pending Assignment.")
    db.commit()
    return go(f"/contracts/{cid}?tab=paperwork", " ".join(msgs) or "Paperwork saved.")


@app.post("/contracts/{cid}/services/add")
async def service_add(request: Request, cid: int, db=Depends(get_db)):
    c = get_or_404(db, TrainingContract, cid)
    form = await request.form()
    stype = get_or_404(db, ServiceType, int(form["service_type_id"]))
    qty = ff(form, "qty") or 0
    eff = fd(form, "effective_date") or (c.contract_start_date if c.status == "Draft" else logic.today())
    c.services.append(ContractService(service_type=stype, contracted_quantity=qty, description=fs(form, "description")
                                      or stype.service_name, hours_per_unit=ff(form, "hours_per_unit")
                                      if ff(form, "hours_per_unit") is not None else stype.default_hours_per_unit,
                                      effective_date=eff, frequency=fs(form, "frequency") or "per contract term"))
    msg = "Service line added."
    if c.status != "Draft":
        c.amendments.append(logic.ContractAmendment(
            amendment_date=eff, changed_by=user_name(request), reason=fs(form, "reason"),
            description=f"Added {stype.service_name} x{qty:g}"))
        msg += " Logged as an amendment."
        if logic.checklist_for(db, c):
            db.flush()
            n = logic.generate_onboarding(db, c)
            if n:
                msg += f" {n} onboarding task(s) added for it."
    db.commit()
    return go(f"/contracts/{cid}?tab=services", msg)


@app.post("/services/{sid}/coach")
async def service_coach(request: Request, sid: int, db=Depends(get_db)):
    ln = get_or_404(db, ContractService, sid)
    form = await request.form()
    ln.assigned_coach_id = int(form["coach_id"]) if form.get("coach_id") else None
    ln.hours_per_unit = ff(form, "hours_per_unit") if ff(form, "hours_per_unit") is not None else ln.hours_per_unit
    ln.notes = fs(form, "notes")
    db.flush()
    msgs = logic.maybe_activate(db, ln.contract)
    db.commit()
    return go(f"/contracts/{ln.contract_id}?tab=services", " ".join(["Saved."] + msgs))


@app.post("/services/{sid}/amend")
async def service_amend(request: Request, sid: int, db=Depends(get_db)):
    ln = get_or_404(db, ContractService, sid)
    form = await request.form()
    new_qty = ff(form, "new_qty")
    if new_qty is None:
        return go(f"/contracts/{ln.contract_id}?tab=services", err="Enter the new quantity.")
    if ln.contract.status == "Draft":
        ln.contracted_quantity = new_qty
        msg = "Quantity updated (draft contract, so no amendment was logged)."
    else:
        logic.amend_service_quantity(db, ln, new_qty, fd(form, "when") or logic.today(), user_name(request),
                                     fs(form, "reason"))
        msg = "Amendment logged: the old line was closed and a new one starts today."
    db.commit()
    return go(f"/contracts/{ln.contract_id}?tab=services", msg)


@app.post("/contracts/{cid}/value")
async def contract_value(request: Request, cid: int, db=Depends(get_db)):
    c = get_or_404(db, TrainingContract, cid)
    form = await request.form()
    new_total = ff(form, "new_total")
    if new_total is None:
        return go(f"/contracts/{cid}?tab=amendments", err="Enter the new contract value.")
    logic.apply_value_change(db, c, new_total, form.get("category") or "other", user_name(request), fs(form, "reason"),
                             fs(form, "description"), fd(form, "when") or logic.today())
    db.commit()
    return go(f"/contracts/{cid}?tab=billing", "Value changed, amendment logged, and remaining installments reset.")


@app.post("/contracts/{cid}/billing/generate")
def billing_generate(cid: int, db=Depends(get_db)):
    c = get_or_404(db, TrainingContract, cid)
    msgs = logic.generate_billing_schedule(db, c, keep_issued=True)
    db.commit()
    return go(f"/contracts/{cid}?tab=billing", " ".join(msgs) or "Billing schedule rebuilt.")


@app.post("/contracts/{cid}/billing/add")
async def billing_add(request: Request, cid: int, db=Depends(get_db)):
    c = get_or_404(db, TrainingContract, cid)
    form = await request.form()
    d, amt = fd(form, "due_date"), ff(form, "amount")
    if not d or amt is None:
        return go(f"/contracts/{cid}?tab=billing", err="Enter a due date and amount.")
    c.billing_schedule.append(BillingInstallment(due_date=d, amount=amt, amount_other=amt, note=fs(form, "note")))
    db.commit()
    return go(f"/contracts/{cid}?tab=billing", "Installment added.")


@app.post("/installments/{iid}")
async def installment_save(request: Request, iid: int, db=Depends(get_db)):
    i = get_or_404(db, BillingInstallment, iid)
    form = await request.form()
    if form.get("delete"):
        cid = i.contract_id
        db.delete(i)
        db.commit()
        return go(f"/contracts/{cid}?tab=billing", "Installment removed.")
    i.status = form.get("status") or i.status
    i.invoice_ref, i.note = fs(form, "invoice_ref"), fs(form, "note")
    if i.contract.billing_frequency == "Custom" or i.status == "Planned":
        i.due_date = fd(form, "due_date") or i.due_date
        new_amt = ff(form, "amount")
        if new_amt is not None and abs(new_amt - i.amount) > 0.004:
            ratio = new_amt / i.amount if i.amount else 0
            for k in logic.CATS:
                setattr(i, f"amount_{k}", round((getattr(i, f"amount_{k}") or 0) * ratio, 2))
            i.amount = new_amt
            i.amount_other = round(i.amount_other + new_amt - sum(getattr(i, f"amount_{k}") for k in logic.CATS), 2)
    db.commit()
    return go(f"/contracts/{i.contract_id}?tab=billing", "Installment saved.")


@app.get("/installments/{iid}/invoice", response_class=HTMLResponse)
def installment_invoice(request: Request, iid: int, db=Depends(get_db)):
    i = get_or_404(db, BillingInstallment, iid)
    c = i.contract
    live = [x for x in c.billing_schedule if x.status != "Cancelled"]
    n = len(live)
    idx = live.index(i) + 1 if i in live else None
    last = max((x.due_date for x in live), default=None)
    return render(request, "invoice.html", db, i=i, c=c, n=n, idx=idx, last=last,
                  party=logic.get_party(db, c.party_type, c.party_id))


@app.post("/contracts/{cid}/onboarding/generate")
def onboarding_generate(cid: int, db=Depends(get_db)):
    c = get_or_404(db, TrainingContract, cid)
    n = logic.generate_onboarding(db, c)
    db.commit()
    return go(f"/contracts/{cid}?tab=onboarding", f"{n} task(s) added from the templates." if n else
              "Checklist already has every task its services call for.")


@app.post("/contracts/{cid}/onboarding")
async def onboarding_save(request: Request, cid: int, db=Depends(get_db)):
    c = get_or_404(db, TrainingContract, cid)
    cl = logic.checklist_for(db, c)
    form = await request.form()
    if cl:
        save_steps(form, cl.tasks, "task")
        if fs(form, "new_task"):
            cl.tasks.append(OnboardingTask(step=fs(form, "new_task"), stage="Added by hand", sort_order=len(cl.tasks)))
        logic.refresh_onboarding_status(cl)
    db.commit()
    return go(f"/contracts/{cid}?tab=onboarding", "Checklist saved.")


@app.post("/contracts/{cid}/delete")
def contract_delete(cid: int, db=Depends(get_db)):
    c = get_or_404(db, TrainingContract, cid)
    if c.status != "Draft" or any(i.status in ("Invoiced", "Paid") for i in c.billing_schedule):
        return go(f"/contracts/{cid}", err="Only draft contracts with nothing invoiced can be deleted. Set the "
                                          "status to Cancelled instead.")
    for model in (ServiceDeliveryLog, ConsultPlan, OnboardingChecklist):
        for row in db.scalars(select(model).where(model.contract_id == cid)):
            db.delete(row)
    db.delete(c)
    db.commit()
    return go("/contracts", "Draft contract deleted.")


# ---------------------------------------------------------------------------
# Delivery log (Training Tracking + Audit Tracking)
# ---------------------------------------------------------------------------
@app.get("/deliveries", response_class=HTMLResponse)
def deliveries(request: Request, kind: str = "", status: str = "", coach: str = "", db=Depends(get_db)):
    sy = current_year(db)
    stmt = select(ServiceDeliveryLog).join(TrainingContract).where(TrainingContract.school_year == sy)
    if status:
        stmt = stmt.where(ServiceDeliveryLog.status == status)
    if coach:
        stmt = stmt.where(ServiceDeliveryLog.coach_id == int(coach))
    rows = list(db.scalars(stmt))
    if kind:
        rows = [r for r in rows if (r.contract_service and r.contract_service.service_type.kind == kind)
                or (kind == "Other" and not r.contract_service)]
    rows.sort(key=lambda r: (r.date_performed is None, r.date_performed or dt.date.max))
    contracts = sorted(db.scalars(select(TrainingContract).where(TrainingContract.school_year == sy)),
                       key=lambda c: logic.party_name(db, c.party_type, c.party_id).lower())
    return render(request, "deliveries.html", db, rows=rows, kind=kind, status=status, coach=coach,
                  coaches=list(db.scalars(select(Coach).order_by(Coach.coach_name))), contracts=contracts)


@app.get("/deliveries/new", response_class=HTMLResponse)
def delivery_new(request: Request, contract: int, line: Optional[int] = None, db=Depends(get_db)):
    c = get_or_404(db, TrainingContract, contract)
    lg = ServiceDeliveryLog(contract_id=c.id, status="Scheduled", units=1, contract_service_id=line)
    if line:
        ln = db.get(ContractService, line)
        lg.coach_id = ln.assigned_coach_id if ln else None
    return render(request, "delivery.html", db, lg=lg, c=c, coaches=list(db.scalars(select(Coach))), new=True)


@app.get("/deliveries/{did}", response_class=HTMLResponse)
def delivery_detail(request: Request, did: int, db=Depends(get_db)):
    lg = get_or_404(db, ServiceDeliveryLog, did)
    return render(request, "delivery.html", db, lg=lg, c=lg.contract, coaches=list(db.scalars(select(Coach))),
                  new=False)


@app.post("/deliveries/save")
async def delivery_save(request: Request, db=Depends(get_db)):
    form = await request.form()
    did = form.get("id")
    lg = get_or_404(db, ServiceDeliveryLog, int(did)) if did else ServiceDeliveryLog(
        contract_id=int(form["contract_id"]))
    if form.get("delete") and did:
        cid = lg.contract_id
        for s in db.scalars(select(ConsultSession).where(ConsultSession.delivery_log_id == lg.id)):
            s.delivery_log_id = None
        db.delete(lg)
        db.commit()
        return go(f"/contracts/{cid}?tab=delivery", "Entry deleted.")
    lg.contract_service_id = int(form["contract_service_id"]) if form.get("contract_service_id") else None
    lg.coach_id = int(form["coach_id"]) if form.get("coach_id") else None
    lg.status = form.get("status") or "Scheduled"
    lg.date_performed, lg.end_date = fd(form, "date_performed"), fd(form, "end_date")
    lg.time_text, lg.duration = fs(form, "time_text"), ff(form, "duration")
    if lg.duration is None and lg.time_text:
        lg.duration = importer.parse_duration(lg.time_text)
    lg.units = ff(form, "units") if ff(form, "units") is not None else 1
    for f_ in ("mode", "audience", "subject", "notes", "pre_meeting", "participants_received", "folder_link_sent",
               "placement_assessment", "pre_assessment", "post_assessment", "evaluations_received", "calpads_login",
               "go_ahead", "follow_up_requested", "follow_up_completed"):
        setattr(lg, f_, fs(form, f_))
    if lg.contract_service_id:
        ln = db.get(ContractService, lg.contract_service_id)
        if ln and ln.service_type.delivery_unit == "hour":
            lg.units = lg.duration or 0
    db.add(lg)
    db.commit()
    return go(form.get("back") or f"/contracts/{lg.contract_id}?tab=delivery", "Saved.")


# ---------------------------------------------------------------------------
# Consult plans (Pre-Consult Meeting Set Up Sheet)
# ---------------------------------------------------------------------------
@app.get("/plans", response_class=HTMLResponse)
def plans(request: Request, db=Depends(get_db)):
    sy = current_year(db)
    rows = sorted(db.scalars(select(ConsultPlan).join(TrainingContract).where(TrainingContract.school_year == sy)),
                  key=lambda p: logic.party_name(db, p.contract.party_type, p.contract.party_id).lower())
    return render(request, "plans.html", db, rows=rows, hours={p.id: logic.plan_hours(p) for p in rows})


@app.post("/contracts/{cid}/plans/new")
async def plan_new(request: Request, cid: int, db=Depends(get_db)):
    c = get_or_404(db, TrainingContract, cid)
    line = next((s for s in c.current_services if s.service_type.kind == "Consult" and s.service_type.delivery_unit
                 == "hour"), None)
    p = ConsultPlan(contract_id=c.id, contract_service_id=line.id if line else None)
    db.add(p)
    db.commit()
    return go(f"/plans/{p.id}", "" if line else "This contract has no consult-hours line, so there are no contracted "
                                              "hours to plan against.")


@app.get("/plans/{pid}", response_class=HTMLResponse)
def plan_detail(request: Request, pid: int, db=Depends(get_db)):
    p = get_or_404(db, ConsultPlan, pid)
    return render(request, "plan.html", db, p=p, h=logic.plan_hours(p), coaches=list(db.scalars(select(Coach))),
                  lines=[s for s in p.contract.current_services if s.service_type.kind == "Consult"])


@app.post("/plans/{pid}")
async def plan_save(request: Request, pid: int, db=Depends(get_db)):
    p = get_or_404(db, ConsultPlan, pid)
    form = await request.form()
    p.consult_date = fd(form, "consult_date")
    p.contract_service_id = int(form["contract_service_id"]) if form.get("contract_service_id") else None
    p.status = form.get("status") or p.status
    for f_ in ("structure_description", "baseline", "goals", "objectives"):
        setattr(p, f_, fs(form, f_))
    for s in list(p.sessions):
        k = f"s{s.id}_"
        if form.get(k + "remove"):
            if s.delivery_log_id:
                return go(f"/plans/{pid}", err="A session already logged as held can't be removed.")
            p.sessions.remove(s)
            continue
        s.duration, s.mode = ff(form, k + "duration"), fs(form, k + "mode")
        s.session_date, s.date_text, s.topic = fd(form, k + "date"), fs(form, k + "date_text"), fs(form, k + "topic")
    for n in range(int(ff(form, "add_rows") or 0)):
        p.sessions.append(ConsultSession(session_no=len(p.sessions) + 1))
    for i, s in enumerate(sorted(p.sessions, key=lambda x: (x.session_date or dt.date.max, x.session_no)), 1):
        s.session_no = i
    db.flush()
    h = logic.plan_hours(p)
    db.commit()
    warn = (f"Planned sessions ({h['booked']:g} hrs) are more than the {h['contracted']:g} contracted hours."
            if h["over"] else "")
    return go(f"/plans/{pid}", "Plan saved.", warn)


@app.post("/plans/{pid}/sessions/{sid}/held")
async def plan_session_held(request: Request, pid: int, sid: int, db=Depends(get_db)):
    s = get_or_404(db, ConsultSession, sid)
    form = await request.form()
    if not s.duration:
        return go(f"/plans/{pid}", err="Enter the session's duration first.")
    logic.mark_session_held(db, s, int(form["coach_id"]) if form.get("coach_id") else None, fd(form, "when"))
    db.commit()
    return go(f"/plans/{pid}", f"Session {s.session_no} logged as delivered ({s.duration:g} hrs).")


@app.post("/plans/{pid}/sent")
def plan_sent(pid: int, db=Depends(get_db)):
    p = get_or_404(db, ConsultPlan, pid)
    p.status, p.plan_sent_date = "Sent", logic.today()
    cl = logic.checklist_for(db, p.contract)
    if cl:
        logic._mark_steps(cl.tasks, ["Complete pre-consult plan and send training plan"], logic.today())
        logic.refresh_onboarding_status(cl)
    db.commit()
    return go(f"/plans/{pid}", "Marked as sent to the client.")


@app.get("/plans/{pid}/docx")
def plan_docx(pid: int, db=Depends(get_db)):
    p = get_or_404(db, ConsultPlan, pid)
    client = logic.party_name(db, p.contract.party_type, p.contract.party_id)
    coach = p.contract_service.assigned_coach.coach_name if p.contract_service and p.contract_service.assigned_coach else ""
    data = docs.consult_plan_docx(p, client, coach, logic.plan_hours(p))
    fname = re.sub(r"[^A-Za-z0-9 _-]", "", f"Pre-Consult Plan - {client}").strip() + ".docx"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ---------------------------------------------------------------------------
# Coaches & capacity
# ---------------------------------------------------------------------------
@app.get("/coaches", response_class=HTMLResponse)
def coaches(request: Request, db=Depends(get_db)):
    sy = current_year(db)
    rows = [logic.coach_capacity(db, c, sy) for c in db.scalars(select(Coach).order_by(Coach.coach_name))]
    pooled, pooled_lines = logic.pooled_demand(db, sy)
    return render(request, "coaches.html", db, rows=rows, pooled=pooled, pooled_lines=pooled_lines)


@app.post("/coaches/new")
async def coach_new(request: Request, db=Depends(get_db)):
    form = await request.form()
    name = fs(form, "coach_name")
    if not name or db.scalar(select(Coach).where(Coach.coach_name == name)):
        return go("/coaches", err="Enter a new coach name.")
    c = Coach(coach_name=name, email=fs(form, "email"))
    db.add(c)
    db.commit()
    return go(f"/coaches/{c.id}", "Coach added. Fill in their monthly capacity.")


@app.get("/coaches/{cid}", response_class=HTMLResponse)
def coach_detail(request: Request, cid: int, db=Depends(get_db)):
    c = get_or_404(db, Coach, cid)
    sy = current_year(db)
    caps = {x.month: x.hours for x in c.capacity if x.school_year == sy}
    recent = list(db.scalars(select(ServiceDeliveryLog).where(ServiceDeliveryLog.coach_id == cid)
                             .order_by(ServiceDeliveryLog.date_performed.desc()).limit(25)))
    return render(request, "coach.html", db, c=c, row=logic.coach_capacity(db, c, sy), caps=caps, recent=recent)


@app.post("/coaches/{cid}")
async def coach_save(request: Request, cid: int, db=Depends(get_db)):
    c = get_or_404(db, Coach, cid)
    form = await request.form()
    sy = current_year(db)
    c.coach_name = fs(form, "coach_name") or c.coach_name
    c.email, c.employee, c.specialties = fs(form, "email"), fs(form, "employee"), fs(form, "specialties")
    c.hours_per_day = ff(form, "hours_per_day") or c.hours_per_day
    c.active = form.get("active") == "on"
    c.notes = fs(form, "notes")
    caps = {x.month: x for x in c.capacity if x.school_year == sy}
    for m in SCHOOL_MONTHS:
        v = ff(form, f"m{m}")
        if v is None:
            continue
        if m in caps:
            caps[m].hours = v
        else:
            c.capacity.append(CoachMonthCapacity(school_year=sy, month=m, hours=v))
    db.commit()
    return go(f"/coaches/{cid}", "Coach saved.")


def _may_edit_timeoff(request, coach_id) -> bool:
    u = request.state.user
    return u.role != "Coach" or u.coach_id == coach_id


@app.post("/coaches/{cid}/unavailability")
async def coach_unavailable(request: Request, cid: int, db=Depends(get_db)):
    c = get_or_404(db, Coach, cid)
    if not _may_edit_timeoff(request, c.id):
        return go(f"/coaches/{cid}", err="Coaches can only change their own time off.")
    form = await request.form()
    s, e = fd(form, "start_date"), fd(form, "end_date")
    if not s:
        return go(f"/coaches/{cid}", err="Enter a start date.")
    c.unavailability.append(CoachUnavailability(start_date=s, end_date=e or s, type=form.get("type") or "Vacation",
                                                note=fs(form, "note")))
    db.commit()
    return go(f"/coaches/{cid}", "Time off added; capacity updated.")


@app.post("/unavailability/{uid}/delete")
def unavailability_delete(request: Request, uid: int, db=Depends(get_db)):
    u = get_or_404(db, CoachUnavailability, uid)
    cid = u.coach_id
    if not _may_edit_timeoff(request, cid):
        return go(f"/coaches/{cid}", err="Coaches can only change their own time off.")
    db.delete(u)
    db.commit()
    return go(f"/coaches/{cid}", "Time off removed.")


# ---------------------------------------------------------------------------
# Setup: service types & onboarding templates
# ---------------------------------------------------------------------------
@app.get("/setup", response_class=HTMLResponse)
def setup(request: Request, db=Depends(get_db)):
    return render(request, "setup.html", db,
                  service_types=list(db.scalars(select(ServiceType).order_by(ServiceType.sort_order))),
                  templates_=list(db.scalars(select(TaskTemplate).order_by(TaskTemplate.sort_order))))


@app.post("/setup/services")
async def setup_services(request: Request, db=Depends(get_db)):
    form = await request.form()
    for s in db.scalars(select(ServiceType)):
        k = f"st{s.id}_"
        if form.get(k + "name") is None:
            continue
        s.service_name = fs(form, k + "name") or s.service_name
        s.kind, s.delivery_unit = form.get(k + "kind") or s.kind, form.get(k + "unit") or s.delivery_unit
        s.default_hours_per_unit = ff(form, k + "hours") or 0
        s.active = form.get(k + "active") == "on"
    if fs(form, "new_name"):
        db.add(ServiceType(service_name=fs(form, "new_name"), kind=form.get("new_kind") or "Other",
                           delivery_unit=form.get("new_unit") or "session", default_hours_per_unit=ff(form, "new_hours")
                           or 0, sort_order=99))
    db.commit()
    return go("/setup", "Service types saved.")


@app.post("/setup/templates/{tid}")
async def setup_template(request: Request, tid: int, db=Depends(get_db)):
    t = get_or_404(db, TaskTemplate, tid)
    form = await request.form()
    t.title = fs(form, "title") or t.title
    t.source_document = fs(form, "source_document")
    t.service_type_id = int(form["service_type_id"]) if form.get("service_type_id") else None
    for it in list(t.items):
        if form.get(f"i{it.id}_remove"):
            t.items.remove(it)
        else:
            it.task, it.stage = fs(form, f"i{it.id}_task") or it.task, fs(form, f"i{it.id}_stage")
    if fs(form, "new_task"):
        t.items.append(TaskTemplateItem(task=fs(form, "new_task"), stage=fs(form, "new_stage"),
                                        sort_order=len(t.items)))
    db.commit()
    return go(f"/setup#t{tid}", "Template saved.")


@app.post("/setup/templates")
async def setup_template_new(request: Request, db=Depends(get_db)):
    form = await request.form()
    title = fs(form, "title")
    if title:
        t = TaskTemplate(title=title, service_type_id=int(form["service_type_id"]) if form.get("service_type_id")
                         else None, sort_order=99)
        db.add(t)
        db.commit()
        return go(f"/setup#t{t.id}", "Template added.")
    return go("/setup", err="Enter a title.")


# ---------------------------------------------------------------------------
# Import / export
# ---------------------------------------------------------------------------
@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request, db=Depends(get_db)):
    last = logic.get_setting(db, "last_import")
    return render(request, "import.html", db, report=json.loads(last) if last else None)


async def _save_upload(form, field_name) -> Optional[str]:
    up = form.get(field_name)
    if not up or not getattr(up, "filename", ""):
        return None
    name = re.sub(r"[^A-Za-z0-9 ._()-]", "_", up.filename)
    path = UPLOAD_DIR / name
    path.write_bytes(await up.read())
    return str(path)


@app.post("/import/prices")
async def import_prices(request: Request, db=Depends(get_db)):
    form = await request.form()
    path = await _save_upload(form, "file")
    if not path:
        return go("/import", err="Choose the Quote Pricing Worksheet (.xlsx).")
    sy = fs(form, "school_year") or current_year(db)
    try:
        rep = importer.import_price_worksheet(db, path, sy)
    except Exception as e:  # show the problem rather than a 500 page
        db.rollback()
        return go("/import", err=f"Couldn't read that workbook: {e}")
    logic.set_setting(db, "last_import", json.dumps(rep.as_dict()))
    db.commit()
    return go("/import#report", f"Price list for {sy} imported.")


@app.post("/import/clients")
async def import_clients(request: Request, db=Depends(get_db)):
    form = await request.form()
    path = await _save_upload(form, "file")
    if not path:
        return go("/import", err="Choose the Client Spreadsheet (.xlsx).")
    try:
        rep = importer.import_client_spreadsheet(db, path, fs(form, "school_year"))
    except Exception as e:
        db.rollback()
        return go("/import", err=f"Couldn't read that workbook: {e}")
    logic.set_setting(db, "last_import", json.dumps(rep.as_dict()))
    prev = json.loads(logic.get_setting(db, "import_matches") or "[]")
    logic.set_setting(db, "import_matches", json.dumps(prev + [m for m in rep.matched if m not in prev]))
    db.commit()
    return go("/import#report", "Client spreadsheet imported. Review the matches below, or on Needs attention.")


@app.get("/export/erpnext.zip")
def export_zip(db=Depends(get_db)):
    data = export.erpnext_export(db)
    fname = f"lcs-training-erpnext-export-{logic.today().isoformat()}.zip"
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{fname}"'})
