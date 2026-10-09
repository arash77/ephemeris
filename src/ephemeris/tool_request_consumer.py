#!/usr/bin/env python
"""Turn Galaxy tool installation requests into work for a tool-list repository.

Users ask for tools through Galaxy's tool installation request form or from a
workflow that misses tools, and every admin receives a
``tool_installation_request`` notification. A scheduled CI job in the
instance's tool-list repository runs this command with an admin API key:

``emit``
    Print a JSON list of work items, one per unseen request. A request that pins
    a version of every tool, each found in an installable revision on the Tool
    Shed, becomes a ``pr`` item; everything else an ``issue``.
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
import os
import sys
from functools import cache

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
DEFAULT_TOOL_SHED = "https://toolshed.g2.bx.psu.edu/"
DEFAULT_PANEL_LABEL = "Tools from user request"


class Unresolved(Exception):
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
    return ToolShedInstance(url=url)


def _find_repository(shed_url: str, name: str | None) -> tuple[str, str]:
    """Find the repository named exactly like a free-text tool name."""
    if not name:
        raise Unresolved("no tool name to search the Tool Shed for")
    hits = _shed(shed_url).repositories.search_repositories(q=name, page_size=100)["hits"]
    matches = sorted(
        {
            (hit["repository"]["repo_owner_username"], hit["repository"]["name"])
            for hit in hits
            if hit["repository"]["name"].lower() == name.lower()
        }
    )
    if not matches:
        raise Unresolved(f"no repository named {name} on {shed_url}")
    if len(matches) > 1:
        raise Unresolved("several repositories match: " + ", ".join(f"{o}/{n}" for o, n in matches))
    return matches[0]


def _find_revision(shed_url: str, repository_id: str, version: str, tool_id: str | None) -> str:
    """Return the newest installable revision that ships the requested tool version."""
    shed = _shed(shed_url)
    response = shed.make_get_request(
        f"{shed.url}/repositories/{repository_id}/metadata", params={"downloadable_only": True}
    )
    response.raise_for_status()
    for revision in sorted(response.json().values(), key=lambda r: r["numeric_revision"], reverse=True):
        for tool in revision.get("tools", []):
            if tool["version"] == version and tool_id in (None, tool["id"]):
                return revision["changeset_revision"]
    raise Unresolved(f"no installable revision has version {version}")


def _resolve(tool: dict, shed_url: str) -> tuple[str, str, str | None]:
    """Return the owner, repository name and pinned revision of a requested tool."""
    tool_id = None
    if tool.get("tool_shed_id"):
        # Workflow requests name the repository as <shed>/repos/<owner>/<name> and the tool by id.
        host, _, repository = tool["tool_shed_id"].partition("/repos/")
        owner, _, name = repository.partition("/")
        if not (owner and name):
            raise Unresolved(f"cannot parse Tool Shed id {tool['tool_shed_id']}")
        # Users choose the workflow, so only look tools up on the Tool Shed the admins chose.
        if format_tool_shed_url(host).split("://")[-1].lower() != shed_url.split("://")[-1].lower():
            raise Unresolved(f"{host} is not {shed_url}")
        tool_id = tool.get("name")
    else:
        owner, name = _find_repository(shed_url, tool.get("name"))
    repositories = _shed(shed_url).repositories.get_repositories(name=name, owner=owner)
    if not repositories:
        raise Unresolved(f"{owner}/{name} is not on {shed_url}")
    revision = None
    if tool.get("requested_version"):
        revision = _find_revision(shed_url, repositories[0]["id"], tool["requested_version"], tool_id)
    return owner, name, revision


def _label(tool: dict) -> str:
    name = tool.get("name") or tool.get("tool_shed_id") or tool.get("tool_url") or "unnamed tool"
    return " ".join(filter(None, [name, tool.get("requested_version")]))


def _code(text: str) -> str:
    """Render user text as code, so it cannot mention people or inject markup."""
    return "`" + " ".join(text.split()).replace("`", "'").replace("|", "/") + "`"


def _code_block(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}\n{text.rstrip()}\n{fence}"


def emit_item(notification: dict, tool_shed_url: str, panel_label: str, label_map: dict | None = None) -> dict:
    content = notification["content"]
    entries: dict[tuple[str, str], dict] = {}
    rows = []
    failed = False
    for tool in content["tools"]:
        try:
            owner, name, revision = _resolve(tool, tool_shed_url)
        except Unresolved as e:
            failed = True
            rows.append(f"| {_code(_label(tool))} | not resolved: {_code(str(e))} | |")
            continue
        entry = entries.setdefault(
            (owner, name),
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

    # Only requests that pin every tool to an installable revision skip the admins' judgement.
    pinned = all(t.get("requested_version") for t in content["tools"])
    action = "pr" if pinned and entries and not failed else "issue"

    labels = [_label(t) for t in content["tools"]]
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
    for tool in content["tools"]:
        details = [f"{key}: {tool[key]}" for key in ("tool_url", "scientific_domain", "description") if tool.get(key)]
        if details:
            body += ["", f"Details for {_code(_label(tool))}:", _code_block("\n".join(details))]
    if content.get("additional_remarks"):
        body += ["", "Remarks:", _code_block(content["additional_remarks"])]
    if entries:
        body += [
            "",
            "Tool list entries:",
            _code_block(yaml.safe_dump({"tools": list(entries.values())}, sort_keys=False)),
        ]
    text = "\n".join(body)
    if len(text) > MAX_BODY_LENGTH:
        text = text[: MAX_BODY_LENGTH - 100] + "\n\n(Truncated, the full request is in Galaxy.)"
    return {
        "notification_id": notification["id"],
        "action": action,
        "title": title,
        "body": text,
        "tools": list(entries.values()),
    }


def _repository_key(entry: dict) -> tuple[str, str, str]:
    return format_tool_shed_url(entry.get("tool_shed_url") or DEFAULT_TOOL_SHED), entry["owner"], entry["name"]


def merge_tool_list(existing: dict | None, items: list[dict]) -> dict:
    """Add the tools of work items to a tool list, extending the revisions of repositories already listed."""
    merged = dict(existing or {})
    tools = [dict(t) for t in merged.get("tools") or []]
    by_repository = {_repository_key(t): t for t in tools}
    for item in items:
        for entry in item["tools"]:
            current = by_repository.get(_repository_key(entry))
            if current is None:
                current = by_repository[_repository_key(entry)] = (
                    dict(entry, revisions=[]) if "revisions" in entry else dict(entry)
                )
                tools.append(current)
            if "revisions" not in current:
                # Without revisions the entry already follows the latest revision.
                continue
            current["revisions"] = current["revisions"] + [
                r for r in entry.get("revisions", []) if r not in current["revisions"]
            ]
    merged["tools"] = tools
    return merged


def fetch_notifications(gi, page_size: int = 100) -> list[dict]:
    """Read all of the user's notifications, page by page."""
    notifications: dict[str, dict] = {}
    offset = 0
    while True:
        response = gi.make_get_request(f"{gi.url}/notifications", params={"limit": page_size, "offset": offset})
        response.raise_for_status()
        page = response.json()
        # Galaxy does not order this list, so pages may overlap.
        notifications.update((n["id"], n) for n in page)
        if len(page) < page_size:
            return list(notifications.values())
        offset += page_size


