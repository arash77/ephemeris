#!/usr/bin/env python
"""Turn Galaxy tool installation requests into work for a tool-list repository.

Users ask for tools through Galaxy's tool installation request form or from a
workflow that misses tools, and every admin receives a
``tool_installation_request`` notification. A scheduled CI job in the
instance's tool-list repository runs this command with an admin API key:

``emit``
    Print a JSON list of work items, one per unseen request. A request that pins
    a version of every tool, each found in an installable revision on the Tool
    Shed, becomes a ``pr`` item; everything else an ``issue``. While the Tool
    Shed fails, requests are left for the next run, for up to a day.
``render-yaml``
    Merge the tools of ``pr`` items into a tool list file.
``reply``
    Tell the requester where their request is handled.
``mark-seen``
    Mark requests as seen, so the next ``emit`` skips them.

The CI job opens the issues and pull requests itself, so each instance keeps
its own repository layout and review process.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from functools import cache
from urllib.parse import quote

import yaml
from bioblend import ConnectionError as BioblendError
from bioblend.toolshed import ToolShedInstance

from . import (
    get_galaxy_connection,
    load_yaml_file,
)
from .common_parser import (
    add_log_file_argument,
    add_verbosity_argument,
    get_common_args,
    HideUnderscoresHelpFormatter,
)
from .ephemeris_log import (
    disable_external_library_logging,
    setup_global_logger,
)
from .generate_tool_list_from_ga_workflow_files import get_dictionary_from_json
from .shed_tools_methods import format_tool_shed_url

CATEGORY = "tool_installation_request"
# GitHub's limit for issue and pull request bodies.
MAX_BODY_LENGTH = 65536
# Longest user text shown in table cells and headings.
MAX_INLINE_LENGTH = 300
TRUNCATED = "(Truncated, the full request is in Galaxy.)"
# Longest user text put into a wrapper request link, which browsers and forges limit in length.
MAX_LINK_TEXT_LENGTH = 500
DEFAULT_TOOL_SHED = "https://toolshed.g2.bx.psu.edu/"
DEFAULT_PANEL_LABEL = "Tools from user request"
# Seconds to wait for each Tool Shed response.
TOOL_SHED_TIMEOUT = 60
# How long a request waits for the Tool Shed to answer before it becomes an issue anyway.
TOOL_SHED_PATIENCE = timedelta(days=1)
TOOL_SHED_ERRORS = (BioblendError, OSError, ValueError)
# Seconds to wait for a Galaxy that sends notifications in a Celery task.
REPLY_TIMEOUT = 60
# The characters the Tool Shed allows in repository names.
REPOSITORY_NAME = re.compile(r"[a-z0-9_]+")

log = logging.getLogger(__name__)


class Unresolved(Exception):
    def __init__(self, message: str, not_on_tool_shed: bool = False):
        super().__init__(message)
        # No repository is named like the tool, so it may not be wrapped yet.
        self.not_on_tool_shed = not_on_tool_shed


class ReplyFailed(Exception):
    pass


def select_requests(notifications: list[dict], include_seen: bool = False) -> list[dict]:
    """Keep the tool installation requests in an admin's notifications.

    Submitters get their confirmation copy in their own inbox, so an admin's
    inbox only holds the requests themselves.
    """
    return [
        n
        for n in notifications
        if n.get("category") == CATEGORY and not n.get("deleted") and (include_seen or not n.get("seen_time"))
    ]


@cache
def _shed(url: str) -> ToolShedInstance:
    shed = ToolShedInstance(url=url)
    shed.timeout = TOOL_SHED_TIMEOUT
    return shed


def _raise_if_rejected(status: int | None) -> None:
    """Treat the Tool Shed rejecting a lookup as unresolved, since asking again gets the same answer.

    Other failures propagate, so that the next run tries the request again.
    """
    if status is not None and 400 <= status < 500 and status not in (408, 429):
        raise Unresolved(f"the Tool Shed rejected the lookup with HTTP {status}")


@cache
def _repositories(shed_url: str, name: str, owner: str | None = None) -> list[dict]:
    try:
        return _shed(shed_url).repositories.get_repositories(name=name, owner=owner)
    except BioblendError as e:
        _raise_if_rejected(e.status_code)
        raise


@cache
def _installable_revisions(shed_url: str, repository_id: str) -> list[dict]:
    shed = _shed(shed_url)
    response = shed.make_get_request(
        f"{shed.url}/repositories/{repository_id}/metadata", params={"downloadable_only": True}
    )
    _raise_if_rejected(response.status_code)
    response.raise_for_status()
    return list(response.json().values())


def _find_repository(shed_url: str, name: str | None) -> dict:
    """Find the repository named exactly like a free-text tool name."""
    if not name:
        raise Unresolved("no tool name to look the repository up by")
    # An exact lookup is fast whatever users type, unlike the Tool Shed's full-text search. Repository
    # names only hold lower case letters, digits and underscores, so try the name in that form too.
    typed = name.strip().lower()
    keys = [
        k for k in dict.fromkeys([typed, re.sub(r"[^a-z0-9]+", "_", typed).strip("_")]) if REPOSITORY_NAME.fullmatch(k)
    ]
    matches = next((found for found in (_repositories(shed_url, key) for key in keys) if found), [])
    if not matches:
        raise Unresolved(f"no repository named {name} on {shed_url}", not_on_tool_shed=True)
    if len(matches) > 1:
        raise Unresolved(
            "several repositories match: " + ", ".join(sorted(f"{r['owner']}/{r['name']}" for r in matches))
        )
    return matches[0]


def _find_revision(shed_url: str, repository_id: str, version: str, tool_id: str | None) -> str:
    """Return the newest installable revision that ships the requested tool version."""
    revisions = _installable_revisions(shed_url, repository_id)
    # Legacy revisions can lack a numeric revision.
    for revision in sorted(revisions, key=lambda r: r["numeric_revision"] or -1, reverse=True):
        for tool in revision.get("tools", []):
            if tool["version"] == version and tool_id in (None, tool["id"]):
                return revision["changeset_revision"]
    raise Unresolved(f"no installable revision has version {version}")


def _without_scheme(url: str) -> str:
    return re.sub(r"^https?://", "", url, flags=re.IGNORECASE).rstrip("/").lower()


def _resolve(tool: dict, shed_url: str) -> tuple[str, str, str | None]:
    """Return the owner, repository name and pinned revision of a requested tool, as the Tool Shed spells them."""
    tool_id = None
    if tool.get("tool_shed_id"):
        # Workflow requests name the repository as <shed>/repos/<owner>/<name> and the tool by id.
        host, _, path = tool["tool_shed_id"].partition("/repos/")
        owner, _, name = path.partition("/")
        if not (owner and name):
            raise Unresolved(f"cannot parse Tool Shed id {tool['tool_shed_id']}")
        # Users choose the workflow, so only look tools up on the Tool Shed the admins chose.
        if _without_scheme(host) != _without_scheme(shed_url):
            raise Unresolved(f"{host} is not {shed_url}")
        repositories = _repositories(shed_url, name, owner)
        if not repositories:
            raise Unresolved(f"{owner}/{name} is not on {shed_url}")
        repository = repositories[0]
        tool_id = tool.get("name")
    else:
        repository = _find_repository(shed_url, tool.get("name"))
    revision = None
    if tool.get("requested_version"):
        revision = _find_revision(shed_url, repository["id"], tool["requested_version"], tool_id)
    return repository["owner"], repository["name"], revision


def _label(tool: dict) -> str:
    name = tool.get("name") or tool.get("tool_shed_id") or tool.get("tool_url") or "unnamed tool"
    return " ".join(filter(None, [name, tool.get("requested_version")]))


def _code(text: str) -> str:
    """Render user text as code, so it cannot mention people or inject markup."""
    text = " ".join(text.split()).replace("`", "'").replace("|", "/")
    if len(text) > MAX_INLINE_LENGTH:
        text = text[: MAX_INLINE_LENGTH - 3] + "..."
    return f"`{text}`"


def _code_block(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}\n{text.rstrip()}\n{fence}"


def _wrapper_request_link(template: str, tool: dict) -> str:
    """Fill a URL template, such as a new issue's, with what the user said about a tool."""
    name, url, description, domain = (
        (tool.get(key) or "")[:MAX_LINK_TEXT_LENGTH] for key in ("name", "tool_url", "description", "scientific_domain")
    )
    body = "\n\n".join(
        filter(
            None,
            [
                description,
                url and f"Homepage: {url}",
                domain and f"Scientific domain: {domain}",
                "A Galaxy user asked for this tool in a tool installation request.",
            ],
        )
    )
    values = {
        "name": name,
        "url": url,
        "description": description,
        "domain": domain,
        "title": f"Tool request: {name}",
        "body": body,
    }
    for key, value in values.items():
        template = template.replace(f"{{{key}}}", quote(value, safe=""))
    return template


