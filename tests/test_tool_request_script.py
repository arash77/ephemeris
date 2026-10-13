"""Run the example script that opens issues and pull requests against fake GitHub and GitLab APIs."""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from http.server import (
    BaseHTTPRequestHandler,
    ThreadingHTTPServer,
)
from urllib.parse import (
    parse_qs,
    urlsplit,
)

import pytest

SCRIPT = pathlib.Path(__file__).parent.parent / "docs" / "examples" / "tool-installation-requests.sh"
LABEL = "tool installation request"
ANSWERED = "tool installation request answered"
TOOLS = """\
tools:
- name: fastp
  owner: iuc
  revisions:
  - 68386e630362
"""

pytestmark = pytest.mark.skipif(
    not all(shutil.which(command) for command in ("bash", "curl", "git", "jq")), reason="needs bash, curl, git and jq"
)


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeForge:
    """The REST API of one GitHub repository or GitLab project, in memory."""

    def __init__(self, kind):
        self.kind = kind
        self.prefix = "/repos/org/tools" if kind == "github" else "/projects/7"
        self.items = []

    def add(self, kind, body="", labels=(LABEL,), state="open", closed_at=None, branch=None, from_fork=False, **fields):
        number = len(self.items) + 1
        path = "issues" if kind == "issue" else "pull"
        item = dict(
            fields,
            number=number,
            url=f"https://forge.test/org/tools/{path}/{number}",
            kind=kind,
            body=body,
            labels=list(labels),
            state=state,
            created_at=fields.get("created_at", "2026-10-01T00:00:00Z"),
            closed_at=closed_at,
            branch=branch,
            from_fork=from_fork,
        )
        self.items.append(item)
        return item

    def _github(self, item):
        rendered = {
            "html_url": item["url"],
            "number": item["number"],
            "body": item["body"],
            "state": "open" if item["state"] == "open" else "closed",
            "labels": [{"name": label} for label in item["labels"]],
            "created_at": item["created_at"],
            "closed_at": item["closed_at"],
        }
        if item["kind"] == "change":
            rendered["pull_request"] = {"merged_at": item["closed_at"] if item["state"] == "merged" else None}
        return rendered

    def _gitlab(self, item):
        state = {"open": "opened"}.get(item["state"], item["state"])
        rendered = {
            "web_url": item["url"],
            "iid": item["number"],
            "description": item["body"],
            "state": state,
            "labels": item["labels"],
            "created_at": item["created_at"],
            "closed_at": None if item["state"] == "merged" else item["closed_at"],
        }
        if item["kind"] == "change":
            rendered.update(
                merged_at=item["closed_at"] if item["state"] == "merged" else None,
                source_project_id=8 if item["from_fork"] else 7,
                target_project_id=7,
            )
        return rendered

    def handle(self, method, path, query, payload):
        resource = path.removeprefix(self.prefix)
        render = self._github if self.kind == "github" else self._gitlab
        if method == "POST" and resource == "/labels":
            return 201, {}
        if method == "GET" and resource in ("/issues", "/merge_requests") and "labels" in query:
            kind = "change" if resource == "/merge_requests" else "issue"
            # GitHub lists pull requests with the issues.
            found = [
                i for i in self.items if query["labels"] in i["labels"] and (self.kind == "github" or i["kind"] == kind)
            ]
            per_page = int(query["per_page"])
            start = (int(query["page"]) - 1) * per_page
            return 200, [render(i) for i in found[start:][:per_page]]
        if method == "POST" and resource == "/issues":
            body = payload.get("body", payload.get("description"))
            labels = payload["labels"] if self.kind == "github" else payload["labels"].split(",")
            return 201, render(self.add("issue", body, labels, title=payload["title"]))
        if method == "GET" and resource in ("/pulls", "/merge_requests"):
            if self.kind == "github":
                # GitHub matches owner:branch, so pull requests from forks have another owner.
                owner, _, branch = query["head"].partition(":")
                found = [i for i in self.items if i["branch"] == branch and owner == "org" and not i["from_fork"]]
                return 200, [render(i) for i in found]
            return 200, [render(i) for i in self.items if i["branch"] == query["source_branch"]]
        if method == "POST" and resource == "/pulls":
            change = self.add("change", payload["body"], (), branch=payload["head"], base=payload["base"])
            return 201, dict(render(change), title=payload["title"])
        if method == "POST" and resource == "/merge_requests":
            change = self.add(
                "change",
                payload["description"],
                payload["labels"].split(","),
                branch=payload["source_branch"],
                base=payload["target_branch"],
            )
            return 201, render(change)
        if method in ("POST", "PUT") and resource.split("/")[1] in ("issues", "merge_requests"):
            item = self.items[int(resource.split("/")[2]) - 1]
            item["labels"] += payload["labels"] if self.kind == "github" else payload["add_labels"].split(",")
            return 200, render(item)
        return 404, {"message": f"no fake for {method} {path}"}


