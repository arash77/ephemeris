import json
import pathlib
import re
from datetime import (
    datetime,
    timezone,
)
from types import SimpleNamespace
from urllib.parse import (
    parse_qs,
    urlsplit,
)

import pytest
import yaml
from bioblend import ConnectionError as BioblendError

from ephemeris import tool_request_consumer
from ephemeris.tool_request_consumer import (
    emit,
    emit_item,
    fetch_notifications,
    installed,
    main,
    MAX_BODY_LENGTH,
    merge_tool_list,
    reply,
    ReplyFailed,
    select_requests,
)

NOTIFICATIONS = json.loads((pathlib.Path(__file__).parent / "tool_installation_requests.json").read_text())
SHED = "https://toolshed.g2.bx.psu.edu/"
FAKE_SHED = "https://fake-toolshed.test/"
LABEL = "Tools from user request"


def _request(notification_id, tools=None, **fields):
    notification = next(n for n in NOTIFICATIONS if n["id"] == notification_id)
    if tools is not None:
        notification = dict(notification, content=dict(notification["content"], tools=tools))
    return dict(notification, **fields)


def _emit(notification_id, tools=None, shed=SHED):
    return emit_item(_request(notification_id, tools), shed, LABEL)


def _galaxy_get(url, params=None):
    """Answer like Galaxy's notifications API, which no longer has requests it expired."""
    notification = next((n for n in NOTIFICATIONS if url.endswith(n["id"])), None)
    return SimpleNamespace(
        status_code=200 if notification else 404, json=lambda: notification, raise_for_status=lambda: None
    )


def _outside_code(markdown):
    without_blocks = re.sub(r"^(`{3,})\n.*?\n\1$", "", markdown, flags=re.S | re.M)
    return re.sub(r"`[^`\n]*`", "", without_blocks)


class FakeToolShed:
    """A Tool Shed with only iuc/fastp and devteam/samtools_sort, failing every lookup with ``error`` if given."""

    OWNERS = {"fastp": "iuc", "samtools_sort": "devteam"}

    def __init__(self, error=None):
        self.url = FAKE_SHED + "api"
        self.error = error
        self.requests = []
        self.repositories = SimpleNamespace(get_repositories=self.get_repositories)

    def get_repositories(self, name=None, owner=None):
        self.requests.append((name, owner))
        if self.error:
            raise self.error
        if name not in self.OWNERS or owner not in (None, self.OWNERS[name]):
            return []
        return [{"id": "f1" if name == "fastp" else "s1", "owner": self.OWNERS[name], "name": name}]

    def make_get_request(self, url, params=None):
        assert url == f"{self.url}/repositories/f1/metadata"
        metadata = {
            "-1:aaaaaaaaaaaa": {"numeric_revision": None, "changeset_revision": "aaaaaaaaaaaa", "tools": []},
            "0:36be11c58dac": {"numeric_revision": 0, "changeset_revision": "36be11c58dac", "tools": []},
            "1:68386e630362": {
                "numeric_revision": 1,
                "changeset_revision": "68386e630362",
                "tools": [{"id": "fastp", "version": "1.3.6+galaxy0"}],
            },
        }
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: metadata)


@pytest.fixture
def fake_tool_shed(monkeypatch):
    def clear_caches():
        tool_request_consumer._repositories.cache_clear()
        tool_request_consumer._installable_revisions.cache_clear()

    def install(**kwargs):
        clear_caches()
        shed = FakeToolShed(**kwargs)
        monkeypatch.setattr(tool_request_consumer, "_shed", lambda url: shed)
        return shed

    yield install
    clear_caches()


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
    assert "| `bwa 0.7.17` | not resolved: `no installable revision has version 0.7.17` | |" in unavailable["body"]

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


def test_emit_keeps_user_text_out_of_markdown(fake_tool_shed):
    fake_tool_shed()
    item = _emit(
        "b2486a20bc56b90f",
        tools=[
            {"name": "@octocat | <b>x</b>"},
            {"name": "x", "tool_shed_id": "fake-toolshed.test/repos/@octocat/<b>x|y</b>"},
        ],
        shed=FAKE_SHED,
    )
    assert "@octocat" not in _outside_code(item["body"])
    assert "<b>" not in _outside_code(item["body"])

    # GitHub rejects titles over 256 characters and bodies over 65536.
    long_names = _emit("b2486a20bc56b90f", tools=[{"name": "x" * 255}, {"name": "y" * 255}], shed=FAKE_SHED)
    assert len(long_names["title"]) <= 256