def _size(lines: list[str]) -> int:
    """Return how much joining ``lines`` onto a body with newlines adds to it."""
    return sum(len(line) + 1 for line in lines)


def emit_item(
    notification: dict,
    tool_shed_url: str,
    panel_label: str,
    label_map: dict | None = None,
    tool_shed_error: Exception | None = None,
    wrapper_request_url: str | None = None,
) -> dict:
    """Turn a request into a work item.

    Tool Shed failures propagate, so that the request can be tried again later,
    unless ``tool_shed_error`` is given: then no tool is looked up, and the
    request becomes an issue that shows the error. For tools that no Tool Shed
    repository is named like, the issue links to ``wrapper_request_url`` filled
    in by :func:`_wrapper_request_link`.
    """
    content = notification["content"]
    entries: dict[tuple[str, str, bool], dict] = {}
    rows = []
    wrapper_links = []
    confirmed_labels = []
    failed = False
    for tool in content["tools"]:
        try:
            if tool_shed_error is not None:
                raise Unresolved(f"not looked up, the Tool Shed failed: {tool_shed_error}")
            owner, name, revision = _resolve(tool, tool_shed_url)
        except Unresolved as e:
            failed = True
            rows.append(f"| {_code(_label(tool))} | not resolved: {_code(str(e))} | |")
            if wrapper_request_url and e.not_on_tool_shed:
                link = _wrapper_request_link(wrapper_request_url, tool)
                wrapper_links.append(f"- [Request a wrapper for {_code(tool['name'])}]({link})")
            continue
        # A tool without a version follows the latest revision, so it needs an entry without revisions.
        entry = entries.setdefault(
            (owner, name, revision is None),
            {
                "name": name,
                "owner": owner,
                "tool_shed_url": tool_shed_url,
                "tool_panel_section_label": (label_map or {}).get(f"{owner}/{name}", panel_label),
            },
        )
        if revision and revision not in entry.setdefault("revisions", []):
            entry["revisions"].append(revision)
        rows.append(f"| {_code(_label(tool))} | {owner}/{name} | {revision or 'latest'} |")
        # A revision is only found for a version the Tool Shed lists, so this label holds no user text.
        confirmed_labels.append(f"{name} {tool['requested_version']}" if revision else name)

    # Only requests that pin every tool to an installable revision skip the admins' judgement.
    pinned = all(t.get("requested_version") for t in content["tools"])
    action = "pr" if pinned and entries and not failed else "issue"

    # A pull request's title becomes its commit message, where GitHub acts on keywords like "Fixes #1".
    labels = confirmed_labels if action == "pr" else [" ".join(_label(t).split()) for t in content["tools"]]
    labels = list(dict.fromkeys(labels))
    title = "Install " + ", ".join(labels[:3]) + (f" and {len(labels) - 3} more" if len(labels) > 3 else "")
    if len(title) > 256:
        title = title[:253] + "..."
    body = [
        f"Tool installation request from Galaxy (notification `{notification['id']}`).",
        "",
        "| Requested tool | Repository | Revision |",
        "| --- | --- | --- |",
        *rows,
    ]
    if content.get("workflow_id"):
        body += ["", f"Needed by workflow `{content['workflow_id']}`."]
    tool_list = []
    if entries:
        tool_list = [
            "",
            "Tool list entries:",
            _code_block(yaml.safe_dump({"tools": list(entries.values())}, sort_keys=False)),
        ]
    free_text = []
    for tool in content["tools"]:
        details = [f"{key}: {tool[key]}" for key in ("tool_url", "scientific_domain", "description") if tool.get(key)]
        if details:
            free_text.append((f"Details for {_code(_label(tool))}:", "\n".join(details)))
    if content.get("additional_remarks"):
        free_text.append(("Remarks:", content["additional_remarks"]))

    # Wrapper links, then free text, get what is left of GitHub's limit. Free text is cut inside its
    # code block, so that it stays code.
    room = MAX_BODY_LENGTH - _size(body) + 1 - _size(tool_list) - _size(["", TRUNCATED])
    truncated = False
    if wrapper_links:
        section = ["", "The Tool Shed has no repository named like these tools. If they are not wrapped yet:"]
        for link in wrapper_links:
            if _size(section) + _size([link]) > room:
                truncated = True
                break
            section.append(link)
        if len(section) > 2:
            body += section
            room -= _size(section)
    for heading, text in [] if truncated else free_text:
        block = ["", heading, _code_block(text)]
        if _size(block) > room:
            block[2] = _code_block(text[: max(0, len(text) - (_size(block) - room))])
            if _size(block) <= room:
                body += block
            truncated = True
            break
        body += block
        room -= _size(block)
    if truncated:
        body += ["", TRUNCATED]
    body += tool_list
    return {
        "notification_id": notification["id"],
        "action": action,
        "title": title,
        "body": "\n".join(body),
        "tools": list(entries.values()),
    }


