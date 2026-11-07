"""Reference data that describes *how LCS-Training works* (service types, tags,
checklist steps, onboarding templates). It holds no client data and no prices:
those are loaded from your own spreadsheets through the Import screen.
"""
from sqlalchemy import select

from .models import (ContactTag, ServiceType, Setting, TaskTemplate, TaskTemplateItem)

# name, kind, delivery unit, default coach hours per unit (from the Specialist
# Capacity Tracker "Assumptions - Max Time" columns: delivery + individual prep).
# 0 means "no assumption on file yet" and is flagged in the app.
SERVICE_TYPES = [
    ("LCS Data Meeting", "Meeting", "session", 0, "Group meeting: presentation and prep time are shared across all clients."),
    ("TOT Meeting", "Program", "participant", 7.5, None),
    ("Email Q&A", "Subscription", "subscription", 10, "10-12 months = 10 hrs, 7-9 = 7.5, 4-6 = 5, 1-3 = 2.5."),
    ("NDT Program", "Program", "participant", 40, "25 hrs presentation + 15 hrs individual prep."),
    ("Free Planning Consult", "Consult", "session", 0, None),
    ("Consults", "Consult", "hour", 1.5, "1.5 hours of coach time per consult hour (incl. prep)."),
    ("Audit Consult", "Consult", "session", 2, None),
    ("Monthly Learning Program", "Training", "session", 8, "2 hrs delivery + 6 hrs prep."),
    ("Audit Trainings", "Training", "session", 9, None),
    ("Customized Trainings", "Training", "session", 11, "3 hrs delivery + 8 hrs prep (half day)."),
    ("Fixed Trainings", "Training", "session", 5, "3 hrs delivery + 2 hrs prep."),
    ("Mini Trainings", "Training", "session", 3.5, "30 min delivery + 3 hrs prep."),
    ("Data Submissions", "Audit", "month", 12, "Hours per month: Small 12, Medium 20, Large 35, XL 48."),
    ("Fall 1 Audit", "Audit", "audit", 8, "Small 8, Medium 10, Large 12, XL 18."),
    ("EOY Audit", "Audit", "audit", 10, "Small 10, Medium 12, Large 14, XL 20."),
    ("SEIS Data Audit", "Audit", "audit", 10, "Small 10, Medium 12, Large 14, XL 18."),
    ("CALPADS Data Audit", "Audit", "audit", 12, "Small 12, Medium 14, Large 16, XL 22."),
    ("Submission Audit", "Audit", "audit", 0, None),
    ("Compliance Audit", "Audit", "audit", 0, None),
    ("Written Audit Report", "Audit", "audit", 0, None),
    ("Other Services", "Other", "session", 0, None),
]

CONTACT_TAGS = [
    ("Main Contact", "Day-to-day contact for the contract"),
    ("Contracts", "Signs or processes contracts"),
    ("Billing", "Receives invoices"),
    ("Onboarding", "Onboarding contact"),
    ("Training Coordinator", None),
    ("SEIS Lead", None),
    ("CALPADS Lead", None),
    ("Meeting Scheduler", None),
    ("Participant - LCS Data", "Attends LCS Data Meetings"),
    ("Participant - TOT", "SEIS Trainer of Trainers participant"),
    ("Participant - Email Q&A", "Uses the Email Q&A subscription"),
    ("Participant - NDT", "New Data Technician program participant"),
    ("Promotional List", "Receives promotional offers"),
]

LEAD_STEPS = [
    "Service options sent out", "Email for triage sent out", "Triage conversation held",
    "Email sent to schedule meeting (with link)", "Meeting scheduled", "Response received from client",
    "Quotes written & sent", "Quote accepted", "Quote signed & returned", "Client added to services",
    "Client added to onboarding", "Follow-up email 1", "Follow-up email 2", "Follow-up email 3",
    "Moved to promotional list", "Try again next school year",
]

PAPERWORK_STEPS = [
    "Quote signed & returned", "Verbal acceptance", "We generated contract",
    "They generated contract (sent back signed)", "W-9 sent out", "Additional docs sent from them",
    "Additional docs returned to them", "COI - Liability", "COI - Workers' Comp", "COI - Professional",
    "COI - Auto", "Board approval anticipated", "Completed (fully executed) contract received",
    "P.O. received", "Invoice information created", "Begin onboarding",
]

