import json
import pathlib
import re
from types import SimpleNamespace

import pytest
import yaml
from bioblend import ConnectionError as BioblendError

from ephemeris.tool_request_consumer import (
    emit_item,
    fetch_notifications,
    main,
    reply,
    select_requests,
)

NOTIFICATIONS = json.loads((pathlib.Path(__file__).parent / "tool_installation_requests.json").read_text())
SHED = "https://toolshed.g2.bx.psu.edu/"
LABEL = "Tools from user request"


def _emit(notification_id, tools=None):
    notification = next(n for n in NOTIFICATIONS if n["id"] == notification_id)
    if tools is not None:
        notification = dict(notification, content=dict(notification["content"], tools=tools))
    return emit_item(notification, SHED, LABEL)


def test_emit_pins_requested_versions_in_a_pr():
    form = _emit("b2486a20bc56b90f")
    assert form["action"] == "pr"
    assert form["tools"] == [
        {
            "name": "fastp",
            "owner": "iuc",
            "tool_shed_url": SHED,
            "tool_panel_section_label": LABEL,
            "revisions": ["68386e630362"],
        }
    ]
    assert "toolrequester@galaxy.test" not in json.dumps(form)

    workflow = _emit("0aab0e4c25198ad8")
    assert workflow["action"] == "pr"
    assert [(t["owner"], t["name"], t["revisions"]) for t in workflow["tools"]] == [
        ("jjohnson", "regex_find_replace", ["9ea374bb0350"]),
        ("kellrott", "regex_replace", ["9a77d5fca67c"]),
    ]

    # Two tools from one repository become one entry.
    same_repo = _emit("5ba8d2a3c1e7f046")
    assert [(t["owner"], t["name"], t["revisions"]) for t in same_repo["tools"]] == [
        ("devteam", "bwa", ["4a196b9c72c2", "1dfc975b48d5"]),
    ]


def test_emit_falls_back_to_an_issue():
    # bwa 0.7.17 has no installable revision.
    unavailable = _emit("d413a19dec13d11e")
    assert unavailable["action"] == "issue"
    assert "bwa 0.7.17" in unavailable["body"]

    unknown = _emit("b887d74393f85b6d")
    assert unknown["action"] == "issue"
    assert unknown["tools"] == []

    # A request without a version stays an issue for the admins to judge.
    unpinned = _emit("b2486a20bc56b90f", tools=[{"name": "fastp", "requested_version": None}])
    assert unpinned["action"] == "issue"
    assert unpinned["tools"] == [
        {"name": "fastp", "owner": "iuc", "tool_shed_url": SHED, "tool_panel_section_label": LABEL}
    ]

    workflow_without_version = _emit(
        "0aab0e4c25198ad8",
        tools=[{"name": "regex_replace", "tool_shed_id": "toolshed.g2.bx.psu.edu/repos/kellrott/regex_replace"}],
    )
    assert workflow_without_version["action"] == "issue"

    missing_repository = _emit(
        "0aab0e4c25198ad8",
        tools=[{"name": "nothing", "tool_shed_id": "toolshed.g2.bx.psu.edu/repos/no-such-owner/no_such_repo"}],
    )
    assert missing_repository["action"] == "issue"
    assert missing_repository["tools"] == []

    # The test Tool Shed has iuc/fastp 1.3.6+galaxy0 too, but it is not the configured Tool Shed.
    other_tool_shed = _emit(
        "0aab0e4c25198ad8",
        tools=[
            {
                "name": "fastp",
                "tool_shed_id": "testtoolshed.g2.bx.psu.edu/repos/iuc/fastp",
                "requested_version": "1.3.6+galaxy0",
            }
        ],
    )
    assert other_tool_shed["action"] == "issue"
    assert other_tool_shed["tools"] == []


def test_emit_keeps_user_text_out_of_markdown():
    item = _emit(
        "b2486a20bc56b90f",
        tools=[
            {"name": "@octocat | <b>x</b>"},
            {"name": "x", "tool_shed_id": "toolshed.g2.bx.psu.edu/repos/@octocat/<b>x|y</b>"},
        ],
    )
    outside_code = re.sub(r"`[^`\n]*`", "", re.sub(r"```.*?```", "", item["body"], flags=re.S))
    assert "@octocat" not in outside_code
    assert "<b>" not in outside_code

    # GitHub rejects titles over 256 characters and bodies over 65536.
    long_names = _emit("b2486a20bc56b90f", tools=[{"name": "x" * 255}, {"name": "y" * 255}])
    assert len(long_names["title"]) <= 256
    long_descriptions = _emit("b2486a20bc56b90f", tools=[{"tool_url": "https://x.org", "description": "d" * 5000}] * 50)
    assert len(long_descriptions["body"]) <= 65536