def _waited(notification: dict) -> timedelta:
    published = datetime.fromisoformat(notification["publication_time"])
    if published.tzinfo is None:
        # Galaxy reports UTC without a time zone.
        published = published.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - published


def _status(error: Exception) -> int | None:
    return getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)


def emit(
    notifications: list[dict],
    tool_shed_url: str,
    panel_label: str,
    label_map: dict | None = None,
    wrapper_request_url: str | None = None,
) -> list[dict]:
    """Turn requests into work items, leaving those that wait for the Tool Shed to a later run."""
    items = []
    # Set once the Tool Shed does not answer at all, so that the run stops waiting for it.
    unreachable: Exception | None = None
    for notification in notifications:
        error = unreachable
        if error is None:
            try:
                items.append(emit_item(notification, tool_shed_url, panel_label, label_map, None, wrapper_request_url))
                continue
            except TOOL_SHED_ERRORS as e:
                error = e
                if _status(e) is None:
                    unreachable = e
        if _waited(notification) < TOOL_SHED_PATIENCE:
            log.warning(f"Skipping {notification['id']} until the Tool Shed answers: {error}")
            continue
        items.append(emit_item(notification, tool_shed_url, panel_label, label_map, error, wrapper_request_url))
    return items


def _repository_key(entry: dict) -> tuple[str, str, str]:
    return _without_scheme(entry.get("tool_shed_url") or DEFAULT_TOOL_SHED), entry["owner"], entry["name"]