def test_emit_fits_long_requests_into_the_body_limit(fake_tool_shed):
    fake_tool_shed()
    # The largest request Galaxy accepts: 50 tools with long URLs and descriptions.
    unnamed = {"tool_url": "https://x.org/ @victim " + "u" * 2000, "description": "d @victim " * 500}
    pinned = {"name": "fastp", "requested_version": "1.3.6+galaxy0", "description": "d" * 5000}
    item = _emit("b2486a20bc56b90f", tools=[unnamed] * 30 + [pinned] * 20, shed=FAKE_SHED)

    assert len(item["body"]) <= MAX_BODY_LENGTH
    assert "(Truncated, the full request is in Galaxy.)" in item["body"]
    assert "@victim" not in _outside_code(item["body"])
    # The tool list entries come last, whole.
    entries = item["body"].rsplit("Tool list entries:\n```\n", 1)[1]
    assert entries.endswith("\n```")
    assert yaml.safe_load(entries[: -len("\n```")])["tools"][0]["revisions"] == ["68386e630362"]


def test_emit_looks_tool_names_up_exactly(fake_tool_shed):
    shed = fake_tool_shed()
    item = _emit(
        "b2486a20bc56b90f",
        tools=[
            {"name": " FastP ", "requested_version": "1.3.6+galaxy0"},
            {"name": "fastp", "requested_version": "1.3.6+galaxy0"},
            # Repository names only hold lower case letters, digits and underscores.
            {"name": "bowtie2: map"},
        ],
        shed=FAKE_SHED,
    )
    assert item["tools"] == [
        {
            "name": "fastp",
            "owner": "iuc",
            "tool_shed_url": FAKE_SHED,
            "tool_panel_section_label": LABEL,
            "revisions": ["68386e630362"],
        }
    ]
    assert shed.requests == [("fastp", None), ("bowtie2_map", None)]


def test_emit_links_tools_no_repository_is_named_like(fake_tool_shed):
    shed = fake_tool_shed()
    template = "https://github.com/org/tools-dev/issues/new?title={title}&body={body}&labels=tool%20request"
    tools = [
        # The Tool Shed spells this samtools_sort.
        {"name": "Samtools sort"},
        {"name": "NewTool @victim", "tool_url": "https://example.org/newtool", "description": "Does (new) things."},
    ]
    item = emit_item(_request("b2486a20bc56b90f", tools), FAKE_SHED, LABEL, wrapper_request_url=template)

    assert ("samtools_sort", None) in shed.requests
    [(name, link)] = re.findall(r"\[Request a wrapper for `([^`]*)`\]\((\S+)\)", item["body"])
    assert name == "NewTool @victim"
    query = parse_qs(urlsplit(link).query)
    assert query["title"] == ["Tool request: NewTool @victim"]
    assert "Does (new) things." in query["body"][0]
    assert "Homepage: https://example.org/newtool" in query["body"][0]
    assert query["labels"] == ["tool request"]
    # The link names the tool as code and encodes what users typed.
    assert "@victim" not in _outside_code(item["body"])

    # Tools the Tool Shed has get no link.
    listed = emit_item(
        _request("b2486a20bc56b90f", [{"name": "fastp"}]), FAKE_SHED, LABEL, wrapper_request_url=template
    )
    assert "Request a wrapper" not in listed["body"]


def test_emit_pins_tools_without_a_version_to_the_newest_revision(fake_tool_shed):
    fake_tool_shed()
    by_name = emit_item(_request("b2486a20bc56b90f", [{"name": "fastp"}]), FAKE_SHED, LABEL, pin_latest=True)
    assert by_name["action"] == "pr"
    assert by_name["tools"][0]["revisions"] == ["68386e630362"]
    # The tools of a repository can have different versions, so only a named tool's version goes in the title.
    assert by_name["title"] == "Install fastp"

    workflow_tool = {"name": "fastp", "tool_shed_id": "fake-toolshed.test/repos/iuc/fastp"}
    by_id = emit_item(_request("0aab0e4c25198ad8", [workflow_tool]), FAKE_SHED, LABEL, pin_latest=True)
    assert by_id["title"] == "Install fastp 1.3.6+galaxy0"


def test_emit_gives_unpinned_tools_their_own_entry(fake_tool_shed):
    fake_tool_shed()
    item = _emit(
        "b2486a20bc56b90f",
        tools=[{"name": "fastp", "requested_version": "1.3.6+galaxy0"}, {"name": "fastp"}],
        shed=FAKE_SHED,
    )
    assert item["action"] == "issue"
    entry = {"name": "fastp", "owner": "iuc", "tool_shed_url": FAKE_SHED, "tool_panel_section_label": LABEL}
    assert item["tools"] == [dict(entry, revisions=["68386e630362"]), entry]