# (template title, service type or None for every contract, [(stage, task), ...])
TEMPLATES = [
    ("General onboarding", None, [
        ("Folder Creation", "Created/verified LEA business folder"),
        ("Folder Creation", "Created/verified service folder"),
        ("Folder Creation", "Created/verified client folder"),
        ("Initial Contact", "Welcome email sent"),
        ("Received from Client", "Received SEIS login"),
        ("Coach Assignment", "Primary coach assigned"),
        ("Follow Up", "Follow up on missing participant lists"),
        ("Follow Up", "Follow up on missing calendars"),
        ("Follow Up", "Follow up on missing SEIS logins"),
    ]),
    ("LCS Data Meeting participants", "LCS Data Meeting", [
        ("Received Participants", "Received participants - LCS Data"),
        ("Emails Documented", "Emails documented in list - LCS Data"),
        ("Participant Emails", "Participant emails sent out - LCS Data"),
        ("Calendar Invites", "Participants added to calendar invites - LCS Data"),
    ]),
    ("TOT participants", "TOT Meeting", [
        ("Received Participants", "Received participants - TOT"),
        ("Emails Documented", "Emails documented in list - TOT"),
        ("Participant Emails", "Participant emails sent out - TOT"),
        ("Calendar Invites", "Participants added to calendar invites - TOT"),
    ]),
    ("Email Q&A participants", "Email Q&A", [
        ("Received Participants", "Received participants - Email Q&A"),
        ("Emails Documented", "Emails documented in list - Email Q&A"),
        ("Participant Emails", "Participant emails sent out - Email Q&A"),
    ]),
    ("NDT Program onboarding", "NDT Program", [
        ("Received Participants", "Received participants - NDT"),
        ("Emails Documented", "Emails documented in list - NDT"),
        ("Participant Emails", "Participant emails sent out - NDT"),
        ("Calendar Invites", "Participants added to calendar invites - NDT training"),
        ("Scheduling", "Contact NDT participants to schedule in-person meeting"),
        ("Secondary Scheduling", "Using the NDT Program Schedule Set Up Sheet, create Zoom meetings & calendar invites"),
    ]),
    ("Consult program set-up", "Consults", [
        ("Consults", "Book pre-consult meeting"),
        ("Consults", "Complete pre-consult plan and send training plan"),
        ("Consults", "Book consult hours"),
        ("Consults", "Reminder sent out"),
    ]),
] + [
    (f"{t} scheduling", t, [("Scheduling", "Add trainings to the training schedule")])
    for t in ["Customized Trainings", "Fixed Trainings", "Mini Trainings", "Monthly Learning Program", "Audit Trainings"]
] + [
    (f"{t} set-up", t, [("Received from Client", "Received CALPADS login"),
                        ("Scheduling", "Add audits to the audit schedule")])
    for t in ["Fall 1 Audit", "EOY Audit", "CALPADS Data Audit", "Submission Audit", "Data Submissions"]
] + [
    (f"{t} set-up", t, [("Scheduling", "Add audits to the audit schedule")])
    for t in ["SEIS Data Audit", "Compliance Audit"]
]


def seed_reference_data(db) -> None:
    """Idempotent: adds anything missing, never overwrites edits."""
    existing = {s.service_name for s in db.scalars(select(ServiceType))}
    for i, (name, kind, unit, hours, note) in enumerate(SERVICE_TYPES):
        if name not in existing:
            db.add(ServiceType(service_name=name, kind=kind, delivery_unit=unit, default_hours_per_unit=hours,
                               sort_order=i, notes=note))
    tags = {t.tag_name for t in db.scalars(select(ContactTag))}
    for i, (name, desc) in enumerate(CONTACT_TAGS):
        if name not in tags:
            db.add(ContactTag(tag_name=name, description=desc, sort_order=i))
    db.flush()
    if not db.scalar(select(TaskTemplate.id).limit(1)):
        st = {s.service_name: s for s in db.scalars(select(ServiceType))}
        for i, (title, svc, items) in enumerate(TEMPLATES):
            t = TaskTemplate(title=title, service_type=st.get(svc) if svc else None, sort_order=i,
                             source_document="Client Spreadsheet - Client Onboarding tab")
            from .onboarding import DEFAULT_AUTO
            t.items = [TaskTemplateItem(stage=s, task=task, sort_order=j, auto=DEFAULT_AUTO.get(task, ""))
                       for j, (s, task) in enumerate(items)]
            db.add(t)
    if not db.get(Setting, "user_name"):
        db.add(Setting(key="user_name", value=""))
    db.commit()