def _pinned_revisions(entry: dict) -> list:
    return entry.get("revisions") or ([entry["changeset_revision"]] if entry.get("changeset_revision") else [])


def merge_tool_list(existing: dict | None, items: list[dict]) -> dict:
    """Add the tools of ``pr`` items to a tool list, extending the revisions of repositories already listed.

    Like shed-tools, an entry pins the revisions in ``revisions``, or else the one in
    ``changeset_revision``, and follows the latest revision without either. A
    repository with an entry that follows the latest revision is left alone, and a
    ``changeset_revision`` pin that gains revisions becomes a ``revisions`` list.
    """
    merged = dict(existing or {})
    tools = [dict(t) for t in merged.get("tools") or []]
    by_repository: dict[tuple[str, str, str], list[dict]] = {}
    for tool in tools:
        by_repository.setdefault(_repository_key(tool), []).append(tool)
    # Issue items wait for the admins' judgement.
    for item in (i for i in items if i["action"] == "pr"):
        for entry in item["tools"]:
            listed = by_repository.setdefault(_repository_key(entry), [])
            if not listed:
                listed.append(dict(entry))
                tools.append(listed[-1])
                continue
            pins = [_pinned_revisions(t) for t in listed]
            if not all(pins):
                continue
            # YAML loads unquoted revisions made of digits as numbers.
            known = {str(r) for pin in pins for r in pin}
            new = [r for r in entry.get("revisions", []) if r not in known]
            if new:
                listed[-1]["revisions"] = list(pins[-1]) + new
                listed[-1].pop("changeset_revision", None)
    merged["tools"] = tools
    return merged


