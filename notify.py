#!/usr/bin/env python3
"""Email every Jira ticket that moved into the target status (default "Ready For QA")."""

import base64
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path


def env(name, default=None):
    value = os.environ.get(name) or default
    if value is None:
        sys.exit(f"Missing required setting: {name}")
    return value


JIRA_BASE_URL = env("JIRA_BASE_URL").rstrip("/")
JIRA_EMAIL = env("JIRA_EMAIL")
JIRA_API_TOKEN = env("JIRA_API_TOKEN")
TARGET_STATUS = env("TARGET_STATUS", "Ready For QA")
JQL_FILTER = env("JQL_FILTER", "")
LOOKBACK_HOURS = int(env("LOOKBACK_HOURS", "24"))
STATE_FILE = Path(env("STATE_FILE", "state/sent.json"))
DRY_RUN = env("DRY_RUN", "") == "1"

if not DRY_RUN:
    SMTP_USER = env("SMTP_USER")
    SMTP_PASSWORD = env("SMTP_PASSWORD")
    MAIL_TO = env("MAIL_TO")
    SMTP_HOST = env("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(env("SMTP_PORT", "465"))

AUTH_HEADER = "Basic " + base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()


def jira(method, path, params=None, body=None):
    url = JIRA_BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": AUTH_HEADER,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        sys.exit(f"Jira API error {error.code} on {path}: {error.read().decode(errors='replace')}")


def search_issues(jql):
    issues, token = [], None
    while True:
        body = {"jql": jql, "fields": ["summary", "description"], "expand": "renderedFields", "maxResults": 50}
        if token:
            body["nextPageToken"] = token
        page = jira("POST", "/rest/api/3/search/jql", body=body)
        issues.extend(page.get("issues", []))
        token = page.get("nextPageToken")
        if not token:
            return issues


def parse_jira_time(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def transitions_into_target(issue_key, since):
    """Yield (history_id, when, who) for each move into TARGET_STATUS after `since`."""
    start = 0
    while True:
        page = jira("GET", f"/rest/api/3/issue/{issue_key}/changelog", params={"startAt": start, "maxResults": 100})
        histories = page.get("values", [])
        for history in histories:
            when = parse_jira_time(history["created"])
            if when < since:
                continue
            for item in history.get("items", []):
                is_status = item.get("fieldId", item.get("field")) == "status"
                if is_status and (item.get("toString") or "").casefold() == TARGET_STATUS.casefold():
                    yield history["id"], when, (history.get("author") or {}).get("displayName", "")
        start += len(histories)
        if page.get("isLast", True) or not histories:
            return


def fingerprint(history_id):
    return hashlib.sha256(f"{JIRA_BASE_URL}|{history_id}".encode()).hexdigest()[:20]


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state, now):
    keep_after = now - timedelta(hours=LOOKBACK_HOURS * 2)
    kept = {k: v for k, v in state.items() if datetime.fromisoformat(v) >= keep_after}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(kept, indent=2, sort_keys=True) + "\n")


def absolutize_links(fragment):
    return re.sub(r'(href|src)="/', rf'\1="{JIRA_BASE_URL}/', fragment)


def build_email(issue, when, who):
    key = issue["key"]
    summary = issue["fields"].get("summary") or ""
    url = f"{JIRA_BASE_URL}/browse/{key}"
    description = (issue.get("renderedFields") or {}).get("description") or "<p><em>(No description)</em></p>"
    moved = when.strftime("%Y-%m-%d %H:%M %z") + (f" by {who}" if who else "")

    message = EmailMessage()
    message["Subject"] = f"[{TARGET_STATUS}] {key}: {summary}"
    message["From"] = SMTP_USER
    message["To"] = MAIL_TO
    message.set_content(f"{key}: {summary}\n{url}\nMoved to {TARGET_STATUS}: {moved}\n\nOpen this email as HTML to read the description.")
    message.add_alternative(
        f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#172b4d">
<p style="color:#5e6c84;margin:0">Moved to <b>{html.escape(TARGET_STATUS)}</b>: {html.escape(moved)}</p>
<h2 style="margin:8px 0"><a href="{url}">{html.escape(key)}</a>: {html.escape(summary)}</h2>
<hr>
<h3>Description</h3>
{absolutize_links(description)}
</body></html>""",
        subtype="html",
    )
    return message


def main():
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=LOOKBACK_HOURS)
    state = load_state()

    jql = f'status CHANGED TO "{TARGET_STATUS}" AFTER -{LOOKBACK_HOURS}h'
    if JQL_FILTER:
        jql = f"({JQL_FILTER}) AND {jql}"

    pending = []
    for issue in search_issues(jql):
        for history_id, when, who in transitions_into_target(issue["key"], since):
            fid = fingerprint(history_id)
            if fid not in state:
                pending.append((when, fid, issue, who))
    pending.sort(key=lambda entry: entry[0])
    print(f"{len(pending)} new ticket move(s) into {TARGET_STATUS!r}")

    if DRY_RUN:
        for when, _, issue, _ in pending:
            print(f"  would email: {issue['key']} ({when.isoformat()})")
        return

    try:
        if pending:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context()) as smtp:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
                for when, fid, issue, who in pending:
                    smtp.send_message(build_email(issue, when, who))
                    state[fid] = now.isoformat()
    finally:
        save_state(state, now)


if __name__ == "__main__":
    main()
