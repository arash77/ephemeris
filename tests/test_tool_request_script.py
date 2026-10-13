"""Run the example script that opens issues and pull requests against fake GitHub and GitLab APIs."""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
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


class FakeForge:
    """The REST API of one GitHub repository or GitLab project, in memory."""

    def __init__(self, kind):
        self.kind = kind
        self.prefix = "/repos/org/tools" if kind == "github" else "/projects/7"
        self.issues = []
        self.changes = []

    def add_issue(self, body, labels=(LABEL,)):
        url = f"https://forge.test/org/tools/issues/{len(self.issues) + 1}"
        self.issues.append({"url": url, "body": body, "labels": list(labels)})
        return url

    def add_change(self, branch, from_fork=False, **fields):
        url = f"https://forge.test/org/tools/pull/{len(self.changes) + 1}"
        self.changes.append(dict(fields, url=url, branch=branch, from_fork=from_fork))
        return url

    def handle(self, method, path, query, payload):
        resource = path.removeprefix(self.prefix)
        if method == "POST" and resource == "/labels":
            return 201, {}
        if method == "GET" and resource == "/issues":
            per_page = int(query["per_page"])
            start = (int(query["page"]) - 1) * per_page
            labeled = [i for i in self.issues if query["labels"] in i["labels"]]
            issues = labeled[start:][:per_page]
            if self.kind == "github":
                return 200, [{"html_url": i["url"], "body": i["body"]} for i in issues]
            return 200, [{"web_url": i["url"], "description": i["body"]} for i in issues]
        if method == "POST" and resource == "/issues":
            if self.kind == "github":
                return 201, {"html_url": self.add_issue(payload["body"], payload["labels"])}
            return 201, {"web_url": self.add_issue(payload["description"], payload["labels"].split(","))}
        if method == "GET" and resource in ("/pulls", "/merge_requests"):
            if self.kind == "github":
                # GitHub matches owner:branch, so pull requests from forks have another owner.
                owner, _, branch = query["head"].partition(":")
                found = [c for c in self.changes if c["branch"] == branch and owner == "org" and not c["from_fork"]]
                return 200, [{"html_url": c["url"]} for c in found]
            found = [c for c in self.changes if c["branch"] == query["source_branch"]]
            return 200, [
                {"web_url": c["url"], "source_project_id": 8 if c["from_fork"] else 7, "target_project_id": 7}
                for c in found
            ]
        if method == "POST" and resource == "/pulls":
            return 201, {"html_url": self.add_change(payload["head"], base=payload["base"], title=payload["title"])}
        if method == "POST" and resource == "/merge_requests":
            url = self.add_change(payload["source_branch"], base=payload["target_branch"], title=payload["title"])
            return 201, {"web_url": url}
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

            do_GET = do_POST = _respond

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return forge, f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()


def _item(notification_id, action, title, tools=()):
    body = f"Tool installation request from Galaxy (notification `{notification_id}`).\n\nDetails."
    return {"notification_id": notification_id, "action": action, "title": title, "body": body, "tools": list(tools)}


def _run(tmp_path, requests, env, gitlab_ci=False):
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
        subprocess.run(
            ["git", "remote", "set-url", "origin", "https://gitlab-ci-token:x@gitlab.invalid/org/tools.git"],
            cwd=checkout,
            check=True,
        )

    (tmp_path / "requests.json").write_text(json.dumps(requests))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "consume-tool-installation-requests"
    stub.write_text(f"""#!/bin/bash
echo "$*" >> {tmp_path / "galaxy.log"}
case "$1" in
  emit) cat {tmp_path / "requests.json"} ;;
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
    result = subprocess.run(["bash", SCRIPT], cwd=checkout, env=script_env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    replies = {}
    for line in (tmp_path / "galaxy.log").read_text().splitlines():
        command, *args = line.split()
        if command == "reply":
            replies[args[1]] = args[3]
    seen = [line.split()[1] for line in (tmp_path / "galaxy.log").read_text().splitlines() if line.startswith("mark")]
    return replies, seen


@pytest.mark.parametrize("kind", ["github", "gitlab"])
def test_script_opens_and_reuses_issues_and_pull_requests(tmp_path, forge_server, kind):
    forge, api = forge_server(kind)
    # The issue a run opened before it stopped, on the second page of the label's issues.
    for i in range(100):
        forge.add_issue(f"Older request {i}")
    # Issues without the label are not the script's.
    forge.add_issue(_item("n5", "issue", "")["body"], labels=["question"])
    reused_issue = forge.add_issue(_item("n2", "issue", "")["body"])
    reused_change = forge.add_change("tool-request/n4")
    forge.add_change("tool-request/n5", from_fork=True)
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

    # The script finds out the forge, its API and the project in GitHub Actions and GitLab CI.
    if kind == "github":
        ci = {"GITHUB_ACTIONS": "true", "GITHUB_API_URL": api, "GITHUB_REPOSITORY": "org/tools"}
    else:
        ci = {
            "GITLAB_CI": "true",
            "CI_API_V4_URL": api,
            "CI_PROJECT_ID": "7",
            "CI_SERVER_URL": f"file://{tmp_path}",
            "CI_PROJECT_PATH": "origin",
            "CI_COMMIT_BRANCH": "main",
        }

    existing = len(forge.issues)
    replies, seen = _run(tmp_path, requests, dict(ci, TOOLS_FILE="tools.yaml"), gitlab_ci=kind == "gitlab")

    [new_change] = [c for c in forge.changes if c["branch"] == "tool-request/n1"]
    assert (new_change["base"], new_change["title"]) == ("main", "Install bwa 0.7.18")
    pushed = subprocess.run(
        ["git", "show", "tool-request/n1:tools.yaml"], cwd=tmp_path / "origin.git", capture_output=True, text=True
    ).stdout
    assert "name: bwa" in pushed and "4a196b9c72c2" in pushed
    assert replies["n1"] == new_change["url"]
    assert replies["n2"] == reused_issue
    assert replies["n4"] == reused_change
    new_issues = {i["url"]: i for i in forge.issues[existing:]}
    assert set(new_issues) == {replies["n3"], replies["n5"]}
    assert all(i["labels"] == [LABEL] for i in new_issues.values())
    assert seen == ["n1", "n2", "n3", "n4", "n5"]


def test_script_opens_only_issues_without_a_tool_list(tmp_path, forge_server):
    forge, api = forge_server("github")
    bwa = {"name": "bwa", "owner": "devteam", "revisions": ["4a196b9c72c2"]}

    env = {"FORGE": "github", "FORGE_API": api, "FORGE_PROJECT": "org/tools"}
    replies, seen = _run(tmp_path, [_item("n1", "pr", "Install bwa 0.7.18", [bwa])], env)

    assert forge.changes == []
    assert replies["n1"] == forge.issues[0]["url"]
    assert seen == ["n1"]