def reply(gi, notification_id: str, url: str) -> bool:
    """Send the requester the URL, or return False if their account is gone."""
    response = gi.make_get_request(f"{gi.url}/notifications/{notification_id}")
    response.raise_for_status()
    email = response.json()["content"].get("requester_email")
    # The f_email filter matches substrings.
    user_ids = (
        [u["id"] for u in gi.users.get_users(f_email=email) if u["email"].lower() == email.lower()] if email else []
    )
    if not user_ids:
        return False
    gi.make_post_request(
        f"{gi.url}/notifications",
        payload={
            "recipients": {"user_ids": user_ids},
            "notification": {
                "source": "tool_installation_request_consumer",
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
    return True


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
        output = yaml.safe_dump(merge_tool_list(existing, items), sort_keys=False)
        if args.output:
            with open(args.output, "w") as f:
                f.write(output)
        else:
            sys.stdout.write(output)
        return

    gi = get_galaxy_connection(args, log=log, login_required=True)
    if args.command == "emit":
        label_map = get_dictionary_from_json(args.panel_label_json) if args.panel_label_json else None
        shed_url = format_tool_shed_url(args.tool_shed_url)
        items = []
        for notification in select_requests(fetch_notifications(gi), include_seen=args.include_seen):
            try:
                item = emit_item(notification, shed_url, args.panel_label, label_map)
            except (BioblendError, OSError, ValueError) as e:
                log.warning(f"Skipping {notification['id']} until the Tool Shed answers: {e}")
                continue
            log.info(f"{item['notification_id']}: {item['action']} {item['title']}")
            items.append(item)
        json.dump(items, sys.stdout, indent=2)
        sys.stdout.write("\n")
    elif args.command == "reply":
        if not reply(gi, args.notification_id, args.url):
            log.warning(f"No Galaxy user matches the requester of {args.notification_id}, not replying")
    elif args.command == "mark-seen":
        mark_seen(gi, args.notification_ids)


if __name__ == "__main__":
    main()