@pytest.fixture
def forge_server():
    servers = []

    def start(kind):
        forge = FakeForge(kind)

        class Handler(BaseHTTPRequestHandler):
            def _respond(self):
                token = self.headers.get("Authorization") if kind == "github" else self.headers.get("PRIVATE-TOKEN")
                url = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length)) if length else None
                if token not in ("Bearer secret", "secret"):
                    status, body = 401, {"message": "unauthorized"}
                else:
                    status, body = forge.handle(self.command, url.path, query, payload)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = _respond

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return forge, f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()


def _body(notification_id):
    return f"Tool installation request from Galaxy (notification `{notification_id}`).\n\nDetails."


def _item(notification_id, action, title, tools=()):
    return {
        "notification_id": notification_id,
        "action": action,
        "title": title,
        "body": _body(notification_id),
        "tools": list(tools),
    }


def _ci(kind, api, tmp_path):
    """What GitHub Actions or GitLab CI tell a job, from which the script finds its settings."""
    if kind == "github":
        return {"GITHUB_ACTIONS": "true", "GITHUB_API_URL": api, "GITHUB_REPOSITORY": "org/tools"}
    return {
        "GITLAB_CI": "true",
        "CI_API_V4_URL": api,
        "CI_PROJECT_ID": "7",
        "CI_SERVER_URL": f"file://{tmp_path}",
        "CI_PROJECT_PATH": "origin",
        "CI_COMMIT_BRANCH": "main",
    }


def _run(tmp_path, requests, env, gitlab_ci=False, installed=()):
    """Run the script in a clone of a tool list repository, with ephemeris answering for Galaxy."""
    subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main", tmp_path / "origin.git"], check=True)
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "--quiet", tmp_path / "origin.git", checkout], check=True)
    (checkout / "tools.yaml").write_text(TOOLS)
    git_env = dict(
        os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"
    )
    for command in (
        ["symbolic-ref", "HEAD", "refs/heads/main"],
        ["add", "."],
        ["commit", "--quiet", "-m", "Add tools"],
    ):
        subprocess.run(["git", *command], cwd=checkout, env=git_env, check=True)
    subprocess.run(["git", "push", "--quiet", "origin", "main"], cwd=checkout, check=True)
    if gitlab_ci:
        # GitLab CI checks out a commit, from a URL with a job token that cannot push.
        subprocess.run(["git", "switch", "--quiet", "--detach"], cwd=checkout, check=True)
        bad_origin = "https://gitlab-ci-token:x@gitlab.invalid/org/tools.git"
        subprocess.run(["git", "remote", "set-url", "origin", bad_origin], cwd=checkout, check=True)

    (tmp_path / "requests.json").write_text(json.dumps(requests))
    (tmp_path / "installed").write_text("".join(f"{i}\n" for i in installed))
    log = tmp_path / "galaxy.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "consume-tool-installation-requests"
    stub.write_text(f"""#!/bin/bash
printf '%s|' "$@" >> {log}; echo >> {log}
case "$1" in
  emit)
    while [ $# -gt 0 ]; do
      [ "$1" = --handled ] && sed 's/$/|/; s/^/handled|/' "$2" >> {log}
      shift
    done
    cat {tmp_path / "requests.json"} ;;
  installed) grep -qx "$3" {tmp_path / "installed"} && echo yes || echo no ;;
  render-yaml) exec {sys.executable} -m ephemeris.tool_request_consumer "$@" ;;
esac
""")
    stub.chmod(0o755)
    # Only what the script needs, so that variables of the CI running this test do not leak in.
    script_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "PYTHONPATH": os.pathsep.join(sys.path),
        "FORGE_TOKEN": "secret",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        **env,
    }
    start = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout, capture_output=True, text=True)
    result = subprocess.run(["bash", SCRIPT], cwd=checkout, env=script_env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    # Cron runs the script again in the same checkout, so it ends where it started.
    end = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout, capture_output=True, text=True)
    assert end.stdout == start.stdout
    return [line.split("|")[:-1] for line in log.read_text().splitlines()]


def _replies(calls):
    """Map notification ids to the URL and state that reply told."""
    replies = {}
    for args in calls:
        if args[0] == "reply":
            options = dict(zip(args[1::2], args[2::2]))
            replies[options["--notification-id"]] = (options.get("--url"), options.get("--state", "handling"))
    return replies