def test_emit_turns_tool_shed_rejections_into_issues(fake_tool_shed):
    fake_tool_shed(error=BioblendError("GET: error 403", status_code=403))
    rejected = _emit("b2486a20bc56b90f", shed=FAKE_SHED)
    assert rejected["action"] == "issue"
    assert "the Tool Shed rejected the lookup with HTTP 403" in rejected["body"]

    # Left unseen, a request that hit a Tool Shed error is emitted again by the next run.
    fake_tool_shed(error=BioblendError("GET: error 502", status_code=502))
    with pytest.raises(BioblendError):
        _emit("b2486a20bc56b90f", shed=FAKE_SHED)


def test_emit_waits_a_day_for_the_tool_shed(fake_tool_shed):
    now = datetime.now(timezone.utc).isoformat()
    fresh = _request("b2486a20bc56b90f", id="fresh", publication_time=now)
    also_fresh = _request("b2486a20bc56b90f", id="also_fresh", publication_time=now)
    old = _request("b2486a20bc56b90f", id="old", publication_time="2026-01-01T00:00:00")

    shed = fake_tool_shed(error=BioblendError("GET: error 502", status_code=502))
    [item] = emit([fresh, old], FAKE_SHED, LABEL)
    assert item["notification_id"] == "old"
    assert item["action"] == "issue"
    assert "not looked up, the Tool Shed failed: GET: error 502" in item["body"]
    assert len(shed.requests) == 2

    # A Tool Shed that does not answer at all is not asked again in the same run.
    shed = fake_tool_shed(error=BioblendError("Connection refused"))
    assert [i["notification_id"] for i in emit([fresh, also_fresh, old], FAKE_SHED, LABEL)] == ["old"]
    assert len(shed.requests) == 1


def test_emit_fails_while_the_tool_shed_is_down():
    notification = next(n for n in NOTIFICATIONS if n["id"] == "b2486a20bc56b90f")
    with pytest.raises((BioblendError, OSError)):
        emit_item(notification, "https://toolshed.invalid/", LABEL)
    assert tool_request_consumer._shed("https://toolshed.invalid/").timeout == tool_request_consumer.TOOL_SHED_TIMEOUT


def test_emit_titles_pull_requests_with_tool_shed_data_only(fake_tool_shed):
    fake_tool_shed()
    # Only text after the last "://" used to be compared with the configured Tool Shed.
    disguised = _emit(
        "0aab0e4c25198ad8",
        tools=[
            {
                "name": None,
                "tool_shed_id": "Fixes #1 @victim x://fake-toolshed.test/repos/iuc/fastp",
                "requested_version": "1.3.6+galaxy0",
            }
        ],
        shed=FAKE_SHED,
    )
    assert disguised["action"] == "issue"
    assert disguised["tools"] == []

    by_id = _emit(
        "0aab0e4c25198ad8",
        tools=[
            {
                "name": None,
                "tool_shed_id": "http://Fake-ToolShed.test/repos/iuc/fastp",
                "requested_version": "1.3.6+galaxy0",
            }
        ],
        shed=FAKE_SHED,
    )
    assert by_id["action"] == "pr"
    assert by_id["title"] == "Install fastp 1.3.6+galaxy0"

    by_name = _emit("b2486a20bc56b90f", tools=[{"name": "FASTP", "requested_version": "1.3.6+galaxy0"}], shed=FAKE_SHED)
    assert by_name["action"] == "pr"
    assert by_name["title"] == "Install fastp 1.3.6+galaxy0"

    # Issue titles keep what users typed, on one line.
    multiline = _emit("b2486a20bc56b90f", tools=[{"name": "fast\np\n\nFixes #1"}], shed=FAKE_SHED)
    assert multiline["action"] == "issue"
    assert multiline["title"] == "Install fast p Fixes #1"


def test_select_requests_skips_seen_deleted_and_other_categories():
    seen = dict(NOTIFICATIONS[0], id="a1", seen_time="2026-10-09T13:00:00")
    deleted = dict(NOTIFICATIONS[0], id="a2", deleted=True)
    message = dict(NOTIFICATIONS[0], id="a3", category="message", content={"category": "message"})
    unseen_ids = [n["id"] for n in NOTIFICATIONS]

    assert [n["id"] for n in select_requests(NOTIFICATIONS + [seen, deleted, message])] == unseen_ids
    assert [n["id"] for n in select_requests([seen], include_seen=True)] == ["a1"]