def test_emit_fails_while_the_tool_shed_is_down():
    # Left unseen, the request is emitted again by the next run instead of becoming an issue.
    notification = next(n for n in NOTIFICATIONS if n["id"] == "b2486a20bc56b90f")
    with pytest.raises((BioblendError, OSError)):
        emit_item(notification, "https://toolshed.invalid/", LABEL)


def test_select_requests_skips_seen_deleted_and_other_categories():
    seen = dict(NOTIFICATIONS[0], id="a1", seen_time="2026-10-09T13:00:00")
    deleted = dict(NOTIFICATIONS[0], id="a2", deleted=True)
    message = dict(NOTIFICATIONS[0], id="a3", category="message", content={"category": "message"})
    unseen_ids = [n["id"] for n in NOTIFICATIONS]

    assert [n["id"] for n in select_requests(NOTIFICATIONS + [seen, deleted, message])] == unseen_ids
    assert [n["id"] for n in select_requests([seen], include_seen=True)] == ["a1"]


def test_fetch_notifications_reads_every_page():
    notifications = [{"id": str(i)} for i in range(250)]

    def get(url, params):
        start, end = params["offset"], params["offset"] + params["limit"]
        return SimpleNamespace(json=lambda: notifications[start:end], raise_for_status=lambda: None)

    gi = SimpleNamespace(url="http://galaxy.test/api", make_get_request=get)
    assert fetch_notifications(gi) == notifications


def test_render_yaml_adds_revisions_to_existing_entries(tmp_path):
    existing = {
        "install_tool_dependencies": True,
        "tools": [
            {
                "name": "fastp",
                "owner": "iuc",
                "tool_panel_section_label": "Quality Control",
                "revisions": ["36be11c58dac"],
            },
            # Without revisions the entry follows the latest revision.
            {"name": "regex_find_replace", "owner": "jjohnson"},
        ],
    }
    new_entry = {
        "name": "regex_replace",
        "owner": "kellrott",
        "tool_shed_url": SHED,
        "tool_panel_section_label": LABEL,
        "revisions": ["9a77d5fca67c"],
    }
    items = [
        {"tools": [{"name": "fastp", "owner": "iuc", "tool_shed_url": SHED, "revisions": ["68386e630362"]}]},
        {"tools": [new_entry]},
        {"tools": [{"name": "regex_find_replace", "owner": "jjohnson", "revisions": ["9ea374bb0350"]}]},
    ]

    tools_file = tmp_path / "tools.yaml"
    tools_file.write_text(yaml.safe_dump(existing))
    items_file = tmp_path / "items.json"
    items_file.write_text(json.dumps(items))

    def render_yaml():
        main(["render-yaml", "--items", str(items_file), "--tools-file", str(tools_file), "-o", str(tools_file)])
        return yaml.safe_load(tools_file.read_text())

    merged = render_yaml()

    assert merged == {
        "install_tool_dependencies": True,
        "tools": [
            {
                "name": "fastp",
                "owner": "iuc",
                "tool_panel_section_label": "Quality Control",
                "revisions": ["36be11c58dac", "68386e630362"],
            },
            {"name": "regex_find_replace", "owner": "jjohnson"},
            new_entry,
        ],
    }
    assert render_yaml() == merged


def test_reply_notifies_the_exact_requester():
    # Galaxy's f_email filter is a substring match.
    users = [
        {"id": "u1", "email": "old.toolrequester@galaxy.test"},
        {"id": "u2", "email": "toolrequester@galaxy.test"},
    ]
    posted = []
    gi = SimpleNamespace(
        url="http://galaxy.test/api",
        users=SimpleNamespace(get_users=lambda f_email: [u for u in users if f_email in u["email"]]),
        make_get_request=lambda url, params=None: SimpleNamespace(
            json=lambda: next(n for n in NOTIFICATIONS if url.endswith(n["id"])), raise_for_status=lambda: None
        ),
        make_post_request=lambda url, payload: posted.append((url, payload)),
    )

    reply(gi, "b2486a20bc56b90f", "https://github.com/org/tools/pull/7")

    [(url, payload)] = posted
    assert url == "http://galaxy.test/api/notifications"
    assert payload["recipients"]["user_ids"] == ["u2"]
    assert payload["notification"]["content"]["category"] == "message"
    assert "https://github.com/org/tools/pull/7" in payload["notification"]["content"]["message"]

    # A requester whose account is gone cannot be answered, which must not stop the run.
    reply(gi, "b887d74393f85b6d", "https://github.com/org/tools/issues/8")
    assert len(posted) == 1
