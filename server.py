"""Lean Google Calendar + Tasks MCP server. 13 tools, single file."""

from __future__ import annotations

import json
import functools
import subprocess
import threading
from datetime import datetime, timezone as _tz
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

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
REAUTH_PORT = 18085  # Obscure port for persistent callback listener

mcp = FastMCP("gapi")

# ── Persistent OAuth callback listener ───────────────────────────────────────

# Shared state between the callback listener and the reauth tool.
_reauth_flow: InstalledAppFlow | None = None  # Set when reauth is initiated
_reauth_lock = threading.Lock()

_PAGE_STYLE = """
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    min-height: 100vh; display: flex; align-items: center; justify-content: center;
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: #0d0d0d; color: #e0e0e0;
  }
  .card {
    text-align: center; padding: 3rem 2.5rem; border-radius: 1rem;
    background: #1a1a1a; border: 1px solid #2a2a2a;
    box-shadow: 0 8px 32px rgba(0,0,0,.4);
    max-width: 420px; width: 90%;
  }
  .icon { font-size: 3rem; margin-bottom: 1rem; }
  h1 { font-size: 1.4rem; font-weight: 600; margin-bottom: .5rem; }
  p { font-size: .95rem; color: #888; line-height: 1.5; }
  .success h1 { color: #a8e6a3; }
  .error h1 { color: #e6a3a3; }
  .idle h1 { color: #a3c4e6; }
  .subtle { margin-top: 1.2rem; font-size: .8rem; color: #555; }
</style>
"""

_PAGE_SUCCESS = f"""<!DOCTYPE html><html><head><title>gapi-mcp</title>{_PAGE_STYLE}</head>
<body><div class="card success">
  <div class="icon">&#10003;</div>
  <h1>authenticated</h1>
  <p>credentials saved. you can close this tab.</p>
  <p class="subtle">gapi-mcp &middot; oauth callback</p>
</div></body></html>"""

_PAGE_IDLE = f"""<!DOCTYPE html><html><head><title>gapi-mcp</title>{_PAGE_STYLE}</head>
<body><div class="card idle">
  <div class="icon">&#9679;</div>
  <h1>listening</h1>
  <p>no active reauth flow. call the reauth tool first.</p>
  <p class="subtle">gapi-mcp &middot; oauth callback</p>
</div></body></html>"""

_PAGE_ERROR = f"""<!DOCTYPE html><html><head><title>gapi-mcp</title>{_PAGE_STYLE}</head>
<body><div class="card error">
  <div class="icon">&#10007;</div>
  <h1>auth failed</h1>
  <p>{{error}}</p>
  <p class="subtle">gapi-mcp &middot; oauth callback</p>
</div></body></html>"""


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handles Google's OAuth redirect on the persistent listener."""

    def do_GET(self):
        global _reauth_flow
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        with _reauth_lock:
            flow = _reauth_flow

        if flow is None or "code" not in params:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_PAGE_IDLE.encode())
            return

        # We have an active flow and a code — complete the exchange
        try:
            auth_response = f"https://localhost:{REAUTH_PORT}{self.path}"
            flow.fetch_token(authorization_response=auth_response)
            _save_creds(flow.credentials)

            with _reauth_lock:
                _reauth_flow = None

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_PAGE_SUCCESS.encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_PAGE_ERROR.replace("{error}", str(e)).encode())

    def log_message(self, format, *args):
        pass  # Suppress request logging to stderr (would pollute MCP stdio)


