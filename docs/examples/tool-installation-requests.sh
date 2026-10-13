#!/usr/bin/env bash
# Opens an issue or a pull request in this repository for each new tool
# installation request on a Galaxy server, then tells the requester where it is
# handled. Run it on a schedule, from any CI system or cron, in a clean checkout
# of the repository. See the consume-tool-installation-requests page of the
# ephemeris docs.
#
# It needs bash, git, curl, jq and ephemeris, and reads these variables:
#
# EPHEMERIS_GALAXY   Galaxy server.
# EPHEMERIS_API_KEY  API key of a Galaxy admin account.
# FORGE_TOKEN        Token that may push branches and open issues and pull requests.
# FORGE              github or gitlab. Found out in GitHub Actions and GitLab CI.
# FORGE_API          REST API URL. Found out in GitHub Actions and GitLab CI, and
#                    https://api.github.com for GitHub elsewhere.
# FORGE_PROJECT      owner/name on GitHub, project ID or path on GitLab. Found out
#                    in GitHub Actions and GitLab CI.
# PUSH_URL           Where to push branches (default: origin, or the project URL
#                    in GitLab CI, whose origin only allows fetching).
# BASE_BRANCH        Branch that pull requests go to (default: the checked out one).
# TOOLS_FILE         Tool list that pull requests add tools to.
# APPLY_COMMAND      Shell command that adds the tools of the work item in the file
#                    $ITEM to the checkout, for repositories that are not a single
#                    tool list. Defaults to render-yaml into TOOLS_FILE.
# ISSUE_LABEL        Label of the issues opened here (default: tool installation request).
# EMIT_OPTIONS       Options for emit, written as in a shell, for example
#                    --tool-shed-url https://toolshed.example.org/ --panel-label 'Other Tools'.
#
# Without TOOLS_FILE or APPLY_COMMAND, every request becomes an issue.
set -euo pipefail

if [ -z "${FORGE:-}" ]; then
    if [ "${GITHUB_ACTIONS:-}" = true ]; then
        FORGE=github
    elif [ "${GITLAB_CI:-}" = true ]; then
        FORGE=gitlab
    fi
fi
case "${FORGE:-}" in
github)
    FORGE_API=${FORGE_API:-${GITHUB_API_URL:-https://api.github.com}}
    FORGE_PROJECT=${FORGE_PROJECT:-${GITHUB_REPOSITORY:-}}
    PUSH_URL=${PUSH_URL:-origin}
    git_user=x-access-token
    ;;
gitlab)
    FORGE_API=${FORGE_API:-${CI_API_V4_URL:-}}
    FORGE_PROJECT=${FORGE_PROJECT:-${CI_PROJECT_ID:-}}
    git_user=oauth2
    if [ -z "${PUSH_URL:-}" ] && [ -n "${CI_SERVER_URL:-}" ]; then
        PUSH_URL="$CI_SERVER_URL/$CI_PROJECT_PATH.git"
        # GitLab CI makes git use the job token, which may not push, for URLs of its server.
        # It leaves a URL that names a user alone, so git asks the credential helper below.
        case "$PUSH_URL" in
        http://* | https://*) PUSH_URL="${PUSH_URL/:\/\//://$git_user@}" ;;
        esac
    fi
    PUSH_URL=${PUSH_URL:-origin}
    ;;
*)
    echo "Set FORGE to github or gitlab" >&2
    exit 1
    ;;
esac
: "${FORGE_TOKEN:?}" "${FORGE_API:?}" "${FORGE_PROJECT:?}"
BASE_BRANCH=${BASE_BRANCH:-${CI_COMMIT_BRANCH:-$(git branch --show-current)}}
: "${BASE_BRANCH:?Set BASE_BRANCH to the branch that pull requests go to}"
ISSUE_LABEL=${ISSUE_LABEL:-tool installation request}
if [ -z "${APPLY_COMMAND:-}" ] && [ -n "${TOOLS_FILE:-}" ]; then
    # shellcheck disable=SC2016 # expanded when the command runs
    APPLY_COMMAND='jq -s . "$ITEM" | consume-tool-installation-requests render-yaml --tools-file "$TOOLS_FILE" -o "$TOOLS_FILE"'
fi
export FORGE_TOKEN TOOLS_FILE

# Pull requests commit every change that APPLY_COMMAND makes.
if [ -n "$(git status --porcelain)" ]; then
    echo "Run this in a clean checkout" >&2
    exit 1
fi
base=$(git rev-parse HEAD)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

uri() { jq -rn --arg value "$1" '$value | @uri'; }

headers() {
    if [ "$FORGE" = github ]; then
        printf '%s\n' "Authorization: Bearer $FORGE_TOKEN" "Accept: application/vnd.github+json"
    else
        printf '%s\n' "PRIVATE-TOKEN: $FORGE_TOKEN"
    fi
    echo "Content-Type: application/json"
}

# api METHOD PATH [JSON_FILE] prints the response to a request about this repository.
api() {
    local status
    # Headers come from a file, so that the token stays out of the process list.
    status=$(curl --silent --show-error --output "$work/response.json" --write-out '%{http_code}' \
        --request "$1" --header @<(headers) ${3:+--data-binary "@$3"} "$FORGE_API/$project$2") || return 1
    if [ "$status" -ge 300 ]; then
        echo "$1 $2 failed with HTTP $status: $(head -c 500 "$work/response.json")" >&2
        return 1
    fi
    cat "$work/response.json"
}