def test_select_requests_picks_up_requests_an_admin_read():
    read = dict(NOTIFICATIONS[0], id="r1", seen_time="2026-10-09T13:00:00", publication_time="2026-10-09T12:00:00")
    handled = dict(read, id="r2")
    # Sent before the first issue or pull request, so presumably handled by hand.
    older = dict(read, id="r3", publication_time="2026-09-01T00:00:00")
    # Handled, but its requester may not have been told yet.
    unseen = dict(NOTIFICATIONS[0], id="r4")
    since = datetime(2026, 10, 1, tzinfo=timezone.utc)

    picked = select_requests([read, handled, older, unseen], handled={"r2", "r4"}, seen_since=since)
    assert [n["id"] for n in picked] == ["r1", "r4"]


def test_fetch_notifications_reads_all_at_once():
    # Galaxy does not order notifications, so pages could skip some.
    notifications = [{"id": str(i)} for i in range(250)]

    def get(url, params):
        assert params == {"limit": 0}
        return SimpleNamespace(json=lambda: notifications, raise_for_status=lambda: None)

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
        {
            "action": "pr",
            "tools": [{"name": "fastp", "owner": "iuc", "tool_shed_url": SHED, "revisions": ["68386e630362"]}],
        },
        {"action": "pr", "tools": [new_entry]},
        {
            "action": "pr",
            "tools": [{"name": "regex_find_replace", "owner": "jjohnson", "revisions": ["9ea374bb0350"]}],
        },
        # Issue items wait for the admins.
        {"action": "issue", "tools": [{"name": "bwa", "owner": "devteam", "revisions": ["4a196b9c72c2"]}]},
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


def test_render_yaml_reads_revisions_like_shed_tools():
    existing = {
        "tools": [
            # shed-tools installs the latest revision for these.
            {"name": "null_revisions", "owner": "o", "revisions": None},
            {"name": "no_revisions", "owner": "o", "revisions": []},
            {"name": "changeset_pin", "owner": "o", "changeset_revision": "36be11c58dac"},
            # An unquoted revision made of digits loads as a number.
            {"name": "numeric", "owner": "o", "revisions": [123456789012]},
        ]
    }
    items = [
        {
            "action": "pr",
            "tools": [
                {"name": "null_revisions", "owner": "o", "revisions": ["68386e630362"]},
                {"name": "no_revisions", "owner": "o", "revisions": ["68386e630362"]},
                {"name": "changeset_pin", "owner": "o", "revisions": ["68386e630362"]},
                {"name": "numeric", "owner": "o", "revisions": ["123456789012"]},
            ],
        }
    ]

    assert merge_tool_list(existing, items)["tools"] == [
        {"name": "null_revisions", "owner": "o", "revisions": None},
        {"name": "no_revisions", "owner": "o", "revisions": []},
        {"name": "changeset_pin", "owner": "o", "revisions": ["36be11c58dac", "68386e630362"]},
        {"name": "numeric", "owner": "o", "revisions": [123456789012]},
    ]


def test_render_yaml_leaves_a_tool_list_that_has_the_revisions_alone(tmp_path):
    # Listed twice, and once with an http:// Tool Shed URL.
    tools = """\
# Requested tools
tools:
  - name: fastp
    owner: iuc
    tool_shed_url: http://toolshed.g2.bx.psu.edu
    revisions: ['68386e630362']
  - name: fastp
    owner: iuc
    revisions: ['36be11c58dac']
"""
    tools_file = tmp_path / "tools.yaml"
    tools_file.write_text(tools)
    items_file = tmp_path / "items.json"
    items_file.write_text(
        json.dumps(
            [
                {
                    "action": "pr",
                    "tools": [{"name": "fastp", "owner": "iuc", "tool_shed_url": SHED, "revisions": ["68386e630362"]}],
                }
            ]
        )
    )

    main(["render-yaml", "--items", str(items_file), "--tools-file", str(tools_file), "-o", str(tools_file)])
    assert tools_file.read_text() == tools


def test_reply_notifies_the_exact_requester():
    # Galaxy's f_email filter is a substring match.
    users = [
        {"id": "u1", "email": "old.toolrequester@galaxy.test"},
        {"id": "u2", "email": "toolrequester@galaxy.test"},
    ]
    posted = []
    sent = SimpleNamespace(count=1)

    def post(url, payload):
        posted.append((url, payload))
        return {"total_notifications_sent": sent.count, "notification": {}}

    gi = SimpleNamespace(
        url="http://galaxy.test/api",
        users=SimpleNamespace(get_users=lambda f_email: [u for u in users if f_email in u["email"]]),
        make_get_request=_galaxy_get,
        make_post_request=post,
    )

    assert reply(gi, "b2486a20bc56b90f", "https://github.com/org/tools/pull/7")

    [(url, payload)] = posted
    assert url == "http://galaxy.test/api/notifications"
    assert payload["recipients"]["user_ids"] == ["u2"]
    assert payload["notification"]["content"]["category"] == "message"
    # Galaxy stores up to 32 characters, and fails to send a notification with a longer source.
    assert len(payload["notification"]["source"]) <= 32
    assert "https://github.com/org/tools/pull/7" in payload["notification"]["content"]["message"]

    # A requester whose account is gone cannot be answered, which must not stop the run.
    assert not reply(gi, "b887d74393f85b6d", "https://github.com/org/tools/issues/8")
    assert len(posted) == 1

    # Galaxy sends nothing to users who turned message notifications off.
    sent.count = 0
    assert not reply(gi, "b2486a20bc56b90f", "https://github.com/org/tools/pull/7")