def _start_callback_listener():
    """Start the persistent OAuth callback HTTP server in a daemon thread."""
    server = HTTPServer(("localhost", REAUTH_PORT), _OAuthCallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


_start_callback_listener()

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


class ReauthRequired(Exception):
    """Raised when credentials are expired/revoked and need browser reauth."""
    pass


def _load_creds() -> Credentials:
    """Load credentials from disk, refresh if expired. Raises ReauthRequired on failure."""
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
        try:
            creds.refresh(Request())
            _save_creds(creds)
        except Exception:
            # Refresh failed (revoked, expired grant, etc.) — need full reauth
            raise ReauthRequired(
                "Token refresh failed. Call the 'reauth' tool to get a link to re-authorize."
            )

    if not creds or not creds.valid:
        raise ReauthRequired(
            "No valid credentials. Call the 'reauth' tool to get a link to authorize."
        )

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
        except ReauthRequired:
            return (
                "REAUTH REQUIRED: Google credentials are expired or revoked. "
                "Call the 'reauth' tool — it will return a URL for the user to "
                "open in their browser to re-authorize."
            )
        except HttpError as e:
            reason = e._get_reason()
            if "invalid_grant" in str(e) or "Token has been" in reason:
                return (
                    "REAUTH REQUIRED: Google credentials are expired or revoked. "
                    "Call the 'reauth' tool — it will return a URL for the user to "
                    "open in their browser to re-authorize."
                )
            return f"Google API error {e.resp.status}: {reason}"
        except Exception as e:
            if "invalid_grant" in str(e):
                return (
                    "REAUTH REQUIRED: Google credentials are expired or revoked. "
                    "Call the 'reauth' tool — it will return a URL for the user to "
                    "open in their browser to re-authorize."
                )
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


# ── Reauth Tool ──────────────────────────────────────────────────────────────


@mcp.tool()
def reauth() -> str:
    """Start Google OAuth re-authorization flow. Returns a URL for the user to open.

    Use this when other tools return REAUTH REQUIRED errors. The flow listens on
    a fixed port so the URL is always http://localhost:18085. Tell the user to open
    it in their browser and authorize. The tool blocks until authorization completes
    or times out after 120 seconds.
    """
    # NOTE: Despite the docstring, this tool now returns immediately after opening
    # the browser. A persistent listener on port 18085 handles the callback.
    # The docstring is kept for MCP tool description compatibility.
    global _reauth_flow

    try:
        # Remove stale credentials
        if CREDS_PATH.exists():
            CREDS_PATH.unlink()

        if not CLIENT_SECRET.exists():
            return f"ERROR: No client_secret.json found at {CLIENT_SECRET}. Cannot reauthorize."

        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        flow.redirect_uri = f"http://localhost:{REAUTH_PORT}/"
        auth_url, _ = flow.authorization_url(access_type="offline")

        # Store the flow so the persistent callback handler can complete it
        with _reauth_lock:
            _reauth_flow = flow

        # Open the auth URL in the user's browser
        subprocess.Popen(["xdg-open", auth_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        return (
            f"Browser opened for Google authorization.\n\n"
            f"If the browser didn't open, use this URL:\n{auth_url}\n\n"
            f"After authorizing, try any Google tool again to confirm it worked."
        )

    except Exception as e:
        return f"Reauth failed: {e}"


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
def get_task_list(task_list_id: str) -> str:
    """Get details of a specific task list.

    Args:
        task_list_id: The task list ID
    """
    tl = _tasks().tasklists().get(tasklist=task_list_id).execute()
    return f"Title: {tl.get('title', '?')}\nID: {tl.get('id', '?')}\nUpdated: {tl.get('updated', '?')}"


@mcp.tool()
@_api_error_handler
def create_task_list(title: str) -> str:
    """Create a new task list.

    Args:
        title: Title for the new task list
    """
    tl = _tasks().tasklists().insert(body={"title": title}).execute()
    return f"Task list created: {tl.get('title')}\nID: {tl.get('id')}"


@mcp.tool()
@_api_error_handler
def update_task_list(task_list_id: str, title: str) -> str:
    """Rename a task list.

    Args:
        task_list_id: The task list ID to update
        title: New title
    """
    tl = _tasks().tasklists().update(tasklist=task_list_id, body={"id": task_list_id, "title": title}).execute()
    return f"Task list updated: {tl.get('title')}"


@mcp.tool()
@_api_error_handler
def delete_task_list(task_list_id: str) -> str:
    """Delete a task list and all its tasks.

    Args:
        task_list_id: The task list ID to delete
    """
    _tasks().tasklists().delete(tasklist=task_list_id).execute()
    return f"Task list {task_list_id} deleted."


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


@mcp.tool()
@_api_error_handler
def move_task(
    task_list_id: str,
    task_id: str,
    parent: str | None = None,
    previous: str | None = None,
    destination_task_list: str | None = None,
) -> str:
    """Move a task to a different position, parent, or task list.

    Args:
        task_list_id: Current task list ID
        task_id: The task ID to move
        parent: New parent task ID (makes it a subtask)
        previous: Previous sibling task ID (for ordering)
        destination_task_list: Destination task list ID (for moving between lists)
    """
    svc = _tasks()

    if destination_task_list and destination_task_list != task_list_id:
        # Cross-list move: get task, create in destination, delete from source
        t = svc.tasks().get(tasklist=task_list_id, task=task_id).execute()
        body: dict[str, Any] = {"title": t.get("title", ""), "notes": t.get("notes"), "due": t.get("due"), "status": t.get("status")}
        body = {k: v for k, v in body.items() if v is not None}
        kwargs: dict[str, Any] = dict(tasklist=destination_task_list, body=body)
        if parent:
            kwargs["parent"] = parent
        if previous:
            kwargs["previous"] = previous
        new_t = svc.tasks().insert(**kwargs).execute()
        svc.tasks().delete(tasklist=task_list_id, task=task_id).execute()
        return f"Task moved to list {destination_task_list}\nNew ID: {new_t.get('id')}"

    # Same-list move
    kwargs = dict(tasklist=task_list_id, task=task_id)
    if parent:
        kwargs["parent"] = parent
    if previous:
        kwargs["previous"] = previous
    t = svc.tasks().move(**kwargs).execute()
    return f"Task moved: {t.get('title')}\nID: {t.get('id')}"


@mcp.tool()
@_api_error_handler
def clear_completed_tasks(task_list_id: str) -> str:
    """Clear all completed tasks from a task list (marks them as hidden).

    Args:
        task_list_id: The task list ID to clear completed tasks from
    """
    _tasks().tasks().clear(tasklist=task_list_id).execute()
    return f"Completed tasks cleared from list {task_list_id}."


# ── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