if [ "$FORGE" = github ]; then
    project="repos/$FORGE_PROJECT"
    label_color=ededed
    issue_fields='.[] | select(has("pull_request") | not) | {url: .html_url, body: (.body // "")}'

    # Prints the URL of a pull request from this repository's branch $1, open or not.
    find_change() {
        api GET "/pulls?state=all&head=$(uri "${FORGE_PROJECT%%/*}:$1")" | jq -r '.[0].html_url // empty'
    }

    open_change() {
        jq -n --arg head "$1" --arg base "$BASE_BRANCH" --arg title "$2" --rawfile body "$work/body.md" \
            '{$head, $base, $title, $body}' > "$work/payload.json"
        api POST /pulls "$work/payload.json" | jq -r .html_url
    }

    open_issue() {
        jq -n --arg title "$1" --rawfile body "$work/body.md" --arg label "$ISSUE_LABEL" \
            '{$title, $body, labels: [$label]}' > "$work/payload.json"
        api POST /issues "$work/payload.json" | jq -r .html_url
    }
else
    project="projects/$(uri "$FORGE_PROJECT")"
    label_color='#ededed'
    issue_fields='.[] | {url: .web_url, body: (.description // "")}'

    # Prints the URL of a merge request from this project's branch $1, open or not.
    find_change() {
        api GET "/merge_requests?state=all&source_branch=$(uri "$1")" |
            jq -r 'map(select(.source_project_id == .target_project_id))[0].web_url // empty'
    }

    open_change() {
        jq -n --arg source_branch "$1" --arg target_branch "$BASE_BRANCH" --arg title "$2" \
            --rawfile description "$work/body.md" \
            '{$source_branch, $target_branch, $title, $description, remove_source_branch: true}' > "$work/payload.json"
        api POST /merge_requests "$work/payload.json" | jq -r .web_url
    }

    open_issue() {
        jq -n --arg title "$1" --rawfile description "$work/body.md" --arg labels "$ISSUE_LABEL" \
            '{$title, $description, $labels}' > "$work/payload.json"
        api POST /issues "$work/payload.json" | jq -r .web_url
    }
fi

push() {
    # The credential helper reads the token from the environment when git asks for it.
    git -c credential.helper= \
        -c "credential.helper=!f() { echo username=$git_user; echo \"password=\$FORGE_TOKEN\"; }; f" \
        push --quiet --force "$PUSH_URL" "HEAD:refs/heads/$1"
}

handle() {
    local id action title branch url
    jq -r .body "$work/item.json" > "$work/body.md"
    id=$(jq -r .notification_id "$work/item.json")
    action=$(jq -r .action "$work/item.json")
    title=$(jq -r .title "$work/item.json")
    branch="tool-request/$id"
    # Reuse what an earlier run opened, also if the request turned from an issue into a pull request since.
    url=$(find_change "$branch") || return 1
    if [ -z "$url" ]; then
        # The first line of the body names the request.
        url=$(jq -rn --arg first "$(head -n 1 "$work/body.md")" \
            'first(inputs | select(.body | startswith($first)) | .url) // empty' "$work/issues.jsonl")
    fi
    if [ -z "$url" ] && [ "$action" = pr ] && [ -n "${APPLY_COMMAND:-}" ]; then
        git switch --quiet -C "$branch" "$base" || return 1
        ITEM="$work/item.json" bash -eo pipefail -c "$APPLY_COMMAND" || return 1
        git add --all || return 1
        if git diff --cached --quiet; then
            # The repository has these tools already, so let an admin look at the request.
            action=issue
        else
            git commit --quiet --message "$title" || return 1
            push "$branch" || return 1
            url=$(open_change "$branch" "$title") || return 1
        fi
    fi
    if [ -z "$url" ]; then
        url=$(open_issue "$title") || return 1
    fi
    consume-tool-installation-requests reply --notification-id "$id" --url "$url" &&
        consume-tool-installation-requests mark-seen "$id"
}

# Make sure the label exists. Creating one that exists fails harmlessly.
jq -n --arg name "$ISSUE_LABEL" --arg color "$label_color" '{$name, $color}' > "$work/label.json"
api POST /labels "$work/label.json" > /dev/null 2>&1 || true

# Listing issues by label, unlike searching them, sees issues opened moments ago.
: > "$work/issues.jsonl"
for ((page = 1; ; page++)); do
    api GET "/issues?state=all&per_page=100&page=$page&labels=$(uri "$ISSUE_LABEL")" > "$work/page.json"
    [ "$(jq length "$work/page.json")" -gt 0 ] || break
    jq -c "$issue_fields" "$work/page.json" >> "$work/issues.jsonl"
done

eval "consume-tool-installation-requests emit ${EMIT_OPTIONS:-}" > "$work/requests.json"
failed=0
for i in $(seq 0 $(($(jq length "$work/requests.json") - 1))); do
    jq ".[$i]" "$work/requests.json" > "$work/item.json"
    if ! handle; then
        echo "Request $(jq -r .notification_id "$work/item.json") failed, the next run tries again" >&2
        failed=1
    fi
    git switch --quiet --discard-changes --detach "$base"
    git clean --quiet --force -d
done
exit "$failed"
