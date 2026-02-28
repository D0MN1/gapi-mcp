"""Lean Google Calendar + Tasks MCP server. 12 tools, single file."""

from __future__ import annotations

import json
import functools
from datetime import datetime, timezone as _tz
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

# ── Config ───────────────────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent
CLIENT_SECRET = PROJECT_DIR / "client_secret.json"
CREDS_PATH = PROJECT_DIR / "credentials.json"
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/tasks",
]

mcp = FastMCP("gapi")

# ── Auth ─────────────────────────────────────────────────────────────────────


def _save_creds(creds: Credentials) -> None:
    """Persist credentials to disk."""
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
    }
    if creds.expiry:
        data["expiry"] = creds.expiry.isoformat()
    CREDS_PATH.write_text(json.dumps(data, indent=2))


def _load_creds() -> Credentials:
    """Load credentials from disk, refresh if expired, or run OAuth flow."""
    creds = None

    if CREDS_PATH.exists():
        data = json.loads(CREDS_PATH.read_text())
        creds = Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=data.get("scopes", SCOPES),
        )

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_creds(creds)

    if not creds or not creds.valid:
        if not CLIENT_SECRET.exists():
            raise RuntimeError(f"No credentials and no client_secret.json at {CLIENT_SECRET}")
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        creds = flow.run_local_server(port=0)
        _save_creds(creds)

    return creds


def _calendar():
    return build("calendar", "v3", credentials=_load_creds())


def _tasks():
    return build("tasks", "v1", credentials=_load_creds())


# ── Helpers ──────────────────────────────────────────────────────────────────


