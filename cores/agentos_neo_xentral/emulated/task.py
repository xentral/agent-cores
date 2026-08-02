"""Xentral V3 facade · task — Aufgabe / Wiedervorlage.

Reads and writes the next-generation entity API ``/api/entity/task``: full CRUD,
44 declared properties, live-verified on mvp 2026-08-02. Nothing in the older
API generations exposes tasks at all, so this entity has no v1/v3 fallback — it
exists because the entity API exists.

What it is for: the follow-up. An agent that finds a mismatched invoice, an
overdue delivery or an unanswered enquiry can leave a dated, prioritised note
for a human instead of ending the conversation.

Three upstream traits shape the model, all measured rather than read off the
schema:

* ``assignee`` is READ-ONLY. It comes back on existing records, and a create or
  update carrying it answers 2xx while the field stays null. A task can be
  raised and scheduled through the API but NOT handed to anyone — the one thing
  that would make it a work item. Recorded as a wish, not worked around.
* ``completionDate`` is not set by writing ``status: completed`` — it stayed
  null on a task the API had just completed. It is filled elsewhere.
* ``timeline`` is declared ``readWrite`` with a full sub-node and is returned by
  NO read: not by the list, not by the single read, and no include/expand
  parameter produces it. Absent from this model until it reads back.
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import RO, FacadeAdapterBase, prop, ref

_CU = {"creatable": True, "updatable": True}
_PRIORITY = [{"value": v, "label": v.capitalize()} for v in ("low", "normal", "high")]
# Upstream vocabulary is snake_case; the model uses the core's camelCase and
# translates both ways, so a caller never sees `in_progress`.
_STATUS_UP = {"open": "open", "inProgress": "in_progress", "completed": "completed"}
_STATUS_DOWN = {v: k for k, v in _STATUS_UP.items()}
_RECURRENCE_UP = {
    "once": "one_time",
    "daily": "daily",
    "weekly": "weekly",
    "monthly": "monthly",
    "yearly": "yearly",
}
_RECURRENCE_DOWN = {v: k for k, v in _RECURRENCE_UP.items()}


class TaskAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="Task",
        label_en="Task",
        category="masterdata",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.task",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read", "create", "update", "delete"),
    )
    v3_path = "/api/entity/task"
    include = ""
    preview_template = "{{title}}"
    bf_sort = True
    # EVERY model path declared filterable or sortable needs its upstream key here,
    # or the facade forwards the model path and upstream answers 422 "Property
    # 'dates.created' does not exist". Nested paths are the whole point of the
    # model, so this table is not optional decoration.
    query_aliases = {
        "dates.due": "dueDate",
        "dates.completed": "completionDate",
        "dates.created": "createdDate",
        "recurrence.interval": "recurrenceInterval",
        "links.customer": "customer",
        "links.contactPerson": "contactPerson",
        "links.project": "project",
        "links.subProject": "subProject",
        "visibility.isPublic": "isPublic",
        "visibility.onStartPage": "isOnStartPage",
        "visibility.onBulletinBoard": "isShownOnBulletinBoard",
        "timeTracking.loggedAt": "loggedAt",
    }
    filter_value_maps = {"status": _STATUS_UP, "recurrence.interval": _RECURRENCE_UP}
    sections = {
        "general": {"label": "General"},
        "scheduling": {"label": "Scheduling"},
        "links": {"label": "Links"},
        "reminders": {"label": "Reminders"},
        "timeTracking": {"label": "Time tracking"},
    }

    def _created_handle(self, resp: Any) -> Any:
        """Entity-API records are addressed by ``uuid``; ``id`` is not filterable."""
        rec = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(rec, dict):
            rec = resp if isinstance(resp, dict) else {}
        return rec.get("uuid") or rec.get("id")

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "title": prop(
                "string",
                "Title",
                **_CU,
                section="general",
                filterable=True,
                searchable=True,
                previewable=True,
                description="Required — the only field upstream insists on.",
            ),
            "description": prop("string", "Description", **_CU, section="general", searchable=True),
            "status": prop(
                "select",
                "Status",
                **_CU,
                section="general",
                filterable=True,
                previewable=True,
                options=[
                    {"value": "open", "label": "Open"},
                    {"value": "inProgress", "label": "In progress"},
                    {"value": "completed", "label": "Completed"},
                ],
            ),
            "priority": prop(
                "select",
                "Priority",
                **_CU,
                section="general",
                filterable=True,
                options=_PRIORITY,
            ),
            "assignee": prop(
                "reference",
                "Assignee",
                **RO,
                reference="Employee",
                renderProperty="name",
                section="general",
                filterable=True,
                description=(
                    "READ-ONLY upstream. A create or update carrying an assignee "
                    "answers 2xx and leaves the field null (measured on mvp "
                    "2026-08-02) — a task can be raised through the API but not "
                    "handed to anyone."
                ),
            ),
            "notes": prop("string", "Notes", **_CU, section="general"),
            "dates": prop(
                "embedded",
                "Dates",
                section="scheduling",
                properties={
                    "due": prop("date", "Due date", **_CU, filterable=True, sortable=True),
                    "dueTime": prop(
                        "string",
                        "Due time",
                        **_CU,
                        description="Wall-clock time on the due date, `HH:MM:SS`.",
                    ),
                    "isAllDay": prop("boolean", "All day", **_CU),
                    "completed": prop(
                        "date",
                        "Completed on",
                        **RO,
                        filterable=True,
                        sortable=True,
                        description=(
                            "NOT set by writing `status: completed` — it stayed null "
                            "on a task the API had just completed."
                        ),
                    ),
                    "created": prop("date", "Created on", **RO, filterable=True, sortable=True),
                },
            ),
            "recurrence": prop(
                "embedded",
                "Recurrence",
                section="scheduling",
                properties={
                    "interval": prop(
                        "select",
                        "Interval",
                        **_CU,
                        filterable=True,
                        options=[
                            {"value": v, "label": v.capitalize()}
                            for v in ("once", "daily", "weekly", "monthly", "yearly")
                        ],
                    )
                },
            ),
            "links": prop(
                "embedded",
                "Links",
                section="links",
                properties={
                    "customer": prop(
                        "reference",
                        "Customer",
                        **_CU,
                        reference="Customer",
                        renderProperty="name",
                        filterable=True,
                    ),
                    "contactPerson": prop("reference", "Contact person", **_CU, filterable=True),
                    "project": prop("reference", "Project", **_CU, reference="Project"),
                    "subProject": prop("reference", "Sub-project", **_CU),
                },
            ),
            "visibility": prop(
                "embedded",
                "Visibility",
                section="general",
                properties={
                    "isPublic": prop("boolean", "Public", **_CU, filterable=True),
                    "onStartPage": prop("boolean", "On start page", **_CU, filterable=True),
                    "onBulletinBoard": prop("boolean", "On bulletin board", **_CU, filterable=True),
                },
            ),
            "reminder": prop(
                "embedded",
                "Reminder",
                section="reminders",
                properties={
                    "byEmail": prop("boolean", "Email reminder", **_CU),
                    "daysBefore": prop("integer", "Days before due", **_CU),
                    "advanceNoticeDays": prop("integer", "Advance notice days", **_CU),
                },
            ),
            "timeTracking": prop(
                "embedded",
                "Time tracking",
                section="timeTracking",
                properties={
                    "hours": prop("decimal", "Hours", **_CU),
                    "required": prop("boolean", "Required", **_CU),
                    "billable": prop("boolean", "Billable", **_CU),
                    "loggedAt": prop("datetime", "Logged at", **_CU, sortable=True),
                },
            ),
            "createdAt": prop("datetime", "Created at", **RO, sortable=True),
            "updatedAt": prop("datetime", "Updated at", **RO, sortable=True),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "task",
            "id": (
                f"tsk_{r['uuid']}"
                if r.get("uuid")
                else (f"tsk_{r.get('id')}" if r.get("id") is not None else None)
            ),
            "title": r.get("title"),
            "description": r.get("description") or None,
            "status": _STATUS_DOWN.get(r.get("status") or "", r.get("status")),
            "priority": r.get("priority"),
            "assignee": ref("emp_", self._ref_id(r.get("assignee")), None, None, "employees"),
            "notes": r.get("additionalNotes") or None,
            "dates": {
                "due": r.get("dueDate"),
                "dueTime": r.get("dueTime") or None,
                "isAllDay": r.get("isAllDay"),
                "completed": r.get("completionDate"),
                "created": r.get("createdDate"),
            },
            "recurrence": {
                "interval": _RECURRENCE_DOWN.get(
                    r.get("recurrenceInterval") or "", r.get("recurrenceInterval")
                )
            },
            "links": {
                "customer": ref("cus_", self._ref_id(r.get("customer")), None, None, "customers"),
                "contactPerson": ref(
                    "cnt_", self._ref_id(r.get("contactPerson")), None, None, "contactPersons"
                ),
                "project": ref("prj_", self._ref_id(r.get("project")), None, None, "projects"),
                "subProject": ref(
                    "sprj_", self._ref_id(r.get("subProject")), None, None, "subProjects"
                ),
            },
            "visibility": {
                "isPublic": r.get("isPublic"),
                "onStartPage": r.get("isOnStartPage"),
                "onBulletinBoard": r.get("isShownOnBulletinBoard"),
            },
            "reminder": {
                "byEmail": r.get("hasEmailReminder"),
                "daysBefore": r.get("emailReminderDays"),
                "advanceNoticeDays": r.get("advanceNoticeDays"),
            },
            "timeTracking": {
                "hours": r.get("hours"),
                "required": r.get("isTimeTrackingRequired"),
                "billable": r.get("isTimeTrackingBillable"),
                "loggedAt": r.get("loggedAt"),
            },
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    @staticmethod
    def _ref_id(value: Any) -> Any:
        return value.get("id") if isinstance(value, dict) else value

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        wire: dict[str, Any] = {}
        rejected: set[str] = set()

        if "title" in model:
            wire["title"] = model["title"]
        if "description" in model:
            wire["description"] = model["description"]
        if "notes" in model:
            wire["additionalNotes"] = model["notes"]
        if "priority" in model:
            wire["priority"] = model["priority"]
        if "status" in model:
            wire["status"] = _STATUS_UP.get(model["status"] or "", model["status"])

        dates = model.get("dates") or {}
        for mine, theirs in (("due", "dueDate"), ("dueTime", "dueTime"), ("isAllDay", "isAllDay")):
            if mine in dates:
                wire[theirs] = dates[mine]

        rec = model.get("recurrence") or {}
        if "interval" in rec:
            wire["recurrenceInterval"] = _RECURRENCE_UP.get(rec["interval"] or "", rec["interval"])

        links = model.get("links") or {}
        for mine, theirs in (
            ("customer", "customer"),
            ("contactPerson", "contactPerson"),
            ("project", "project"),
            ("subProject", "subProject"),
        ):
            if mine in links:
                rid = self._ref_id(links[mine])
                wire[theirs] = {"id": str(rid).split("_", 1)[-1]} if rid else None

        vis = model.get("visibility") or {}
        for mine, theirs in (
            ("isPublic", "isPublic"),
            ("onStartPage", "isOnStartPage"),
            ("onBulletinBoard", "isShownOnBulletinBoard"),
        ):
            if mine in vis:
                wire[theirs] = bool(vis[mine])

        rem = model.get("reminder") or {}
        for mine, theirs in (
            ("byEmail", "hasEmailReminder"),
            ("daysBefore", "emailReminderDays"),
            ("advanceNoticeDays", "advanceNoticeDays"),
        ):
            if mine in rem:
                wire[theirs] = rem[mine]

        tt = model.get("timeTracking") or {}
        for mine, theirs in (
            ("hours", "hours"),
            ("required", "isTimeTrackingRequired"),
            ("billable", "isTimeTrackingBillable"),
            ("loggedAt", "loggedAt"),
        ):
            if mine in tt:
                wire[theirs] = tt[mine]

        # `assignee` answers 2xx and does not persist — refuse it rather than let a
        # caller believe the task was handed over. `dates.completed` is derived.
        if "assignee" in model:
            rejected.add("assignee")
        if "completed" in dates:
            rejected.add("dates.completed")
        return wire, rejected