def fetch_notifications(gi) -> list[dict]:
    """Read all of the user's notifications."""
    # Galaxy does not order this list, so paging through it could miss notifications. A limit of 0 returns all.
    response = gi.make_get_request(f"{gi.url}/notifications", params={"limit": 0})
    response.raise_for_status()
    return response.json()


def reply(gi, notification_id: str, url: str) -> bool:
    """Send the requester the URL, or return False if their account is gone or they turned messages off."""
    response = gi.make_get_request(f"{gi.url}/notifications/{notification_id}")
    response.raise_for_status()
    email = response.json()["content"].get("requester_email")
    # The f_email filter matches substrings.
    user_ids = (
        [u["id"] for u in gi.users.get_users(f_email=email) if u["email"].lower() == email.lower()] if email else []
    )
    if not user_ids:
        return False
    response = gi.make_post_request(
        f"{gi.url}/notifications",
        payload={
            "recipients": {"user_ids": user_ids},
            "notification": {
                # Galaxy stores up to 32 characters.
                "source": "tool_installation_request_reply",
                "category": "message",
                "variant": "info",
                "content": {
                    "category": "message",
                    "subject": "Your tool installation request",
                    "message": f"Your tool installation request is being handled here: {url}",
                },
            },
        },
    )
    if "total_notifications_sent" in response:
        # Galaxy skips users who turned message notifications off.
        return response["total_notifications_sent"] > 0
    # With Celery, Galaxy answers before it sends, so only the task tells whether sending worked.
    _wait_for_task(gi, response["id"])
    return True


def _wait_for_task(gi, task_id: str) -> None:
    deadline = time.monotonic() + REPLY_TIMEOUT
    while True:
        response = gi.make_get_request(f"{gi.url}/tasks/{task_id}/state")
        response.raise_for_status()
        state = response.json()
        if state == "SUCCESS":
            return
        if state == "FAILURE":
            raise ReplyFailed(f"Galaxy failed to send the reply, see its log for task {task_id}")
        if time.monotonic() > deadline:
            raise ReplyFailed(f"Galaxy did not send the reply within {REPLY_TIMEOUT} seconds (task {task_id})")
        time.sleep(1)


def mark_seen(gi, notification_ids: list[str]) -> None:
    gi.make_put_request(
        f"{gi.url}/notifications", payload={"notification_ids": notification_ids, "changes": {"seen": True}}
    )