def test_reply_tells_how_the_request_ended():
    posted = []

    def post(url, payload):
        posted.append(payload["notification"]["content"])
        return {"total_notifications_sent": 1, "notification": {}}

    gi = SimpleNamespace(
        url="http://galaxy.test/api",
        users=SimpleNamespace(get_users=lambda f_email: [{"id": "u2", "email": f_email}]),
        make_get_request=_galaxy_get,
        make_post_request=post,
    )

    # Admins without a tool list repository have no link to give.
    assert reply(gi, "b2486a20bc56b90f", state="installed")
    assert posted[-1]["subject"] == "Your requested tools are installed"
    assert "http" not in posted[-1]["message"]
    assert reply(gi, "b2486a20bc56b90f", "https://github.com/org/tools/issues/8", state="closed")
    assert posted[-1]["message"].endswith(": https://github.com/org/tools/issues/8")
    # Galaxy deletes requests some months after they were sent.
    assert not reply(gi, "0000000000000000", state="closed")
    assert len(posted) == 2


def test_installed_checks_galaxys_repositories(fake_tool_shed):
    fake_tool_shed()

    def galaxy(**changes):
        repository = {
            "status": "Installed",
            "deleted": False,
            "tool_shed": "fake-toolshed.test",
            "owner": "iuc",
            "name": "fastp",
            "changeset_revision": "68386e630362",
            "installed_changeset_revision": "68386e630362",
        }
        return SimpleNamespace(toolshed=SimpleNamespace(get_repositories=lambda: [dict(repository, **changes)]))

    pinned = _request("b2486a20bc56b90f", [{"name": "fastp", "requested_version": "1.3.6+galaxy0"}])
    unpinned = _request("b2486a20bc56b90f", [{"name": "fastp"}])
    older = {"changeset_revision": "36be11c58dac", "installed_changeset_revision": "36be11c58dac"}

    assert installed(galaxy(), pinned, FAKE_SHED)
    # Updated since, the installed revision still counts.
    assert installed(galaxy(changeset_revision="4990e6f1348f"), pinned, FAKE_SHED)
    assert not installed(galaxy(**older), pinned, FAKE_SHED)
    assert installed(galaxy(**older), unpinned, FAKE_SHED)
    assert not installed(galaxy(status="Error"), pinned, FAKE_SHED)
    assert not installed(galaxy(tool_shed="testtoolshed.g2.bx.psu.edu"), pinned, FAKE_SHED)
    # A tool the Tool Shed does not have cannot be checked.
    assert not installed(galaxy(), _request("b2486a20bc56b90f", [{"name": "Foldseek"}]), FAKE_SHED)


def test_reply_waits_for_galaxy_to_send_it_in_a_task():
    # With Celery, Galaxy answers with a task, and sends the notification in it.
    states = {"t1": ["PENDING", "SUCCESS"], "t2": ["STARTED", "FAILURE"]}
    task = SimpleNamespace(id=None)

    def get(url, params=None):
        if "/tasks/" in url:
            state = states[url.split("/")[-2]].pop(0)
            return SimpleNamespace(json=lambda: state, raise_for_status=lambda: None)
        return _galaxy_get(url)

    gi = SimpleNamespace(
        url="http://galaxy.test/api",
        users=SimpleNamespace(get_users=lambda f_email: [{"id": "u2", "email": f_email}]),
        make_get_request=get,
        make_post_request=lambda url, payload: {"id": task.id, "ignored": False, "name": None, "queue": None},
    )

    task.id = "t1"
    assert reply(gi, "b2486a20bc56b90f", "https://github.com/org/tools/pull/7")
    # The request then stays unseen, so that the next run answers it.
    task.id = "t2"
    with pytest.raises(ReplyFailed):
        reply(gi, "b2486a20bc56b90f", "https://github.com/org/tools/pull/7")
