#!/usr/bin/env python3
"""Email a digest of every Jira ticket currently in the target status (default "Ready For QA")."""

import base64
import html
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage


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
DRY_RUN = env("DRY_RUN", "") == "1"

if not DRY_RUN:
    SMTP_USER = env("SMTP_USER")
    SMTP_PASSWORD = env("SMTP_PASSWORD")
    MAIL_TO = env("MAIL_TO")
    SMTP_HOST = env("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(env("SMTP_PORT", "465"))

AUTH_HEADER = "Basic " + base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
VIETNAM_TIME = timezone(timedelta(hours=7))


def search_issues(jql):
    issues, token = [], None
    while True:
        body = {"jql": jql, "fields": ["summary", "description"], "expand": "renderedFields", "maxResults": 50}
        if token:
            body["nextPageToken"] = token
        request = urllib.request.Request(
            JIRA_BASE_URL + "/rest/api/3/search/jql",
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": AUTH_HEADER,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                page = json.load(response)
        except urllib.error.HTTPError as error:
            sys.exit(f"Jira API error {error.code}: {error.read().decode(errors='replace')}")
        issues.extend(page.get("issues", []))
        token = page.get("nextPageToken")
        if not token:
            return issues


def absolutize_links(fragment):
    return re.sub(r'(href|src)="/', rf'\1="{JIRA_BASE_URL}/', fragment)


def issue_section(issue):
    key = issue["key"]
    summary = issue["fields"].get("summary") or ""
    description = (issue.get("renderedFields") or {}).get("description") or "<p><em>(No description)</em></p>"
    return f"""<div style="border:1px solid #dfe1e6;border-radius:6px;padding:12px 16px;margin:0 0 16px">
<h2 style="margin:0 0 8px;font-size:18px"><a href="{JIRA_BASE_URL}/browse/{key}">{html.escape(key)}</a>: {html.escape(summary)}</h2>
{absolutize_links(description)}
</div>"""


def build_email(issues, today):
    count = len(issues)
    message = EmailMessage()
    message["Subject"] = f"[{TARGET_STATUS}] {count} ticket{'s' if count != 1 else ''} — {today}"
    message["From"] = SMTP_USER
    message["To"] = MAIL_TO
    message.set_content(
        "\n".join(f"{i['key']}: {i['fields'].get('summary') or ''}\n{JIRA_BASE_URL}/browse/{i['key']}" for i in issues)
        + "\n\nOpen this email as HTML to read the descriptions."
    )
    message.add_alternative(
        f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#172b4d">
<p>{count} ticket{'s' if count != 1 else ''} in <b>{html.escape(TARGET_STATUS)}</b> as of {today}.</p>
{''.join(issue_section(issue) for issue in issues)}
</body></html>""",
        subtype="html",
    )
    return message


def main():
    jql = f'status = "{TARGET_STATUS}"'
    if JQL_FILTER:
        jql = f"({JQL_FILTER}) AND {jql}"
    issues = search_issues(jql + " ORDER BY key ASC")
    print(f"{len(issues)} ticket(s) in {TARGET_STATUS!r}")

    if not issues or DRY_RUN:
        return

    today = datetime.now(VIETNAM_TIME).strftime("%Y-%m-%d")
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context()) as smtp:
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(build_email(issues, today))


if __name__ == "__main__":
    main()