def _parser():
    parser = argparse.ArgumentParser(
        description="Turn Galaxy tool installation requests into issues and pull requests for a tool list repository.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    galaxy_arguments = get_common_args(log_file=True)

    emit = subparsers.add_parser(
        "emit",
        parents=[galaxy_arguments],
        formatter_class=HideUnderscoresHelpFormatter,
        help="Print a JSON list of work items for unseen requests.",
    )
    emit.add_argument("--tool-shed-url", default=DEFAULT_TOOL_SHED, help="Tool Shed to look requested tools up on.")
    emit.add_argument("--include-seen", action="store_true", help="Also emit requests already marked as seen.")
    emit.add_argument(
        "-l",
        "--panel-label",
        "--panel_label",
        dest="panel_label",
        default=DEFAULT_PANEL_LABEL,
        help="Tool panel section label for requested tools.",
    )
    emit.add_argument(
        "-j",
        "--json-panel-label-suggestion",
        "--json_panel_label_suggestion",
        dest="panel_label_json",
        help="JSON file or URL mapping 'owner/name' to a tool panel section label, overriding -l.",
    )
    emit.add_argument(
        "--wrapper-request-url",
        help="URL that issues link to for tools no Tool Shed repository is named like, such as a new issue in "
        "a tool development repository. {name}, {url}, {description}, {domain}, {title} and {body} in it "
        "are replaced with what the user said about the tool.",
    )

    # render-yaml needs no Galaxy.
    render = subparsers.add_parser(
        "render-yaml",
        formatter_class=HideUnderscoresHelpFormatter,
        help="Merge the tools of work items into a tool list.",
    )
    add_verbosity_argument(render)
    add_log_file_argument(render)
    render.add_argument("--items", help="Work items JSON (default: stdin).")
    render.add_argument("--tools-file", help="Tool list to merge into.")
    render.add_argument("-o", "--output", help="Where to write the tool list (default: stdout).")

    reply = subparsers.add_parser(
        "reply",
        parents=[galaxy_arguments],
        formatter_class=HideUnderscoresHelpFormatter,
        help="Send the requester the URL of their issue or pull request.",
    )
    reply.add_argument("--notification-id", required=True, help="The request to answer.")
    reply.add_argument("--url", required=True, help="Issue or pull request URL.")

    mark_seen = subparsers.add_parser(
        "mark-seen",
        parents=[galaxy_arguments],
        formatter_class=HideUnderscoresHelpFormatter,
        help="Mark requests as seen.",
    )
    mark_seen.add_argument("notification_ids", nargs="+", metavar="notification_id", help="Requests to mark.")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    log = setup_global_logger(__name__, log_file=args.log_file, verbose=args.verbose)
    disable_external_library_logging()

    if args.command == "render-yaml":
        if args.items:
            with open(args.items) as f:
                items = json.load(f)
        else:
            items = json.load(sys.stdin)
        existing = load_yaml_file(args.tools_file) if args.tools_file and os.path.exists(args.tools_file) else None
        merged = merge_tool_list(existing, items)
        if existing is not None and merged == existing:
            # Keep the comments and formatting of a tool list that gains nothing.
            with open(args.tools_file) as f:
                output = f.read()
        else:
            output = yaml.safe_dump(merged, sort_keys=False)
        if args.output:
            with open(args.output, "w") as f:
                f.write(output)
        else:
            sys.stdout.write(output)
        return

    gi = get_galaxy_connection(args, log=log, login_required=True)
    if args.command == "emit":
        label_map = get_dictionary_from_json(args.panel_label_json) if args.panel_label_json else None
        pending = select_requests(fetch_notifications(gi), include_seen=args.include_seen)
        shed_url = format_tool_shed_url(args.tool_shed_url)
        items = emit(pending, shed_url, args.panel_label, label_map, args.wrapper_request_url)
        for item in items:
            log.info(f"{item['notification_id']}: {item['action']} {item['title']}")
        json.dump(items, sys.stdout, indent=2)
        sys.stdout.write("\n")
    elif args.command == "reply":
        try:
            sent = reply(gi, args.notification_id, args.url)
        except ReplyFailed as e:
            log.error(f"{e}, so {args.notification_id} stays unseen")
            return 1
        if not sent:
            log.warning(
                f"The requester of {args.notification_id} has no Galaxy account anymore or turned messages off,"
                " so they were not told"
            )
    elif args.command == "mark-seen":
        mark_seen(gi, args.notification_ids)


if __name__ == "__main__":
    sys.exit(main())