@pytest.mark.parametrize("kind", ["github", "gitlab"])
def test_script_opens_and_reuses_issues_and_pull_requests(tmp_path, forge_server, kind):
    forge, api = forge_server(kind)
    # The issue a run opened before it stopped, on the second page of the label's issues.
    for i in range(100):
        forge.add("issue", f"Older request {i}", created_at="2026-09-01T00:00:00Z")
    # Issues without the label are not the script's.
    forge.add("issue", _body("n5"), labels=["question"])
    reused_issue = forge.add("issue", _body("n2"))["url"]
    # Opened before pull requests got the label.
    reused_change = forge.add("change", _body("n4"), labels=(), branch="tool-request/n4")["url"]
    forge.add("change", _body("n5"), labels=(), branch="tool-request/n5", from_fork=True)
    bwa = {
        "name": "bwa",
        "owner": "devteam",
        "tool_shed_url": "https://toolshed.g2.bx.psu.edu/",
        "revisions": ["4a196b9c72c2"],
    }
    fastp = {"name": "fastp", "owner": "iuc", "revisions": ["68386e630362"]}
    requests = [
        _item("n1", "pr", "Install bwa 0.7.18", [bwa]),
        _item("n2", "issue", "Install something"),
        # The tool list has this revision already.
        _item("n3", "pr", "Install fastp 1.3.6+galaxy0", [fastp]),
        # Turned from a pull request into an issue since an earlier run.
        _item("n4", "issue", "Install fastp"),
        _item("n5", "issue", "Install other"),
    ]

    existing = len(forge.items)
    env = dict(_ci(kind, api, tmp_path), TOOLS_FILE="tools.yaml")
    calls = _run(tmp_path, requests, env, gitlab_ci=kind == "gitlab")

    # Requests an admin read in Galaxy are handled too, unless they have an issue or pull request.
    [emit] = [args for args in calls if args[0] == "emit"]
    assert emit[emit.index("--seen-since") + 1] == "2026-09-01T00:00:00Z"
    assert [args[1] for args in calls if args[0] == "handled"] == ["n2"]
    new = forge.items[existing:]
    [new_change] = [i for i in new if i["branch"] == "tool-request/n1"]
    assert (new_change["base"], new_change["labels"]) == ("main", [LABEL])
    pushed = subprocess.run(
        ["git", "show", "tool-request/n1:tools.yaml"], cwd=tmp_path / "origin.git", capture_output=True, text=True
    ).stdout
    assert "name: bwa" in pushed and "4a196b9c72c2" in pushed
    replies = _replies(calls)
    assert replies["n1"] == (new_change["url"], "handling")
    assert replies["n2"] == (reused_issue, "handling")
    assert replies["n4"] == (reused_change, "handling")
    new_issues = {i["url"]: i for i in new if i["kind"] == "issue"}
    assert set(new_issues) == {replies["n3"][0], replies["n5"][0]}
    assert all(i["labels"] == [LABEL] for i in new_issues.values())
    assert [args[1] for args in calls if args[0] == "mark-seen"] == ["n1", "n2", "n3", "n4", "n5"]


@pytest.mark.parametrize("kind", ["github", "gitlab"])
def test_script_tells_requesters_how_their_request_ended(tmp_path, forge_server, kind):
    forge, api = forge_server(kind)
    rejected = forge.add("issue", _body("c1"), state="closed", closed_at=_ago(1))
    merged = forge.add("change", _body("c2"), state="merged", closed_at=_ago(1), branch="tool-request/c2")
    # Merged, but nothing installed it yet.
    forge.add("change", _body("c3"), state="merged", closed_at=_ago(1), branch="tool-request/c3")
    forge.add("issue", _body("c4"), labels=[LABEL, ANSWERED], state="closed", closed_at=_ago(1))
    forge.add("issue", _body("c5"), state="closed", closed_at=_ago(40))
    forge.add("issue", _body("c6"))

    env = dict(_ci(kind, api, tmp_path), TOOLS_FILE="tools.yaml")
    calls = _run(tmp_path, [], env, gitlab_ci=kind == "gitlab", installed=["c2"])

    assert [args[2] for args in calls if args[0] == "installed"] == ["c1", "c2", "c3"]
    assert _replies(calls) == {"c1": (rejected["url"], "closed"), "c2": (merged["url"], "installed")}
    answered = [i["body"].split("`")[1] for i in forge.items if ANSWERED in i["labels"]]
    assert sorted(answered) == ["c1", "c2", "c4"]


def test_script_opens_only_issues_without_a_tool_list(tmp_path, forge_server):
    forge, api = forge_server("github")
    bwa = {"name": "bwa", "owner": "devteam", "revisions": ["4a196b9c72c2"]}

    env = {"FORGE": "github", "FORGE_API": api, "FORGE_PROJECT": "org/tools", "EMIT_OPTIONS": "-l 'Other Tools'"}
    calls = _run(tmp_path, [_item("n1", "pr", "Install bwa 0.7.18", [bwa])], env)

    [emit] = [args for args in calls if args[0] == "emit"]
    assert emit[-2:] == ["-l", "Other Tools"]
    # No issue or pull request yet, so only unseen requests count.
    assert "--seen-since" not in emit
    assert [i["kind"] for i in forge.items] == ["issue"]
    assert _replies(calls)["n1"] == (forge.items[0]["url"], "handling")