def _api_error_handler(func):
    """Wrap tool functions to catch Google API errors."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except HttpError as e:
            return f"Google API error {e.resp.status}: {e._get_reason()}"
        except Exception as e:
            return f"Error: {e}"
    return wrapper


def _fmt_event(ev: dict) -> str:
    """Format a calendar event for display."""
    start = ev.get("start", {})
    end = ev.get("end", {})
    s = start.get("dateTime", start.get("date", "?"))
    e = end.get("dateTime", end.get("date", "?"))
    summary = ev.get("summary", "(no title)")
    eid = ev.get("id", "")
    link = ev.get("htmlLink", "")
    parts = [f"- {summary}", f"  Start: {s}", f"  End: {e}", f"  ID: {eid}"]
    if link:
        parts.append(f"  Link: {link}")
    loc = ev.get("location")
    if loc:
        parts.append(f"  Location: {loc}")
    return "\n".join(parts)


def _fmt_task(t: dict) -> str:
    """Format a task for display."""
    title = t.get("title", "(no title)")
    status = t.get("status", "?")
    tid = t.get("id", "")
    due = t.get("due")
    notes = t.get("notes")
    parts = [f"- [{status}] {title}", f"  ID: {tid}"]
    if due:
        parts.append(f"  Due: {due}")
    if notes:
        preview = notes[:200] + "..." if len(notes) > 200 else notes
        parts.append(f"  Notes: {preview}")
    return "\n".join(parts)


# ── Calendar Tools ───────────────────────────────────────────────────────────


@mcp.tool()
@_api_error_handler
def list_calendars() -> str:
    """List all calendars accessible to the user."""
    result = _calendar().calendarList().list().execute()
    items = result.get("items", [])
    if not items:
        return "No calendars found."
    lines = []
    for cal in items:
        primary = " (primary)" if cal.get("primary") else ""
        lines.append(f"- {cal.get('summary', '?')}{primary}\n  ID: {cal.get('id', '?')}")
    return "\n".join(lines)


@mcp.tool()
@_api_error_handler
def get_events(
    time_min: str,
    time_max: str,
    calendar_id: str = "primary",
    query: str | None = None,
    max_results: int = 25,
) -> str:
    """Get calendar events in a time range.

    Args:
        time_min: Start time (RFC3339, e.g. '2026-02-28T00:00:00Z' or '2026-02-28')
        time_max: End time (RFC3339, e.g. '2026-02-28T23:59:59Z' or '2026-03-01')
        calendar_id: Calendar ID (default: 'primary')
        query: Optional keyword search in event fields
        max_results: Maximum events to return (default: 25)
    """
    # Normalize date-only to RFC3339
    if len(time_min) == 10:
        time_min += "T00:00:00Z"
    if len(time_max) == 10:
        time_max += "T00:00:00Z"

    kwargs: dict[str, Any] = dict(
        calendarId=calendar_id,
        timeMin=time_min,
        timeMax=time_max,
        maxResults=max_results,
        singleEvents=True,
        orderBy="startTime",
    )
    if query:
        kwargs["q"] = query

    result = _calendar().events().list(**kwargs).execute()
    items = result.get("items", [])
    if not items:
        return "No events found in the specified range."
    return "\n\n".join(_fmt_event(ev) for ev in items)


@mcp.tool()
@_api_error_handler
def create_event(
    summary: str,
    start: str,
    end: str,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    timezone: str | None = None,
    add_meet: bool = False,
    calendar_id: str = "primary",
) -> str:
    """Create a calendar event.

    Args:
        summary: Event title
        start: Start time (RFC3339, e.g. '2026-02-28T10:00:00+01:00' or '2026-02-28' for all-day)
        end: End time (RFC3339, e.g. '2026-02-28T11:00:00+01:00' or '2026-03-01' for all-day)
        description: Event description
        location: Event location
        attendees: List of attendee email addresses
        timezone: Timezone (e.g. 'Europe/Amsterdam')
        add_meet: Whether to add a Google Meet link
        calendar_id: Calendar ID (default: 'primary')
    """
    is_allday = len(start) == 10

    body: dict[str, Any] = {"summary": summary}

    if is_allday:
        body["start"] = {"date": start}
        body["end"] = {"date": end}
    else:
        body["start"] = {"dateTime": start}
        body["end"] = {"dateTime": end}
        if timezone:
            body["start"]["timeZone"] = timezone
            body["end"]["timeZone"] = timezone

    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": e} for e in attendees]
    if add_meet:
        body["conferenceData"] = {
            "createRequest": {"requestId": f"meet-{datetime.now(_tz.utc).strftime('%Y%m%d%H%M%S')}"}
        }

    kwargs: dict[str, Any] = dict(calendarId=calendar_id, body=body)
    if add_meet:
        kwargs["conferenceDataVersion"] = 1

    ev = _calendar().events().insert(**kwargs).execute()
    return f"Event created: {ev.get('summary')}\nLink: {ev.get('htmlLink')}\nID: {ev.get('id')}"


@mcp.tool()
@_api_error_handler
def modify_event(
    event_id: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    timezone: str | None = None,
    calendar_id: str = "primary",
) -> str:
    """Update fields on an existing calendar event. Only provided fields are changed.

    Args:
        event_id: The event ID to modify
        summary: New event title
        start: New start time (RFC3339)
        end: New end time (RFC3339)
        description: New description
        location: New location
        attendees: New attendee email list (replaces existing)
        timezone: Timezone for start/end
        calendar_id: Calendar ID (default: 'primary')
    """
    svc = _calendar()
    ev = svc.events().get(calendarId=calendar_id, eventId=event_id).execute()

    if summary is not None:
        ev["summary"] = summary
    if description is not None:
        ev["description"] = description
    if location is not None:
        ev["location"] = location
    if attendees is not None:
        ev["attendees"] = [{"email": e} for e in attendees]

    if start is not None:
        if len(start) == 10:
            ev["start"] = {"date": start}
        else:
            ev["start"] = {"dateTime": start}
            if timezone:
                ev["start"]["timeZone"] = timezone

    if end is not None:
        if len(end) == 10:
            ev["end"] = {"date": end}
        else:
            ev["end"] = {"dateTime": end}
            if timezone:
                ev["end"]["timeZone"] = timezone

    updated = svc.events().update(calendarId=calendar_id, eventId=event_id, body=ev).execute()
    return f"Event updated: {updated.get('summary')}\nLink: {updated.get('htmlLink')}"


@mcp.tool()
@_api_error_handler
def delete_event(event_id: str, calendar_id: str = "primary") -> str:
    """Delete a calendar event.

    Args:
        event_id: The event ID to delete
        calendar_id: Calendar ID (default: 'primary')
    """
    _calendar().events().delete(calendarId=calendar_id, eventId=event_id).execute()
    return f"Event {event_id} deleted."


@mcp.tool()
@_api_error_handler
def freebusy(
    time_min: str,
    time_max: str,
    calendar_ids: list[str] | None = None,
) -> str:
    """Check free/busy information for calendars.

    Args:
        time_min: Start of interval (RFC3339)
        time_max: End of interval (RFC3339)
        calendar_ids: Calendar IDs to query (default: ['primary'])
    """
    if len(time_min) == 10:
        time_min += "T00:00:00Z"
    if len(time_max) == 10:
        time_max += "T00:00:00Z"

    ids = calendar_ids or ["primary"]
    body = {
        "timeMin": time_min,
        "timeMax": time_max,
        "items": [{"id": cid} for cid in ids],
    }
    result = _calendar().freebusy().query(body=body).execute()
    calendars = result.get("calendars", {})

    lines = []
    for cal_id, info in calendars.items():
        busy = info.get("busy", [])
        if busy:
            lines.append(f"{cal_id}:")
            for period in busy:
                lines.append(f"  Busy: {period['start']} → {period['end']}")
        else:
            lines.append(f"{cal_id}: Free")
    return "\n".join(lines) or "No calendars returned."


# ── Tasks Tools ──────────────────────────────────────────────────────────────


@mcp.tool()
@_api_error_handler
def list_task_lists() -> str:
    """List all task lists."""
    result = _tasks().tasklists().list(maxResults=100).execute()
    items = result.get("items", [])
    if not items:
        return "No task lists found."
    lines = []
    for tl in items:
        lines.append(f"- {tl.get('title', '?')}\n  ID: {tl.get('id', '?')}")
    return "\n".join(lines)


@mcp.tool()
@_api_error_handler
def list_tasks(
    task_list_id: str,
    max_results: int = 100,
    show_completed: bool = True,
    show_hidden: bool = False,
    due_max: str | None = None,
    due_min: str | None = None,
) -> str:
    """List tasks in a task list.

    Args:
        task_list_id: The task list ID
        max_results: Maximum tasks to return (default: 100)
        show_completed: Include completed tasks (default: True)
        show_hidden: Include hidden tasks (default: False)
        due_max: Upper bound for due date (RFC3339)
        due_min: Lower bound for due date (RFC3339)
    """
    kwargs: dict[str, Any] = dict(
        tasklist=task_list_id,
        maxResults=max_results,
        showCompleted=show_completed,
        showHidden=show_hidden,
    )
    if due_max:
        kwargs["dueMax"] = due_max
    if due_min:
        kwargs["dueMin"] = due_min

    result = _tasks().tasks().list(**kwargs).execute()
    items = result.get("items", [])
    if not items:
        return "No tasks found."
    return "\n\n".join(_fmt_task(t) for t in items)


@mcp.tool()
@_api_error_handler
def get_task(task_list_id: str, task_id: str) -> str:
    """Get details of a specific task.

    Args:
        task_list_id: The task list ID
        task_id: The task ID
    """
    t = _tasks().tasks().get(tasklist=task_list_id, task=task_id).execute()
    parts = [
        f"Title: {t.get('title', '?')}",
        f"Status: {t.get('status', '?')}",
        f"ID: {t.get('id', '?')}",
    ]
    if t.get("due"):
        parts.append(f"Due: {t['due']}")
    if t.get("notes"):
        parts.append(f"Notes: {t['notes']}")
    if t.get("completed"):
        parts.append(f"Completed: {t['completed']}")
    if t.get("parent"):
        parts.append(f"Parent: {t['parent']}")
    return "\n".join(parts)


@mcp.tool()
@_api_error_handler
def create_task(
    task_list_id: str,
    title: str,
    notes: str | None = None,
    due: str | None = None,
    parent: str | None = None,
) -> str:
    """Create a new task.

    Args:
        task_list_id: The task list ID to create in
        title: Task title
        notes: Task notes/description
        due: Due date (RFC3339, e.g. '2026-02-28T00:00:00Z')
        parent: Parent task ID (for subtasks)
    """
    body: dict[str, Any] = {"title": title}
    if notes:
        body["notes"] = notes
    if due:
        body["due"] = due

    kwargs: dict[str, Any] = dict(tasklist=task_list_id, body=body)
    if parent:
        kwargs["parent"] = parent

    t = _tasks().tasks().insert(**kwargs).execute()
    return f"Task created: {t.get('title')}\nID: {t.get('id')}"


@mcp.tool()
@_api_error_handler
def update_task(
    task_list_id: str,
    task_id: str,
    title: str | None = None,
    notes: str | None = None,
    status: str | None = None,
    due: str | None = None,
) -> str:
    """Update an existing task. Only provided fields are changed.

    Args:
        task_list_id: The task list ID
        task_id: The task ID to update
        title: New title
        notes: New notes
        status: New status ('needsAction' or 'completed')
        due: New due date (RFC3339)
    """
    svc = _tasks()
    t = svc.tasks().get(tasklist=task_list_id, task=task_id).execute()

    if title is not None:
        t["title"] = title
    if notes is not None:
        t["notes"] = notes
    if status is not None:
        t["status"] = status
        if status == "completed":
            t["completed"] = datetime.now(_tz.utc).isoformat()
        elif status == "needsAction":
            t.pop("completed", None)
    if due is not None:
        t["due"] = due

    updated = svc.tasks().update(tasklist=task_list_id, task=task_id, body=t).execute()
    return f"Task updated: {updated.get('title')}\nStatus: {updated.get('status')}"


@mcp.tool()
@_api_error_handler
def delete_task(task_list_id: str, task_id: str) -> str:
    """Delete a task.

    Args:
        task_list_id: The task list ID
        task_id: The task ID to delete
    """
    _tasks().tasks().delete(tasklist=task_list_id, task=task_id).execute()
    return f"Task {task_id} deleted."


# ── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
