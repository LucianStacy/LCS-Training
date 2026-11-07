"""Onboarding automation: tick checklist tasks from what already happened in the app,
and draft follow-up emails for things still missing from the client.

Each template task can carry an ``auto`` rule key (chosen under Setup). A task with a
rule is ticked the moment the app's own data shows it's done: the welcome email went
out, every service line has a coach, the training is on the schedule, and so on.
Ticking only ever moves a task forward; nobody's manual status is undone.

``auto`` is None on rows that predate this feature (they get the default for their
wording on startup) and "" when someone chose "by hand" on purpose.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import select

from . import logic
from .models import (ConsultPlan, EmailMessage, OnboardingChecklist, OnboardingTask, ServiceDeliveryLog,
                     TaskTemplateItem, TrainingContract)

# key -> label shown in Setup and on the checklist
RULES = {
    "welcome_sent": "the welcome email is sent",
    "coach_assigned": "every service line has a coach",
    "scheduled": "a session for this service is on the schedule",
    "consult_booked": "the consult plan has a meeting date",
    "plan_sent": "the consult plan is sent to the client",
    "consult_hours_booked": "every consult plan session has a date",
    "reminder_sent": "a reminder email for this service is sent",
    "followup_participants": "every participant list is received",
    "followup_logins": "every login is received",
}

# Defaults for the stock wording (used for templates seeded before rules existed)
DEFAULT_AUTO = {
    "Welcome email sent": "welcome_sent",
    "Primary coach assigned": "coach_assigned",
    "Add trainings to the training schedule": "scheduled",
    "Add audits to the audit schedule": "scheduled",
    "Book pre-consult meeting": "consult_booked",
    "Complete pre-consult plan and send training plan": "plan_sent",
    "Book consult hours": "consult_hours_booked",
    "Reminder sent out": "reminder_sent",
    "Follow up on missing participant lists": "followup_participants",
    "Follow up on missing SEIS logins": "followup_logins",
}

OPEN = ("Not Started", "In Progress")
RECEIVED_STAGES = ("Received from Client", "Received Participants")


def backfill(db) -> None:
    """Give pre-existing template items and checklist tasks their default rule."""
    for it in db.scalars(select(TaskTemplateItem).where(TaskTemplateItem.auto.is_(None))):
        it.auto = DEFAULT_AUTO.get(it.task, "")
    items = {it.id: it.auto for it in db.scalars(select(TaskTemplateItem))}
    for t in db.scalars(select(OnboardingTask).where(OnboardingTask.auto.is_(None))):
        t.auto = items.get(t.template_item_id, DEFAULT_AUTO.get(t.step, "")) or ""
    db.commit()


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def _received(t: OnboardingTask) -> bool:
    return t.stage in RECEIVED_STAGES and t.step.lower().startswith("received")


def _is_participants(t):
    return "participant" in t.step.lower()


def missing_items(cl: OnboardingChecklist) -> list[OnboardingTask]:
    """'Received …' tasks still open: what we're waiting on from the client."""
    return [t for t in cl.tasks if _received(t) and t.status in OPEN]


def item_label(t: OnboardingTask) -> str:
    s = t.step.strip()
    s = s[len("received"):].strip() if s.lower().startswith("received") else s
    if s.lower().startswith("participants"):
        rest = s[len("participants"):].strip(" -–")
        return f"Participant list for {rest}" if rest else "Participant list"
    return s[:1].upper() + s[1:]


def _check(db, c: TrainingContract, cl: OnboardingChecklist, t: OnboardingTask) -> Optional[str]:
    """Return a short reason if the rule is met, else None."""
    rule = t.auto
    if rule == "welcome_sent":
        m = db.scalar(select(EmailMessage).where(EmailMessage.contract_id == c.id, EmailMessage.template_key ==
                                                 "welcome", EmailMessage.status == "Sent"))
        return f"welcome email sent {m.sent_at:%m/%d/%Y}" if m else None
    if rule == "coach_assigned":
        cur = c.current_services
        return "every service line has a coach" if cur and all(s.assigned_coach_id for s in cur) else None
    if rule in ("scheduled", "reminder_sent"):
        logs = [d for d in db.scalars(select(ServiceDeliveryLog).where(ServiceDeliveryLog.contract_id == c.id))
                if d.status != "Cancelled" and (t.service_type_id is None or (
                    d.contract_service and d.contract_service.service_type_id == t.service_type_id))]
        if rule == "scheduled":
            dated = [d for d in logs if d.date_performed]
            return f"{len(dated)} session(s) on the schedule" if dated else None
        ids = [d.id for d in logs]
        if not ids:
            return None
        m = db.scalar(select(EmailMessage).where(EmailMessage.delivery_id.in_(ids), EmailMessage.status == "Sent"))
        return f"reminder sent {m.sent_at:%m/%d/%Y}" if m else None
    if rule in ("consult_booked", "plan_sent", "consult_hours_booked"):
        plans = list(db.scalars(select(ConsultPlan).where(ConsultPlan.contract_id == c.id)))
        if rule == "consult_booked":
            p = next((p for p in plans if p.consult_date), None)
            return f"consult plan meeting {p.consult_date:%m/%d/%Y}" if p else None
        if rule == "plan_sent":
            p = next((p for p in plans if p.plan_sent_date or p.status not in ("Draft",)), None)
            return "consult plan sent" + (f" {p.plan_sent_date:%m/%d/%Y}" if p and p.plan_sent_date else "") \
                if p else None
        p = next((p for p in plans if p.sessions and all(s.session_date or s.date_text for s in p.sessions)), None)
        return f"{len(p.sessions)} consult session(s) dated" if p else None
    if rule in ("followup_participants", "followup_logins"):
        want = [x for x in cl.tasks if _received(x) and (_is_participants(x) == (rule == "followup_participants"))]
        if want and all(x.status in ("Done", "N/A") for x in want):
            return "everything was received"
        return None
    return None


def sync(db, c: TrainingContract) -> int:
    """Tick every open task whose rule is now met. Returns how many were ticked."""
    cl = logic.checklist_for(db, c)
    if not cl:
        return 0
    todo = [t for t in cl.tasks if t.auto in RULES and t.status in OPEN]
    todo.sort(key=lambda t: t.auto.startswith("followup"))  # follow-ups last: they depend on the others
    n = 0
    for t in todo:
        why = _check(db, c, cl, t)
        if why:
            t.status, t.step_date = "Done", t.step_date or logic.today()
            t.note = (t.note + " · " if t.note else "") + f"Ticked automatically: {why}"
            n += 1
    if n:
        logic.refresh_onboarding_status(cl)
    return n


def sync_contract_id(db, cid: Optional[int]) -> int:
    c = db.get(TrainingContract, cid) if cid else None
    return sync(db, c) if c else 0


def sync_all(db) -> int:
    n = 0
    for cl in db.scalars(select(OnboardingChecklist).join(TrainingContract).where(
            TrainingContract.status.in_(logic.ACTIVE_CONTRACT), OnboardingChecklist.status != "Complete")):
        n += sync(db, cl.contract)
    db.commit()
    return n


# ---------------------------------------------------------------------------
# Follow-up emails for missing items
# ---------------------------------------------------------------------------
def followups_due(db, every: int, start: dt.date, today: dt.date):
    """(contract, missing tasks, round) for checklists still waiting on the client."""
    for cl in db.scalars(select(OnboardingChecklist).join(TrainingContract).where(
            TrainingContract.status.in_(logic.ACTIVE_CONTRACT), OnboardingChecklist.generated_date >= start)):
        miss = missing_items(cl)
        age = (today - cl.generated_date).days
        if miss and every > 0 and age >= every:
            yield cl.contract, miss, age // every
