#!/usr/bin/env python3
"""Bridge pr-codex GitHub issues/review feedback into autonomous developer Kanban tasks.

Policy:
- Deterministic cron helper; no LLM in cron.
- Creates at most one developer task per run.
- Does not mutate GitHub directly.
- Prioritizes PR Must Fix repair tasks over starting new issue work.
- Starts new issue work only when conservative PR/developer capacity gates allow it.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import namedtuple
from typing import Any, Callable, Dict, Iterable, List, Sequence, Set

REPO = "yuki777/pr-codex"
BOARD = "pr-codex"
AUTOMATION_HUB_ISSUE = 28
ROADMAP_ISSUE = 15
TRACKER_ISSUES = {ROADMAP_ISSUE, AUTOMATION_HUB_ISSUE}
DEFAULT_ISSUE_ORDER = [36, 37, 38, 39, 40, 41, 42, 43]
CODEX_REVIEW_AUTHOR = "chatgpt-codex-connector"
HERMES_REPLY_AUTHORS = {"yuki777", "adachi", "ada"}
ACTIVE_STATUSES = {"todo", "ready", "running", "blocked", "triage"}
TERMINAL_OR_IGNORED_STATUSES = {"archived", "cancelled"}

Completed = namedtuple("Completed", "returncode stdout stderr")

GITHUB_OUTPUT_LANGUAGE_POLICY = """GITHUB OUTPUT LANGUAGE POLICY:
- GitHub-facing output must be written in Japanese by default: Issue titles/bodies, PR titles/bodies, issue comments, PR comments, PR review bodies, review replies, and public progress summaries.
- Keep machine-required tokens, file paths, command names, JSON keys, branch names, commit types, closing keywords such as `Closes #N`, and the `<!-- hermes-auto:... -->` sentinel exactly as required.
- Severity labels may include the English token in parentheses for automation compatibility, e.g. `要修正 (Must Fix)`, but the explanation and prose must be Japanese.
- Do not post English prose to GitHub unless quoting existing source text is necessary; summarize quoted English in Japanese instead.
""".strip()

PUBLIC_REPO_SAFETY = """PUBLIC REPO SAFETY:
- yuki777/pr-codex is public.
- Do not publish secrets, API keys, tokens, credential file contents, local credential paths, private business information, or sensitive raw logs to GitHub.
- Summarize and scrub all GitHub-facing output.
""".strip()

AUTONOMOUS_DEVELOPER_POLICY = """AUTONOMOUS DEVELOPER POLICY:
- ada explicitly authorized this pr-codex automation lane to proceed without per-step confirmation.
- Implement the assigned issue, create a PR, and report the PR URL in Kanban without asking for human confirmation.
- Use Japanese for GitHub issue/PR/comment-facing prose: PR title/body, PR comments, review replies, progress comments, and any issue comments.
- Do not merge the PR yourself. Do not push to main. Do not expose secrets.
- If a safety, credential, destructive-operation, or genuinely ambiguous scope blocker appears, block the Kanban task with a concise public-safe question instead of guessing.
""".strip()

AUTONOMOUS_REVIEW_FIX_POLICY = """AUTONOMOUS REVIEW-FIX POLICY:
- ada explicitly authorized this pr-codex automation lane to proceed without per-step confirmation.
- Fix only the referenced Must Fix for the current PR head.
- Use Japanese for GitHub issue/PR/comment-facing prose: PR title/body updates, PR comments, review replies, progress comments, and any issue comments.
- Push only to the PR branch. Do not push to main. Do not merge the PR yourself.
- If the PR head changed or the Must Fix no longer applies, complete with a public-safe superseded note instead of guessing.
""".strip()

STRICT_TDD = """STRICT TDD REQUIRED:
1. Write or update failing tests first for the behavior required by the issue/review finding.
2. Run the targeted test and confirm it fails for the expected reason.
3. Implement the minimal change needed to pass.
4. Re-run targeted tests, then the relevant/full test suite.
5. Refactor only after tests are green.
""".strip()

PR_REVIEW_SENTINEL_RE = re.compile(
    r"<!--\s*hermes-auto:pr-codex\s+pr-review\s+v\d+\s+pr=(\d+)\s+head=([0-9a-fA-F]+)\s*-->"
)


def run(cmd: List[str], *, input_text: str | None = None) -> Completed:
    p = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return Completed(p.returncode, p.stdout, p.stderr)


def parse_json(text: str) -> Any:
    return json.JSONDecoder(strict=False).decode(text or "null")


def gh_api(path: str, *, run_cmd: Callable[..., Completed] = run) -> Any:
    p = run_cmd(["gh", "api", path])
    if p.returncode != 0:
        raise RuntimeError(f"gh api failed for {path}: {p.stderr.strip()[:500]}")
    return parse_json(p.stdout)


def label_names(labels: Iterable[Any]) -> List[str]:
    names: List[str] = []
    for label in labels or []:
        if isinstance(label, str):
            names.append(label)
        elif isinstance(label, dict) and label.get("name"):
            names.append(str(label["name"]))
    return names


def safe_title(s: str, limit: int = 110) -> str:
    compact = " ".join((s or "").replace("\n", " ").split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def list_open_issues(repo: str = REPO, *, run_cmd: Callable[..., Completed] = run) -> List[Dict[str, Any]]:
    raw = gh_api(f"repos/{repo}/issues?state=open&per_page=100", run_cmd=run_cmd)
    issues: List[Dict[str, Any]] = []
    for item in raw or []:
        if "pull_request" in item:
            continue
        issues.append({
            "number": int(item.get("number") or 0),
            "title": item.get("title") or "",
            "url": item.get("html_url") or "",
            "labels": label_names(item.get("labels") or []),
            "updated_at": item.get("updated_at") or "",
            "created_at": item.get("created_at") or "",
        })
    return sorted(issues, key=lambda i: int(i.get("number") or 0))


def list_open_prs(repo: str = REPO, *, run_cmd: Callable[..., Completed] = run) -> List[Dict[str, Any]]:
    raw = gh_api(f"repos/{repo}/pulls?state=open&per_page=100", run_cmd=run_cmd)
    prs: List[Dict[str, Any]] = []
    for item in raw or []:
        head = item.get("head") or {}
        base = item.get("base") or {}
        prs.append({
            "number": int(item.get("number") or 0),
            "title": item.get("title") or "",
            "url": item.get("html_url") or "",
            "body": item.get("body") or "",
            "head_ref": head.get("ref") or "",
            "head_sha": head.get("sha") or "",
            "base_ref": base.get("ref") or "",
            "draft": bool(item.get("draft")),
        })
    return sorted(prs, key=lambda p: int(p.get("number") or 0))


def list_kanban_tasks(board: str = BOARD, *, run_cmd: Callable[..., Completed] = run) -> List[Dict[str, Any]]:
    p = run_cmd(["hermes", "kanban", "--board", board, "list", "--json"])
    if p.returncode != 0:
        raise RuntimeError(f"kanban list failed: {p.stderr.strip()[:500]} {p.stdout.strip()[:300]}")
    data = parse_json(p.stdout)
    return data if isinstance(data, list) else []


def extract_closing_issue_numbers(text: str) -> Set[int]:
    numbers: Set[int] = set()
    if not text:
        return numbers
    # Covers common forms such as "Closes #43" and "Fixes owner/repo#43".
    pattern = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(?:[\w.-]+/[\w.-]+)?#(\d+)\b")
    for match in pattern.finditer(text):
        numbers.add(int(match.group(1)))
    return numbers


def issues_with_open_closing_prs(open_prs: Sequence[Dict[str, Any]]) -> Set[int]:
    closed: Set[int] = set()
    for pr in open_prs:
        closed.update(extract_closing_issue_numbers(pr.get("body") or ""))
    return closed


def task_matches_issue(task: Dict[str, Any], issue_number: int) -> bool:
    haystack = f"{task.get('title') or ''}\n{task.get('body') or ''}"
    return bool(re.search(rf"(?<!\d)#{issue_number}(?!\d)", haystack))


def task_matches_pr_head(task: Dict[str, Any], pr_number: int, head_sha: str) -> bool:
    haystack = f"{task.get('title') or ''}\n{task.get('body') or ''}"
    return bool(re.search(rf"(?<!\d)PR\s*#{pr_number}(?!\d)", haystack, re.I)) and bool(head_sha and head_sha in haystack)


def has_developer_task_for_issue(tasks: Sequence[Dict[str, Any]], issue_number: int) -> bool:
    for task in tasks:
        if task.get("assignee") != "developer":
            continue
        status = str(task.get("status") or "").lower()
        if status in TERMINAL_OR_IGNORED_STATUSES:
            continue
        if task_matches_issue(task, issue_number):
            return True
    return False


def has_review_fix_task_for_pr(tasks: Sequence[Dict[str, Any]], pr_number: int, head_sha: str) -> bool:
    for task in tasks:
        if task.get("assignee") != "developer":
            continue
        status = str(task.get("status") or "").lower()
        if status in TERMINAL_OR_IGNORED_STATUSES:
            continue
        if task_matches_pr_head(task, pr_number, head_sha):
            return True
    return False


def active_developer_tasks(tasks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    active: List[Dict[str, Any]] = []
    for task in tasks:
        if task.get("assignee") != "developer":
            continue
        status = str(task.get("status") or "").lower()
        if status in ACTIVE_STATUSES or (status and status not in TERMINAL_OR_IGNORED_STATUSES and status != "done"):
            active.append(task)
    return active


def issue_sort_key(issue: Dict[str, Any], explicit_order: Sequence[int] = DEFAULT_ISSUE_ORDER) -> tuple[int, int]:
    number = int(issue.get("number") or 0)
    try:
        return (list(explicit_order).index(number), number)
    except ValueError:
        return (10_000, number)


def is_issue_candidate(issue: Dict[str, Any]) -> bool:
    number = int(issue.get("number") or 0)
    if number in TRACKER_ISSUES:
        return False
    labels = {name.lower() for name in label_names(issue.get("labels") or [])}
    if labels.intersection({"blocked", "wontfix", "duplicate", "invalid"}):
        return False
    return True


def pick_next_issue(
    issues: Sequence[Dict[str, Any]],
    open_prs: Sequence[Dict[str, Any]],
    tasks: Sequence[Dict[str, Any]],
    *,
    explicit_order: Sequence[int] = DEFAULT_ISSUE_ORDER,
) -> Dict[str, Any] | None:
    open_pr_issue_numbers = issues_with_open_closing_prs(open_prs)
    candidates: List[Dict[str, Any]] = []
    for issue in issues:
        number = int(issue.get("number") or 0)
        if not is_issue_candidate(issue):
            continue
        if number in open_pr_issue_numbers:
            continue
        if has_developer_task_for_issue(tasks, number):
            continue
        candidates.append(issue)
    if not candidates:
        return None
    return sorted(candidates, key=lambda issue: issue_sort_key(issue, explicit_order))[0]


def list_pr_review_comments(
    pr_number: int,
    repo: str = REPO,
    *,
    run_cmd: Callable[..., Completed] = run,
) -> Dict[str, List[Dict[str, Any]]]:
    issue_comments = gh_api(f"repos/{repo}/issues/{pr_number}/comments?per_page=100", run_cmd=run_cmd) or []
    reviews = gh_api(f"repos/{repo}/pulls/{pr_number}/reviews?per_page=100", run_cmd=run_cmd) or []
    return {
        "comments": issue_comments if isinstance(issue_comments, list) else [],
        "reviews": reviews if isinstance(reviews, list) else [],
    }


def split_repo(repo: str) -> tuple[str, str]:
    try:
        owner, name = repo.split("/", 1)
    except ValueError as exc:
        raise ValueError(f"repo must be owner/name, got {repo!r}") from exc
    return owner, name


def list_pr_review_thread_comment_page(
    thread_id: str,
    cursor: str,
    *,
    run_cmd: Callable[..., Completed] = run,
) -> Dict[str, Any]:
    query = """
query($id: ID!, $cursor: String!) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $cursor) {
        nodes {
          id
          body
          createdAt
          url
          author { login }
          authorAssociation
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""
    cmd = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={query}",
        "-f",
        f"id={thread_id}",
        "-f",
        f"cursor={cursor}",
    ]
    p = run_cmd(cmd)
    if p.returncode != 0:
        raise RuntimeError(f"gh graphql review thread comments failed for thread {thread_id}: {p.stderr.strip()[:500]}")
    payload = parse_json(p.stdout) or {}
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    node = data.get("node") if isinstance(data, dict) else {}
    return (node or {}).get("comments") or {}


def paginate_review_thread_comments(
    thread: Dict[str, Any],
    *,
    run_cmd: Callable[..., Completed] = run,
) -> None:
    comments = thread.get("comments") if isinstance(thread, dict) else None
    if not isinstance(comments, dict):
        return
    page_info = comments.get("pageInfo") or {}
    thread_id = str(thread.get("id") or "")
    while thread_id and page_info.get("hasNextPage"):
        cursor = page_info.get("endCursor")
        if not cursor:
            break
        next_comments = list_pr_review_thread_comment_page(thread_id, str(cursor), run_cmd=run_cmd)
        comments.setdefault("nodes", [])
        comments["nodes"].extend(next_comments.get("nodes") or [])
        page_info = next_comments.get("pageInfo") or {}
        comments["pageInfo"] = page_info


def list_pr_review_threads(
    pr_number: int,
    repo: str = REPO,
    *,
    run_cmd: Callable[..., Completed] = run,
) -> List[Dict[str, Any]]:
    owner, name = split_repo(repo)
    query = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 100) {
            nodes {
              id
              body
              createdAt
              url
              author { login }
              authorAssociation
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""
    threads: List[Dict[str, Any]] = []
    cursor: str | None = None
    while True:
        cmd = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={pr_number}",
        ]
        if cursor:
            cmd.extend(["-f", f"cursor={cursor}"])
        p = run_cmd(cmd)
        if p.returncode != 0:
            raise RuntimeError(f"gh graphql review threads failed for PR #{pr_number}: {p.stderr.strip()[:500]}")
        payload = parse_json(p.stdout) or {}
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        review_threads = (((data.get("repository") or {}).get("pullRequest") or {}).get("reviewThreads")) or {}
        nodes = review_threads.get("nodes") or []
        for thread in nodes:
            paginate_review_thread_comments(thread, run_cmd=run_cmd)
        threads.extend(nodes)
        page_info = review_threads.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return threads
        cursor = page_info.get("endCursor")


def _thread_comments(thread: Dict[str, Any]) -> List[Dict[str, Any]]:
    comments = ((thread.get("comments") or {}).get("nodes")) or []
    return comments if isinstance(comments, list) else []


def _comment_author(comment: Dict[str, Any]) -> str:
    author = comment.get("author") or comment.get("user") or {}
    return str(author.get("login") or "") if isinstance(author, dict) else ""


def _comment_created(comment: Dict[str, Any]) -> str:
    return str(comment.get("createdAt") or comment.get("created_at") or comment.get("submitted_at") or "")


def has_reply_after_codex_comment(thread: Dict[str, Any], *, reply_authors: Set[str] = HERMES_REPLY_AUTHORS) -> bool:
    comments = _thread_comments(thread)
    codex_times = [_comment_created(c) for c in comments if _comment_author(c) == CODEX_REVIEW_AUTHOR]
    if not codex_times:
        return False
    latest_codex = max(codex_times)
    for comment in comments:
        author = _comment_author(comment)
        if author == CODEX_REVIEW_AUTHOR:
            continue
        if author in reply_authors and _comment_created(comment) >= latest_codex:
            return True
    return False


def is_unreplied_codex_thread(thread: Dict[str, Any]) -> bool:
    if thread.get("isResolved") is True:
        return False
    if thread.get("isOutdated") is True:
        return False
    if not any(_comment_author(c) == CODEX_REVIEW_AUTHOR for c in _thread_comments(thread)):
        return False
    return not has_reply_after_codex_comment(thread)


def find_unreplied_codex_review_thread(pr: Dict[str, Any], threads: Sequence[Dict[str, Any]]) -> Dict[str, Any] | None:
    for thread in threads:
        if is_unreplied_codex_thread(thread):
            return {"pr": pr, "thread": thread}
    return None


def evaluate_codex_reply_gate(pr: Dict[str, Any], threads: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    unreplied = [thread for thread in threads if is_unreplied_codex_thread(thread)]
    return {
        "ok": not unreplied,
        "pr": int(pr.get("number") or 0),
        "unreplied_count": len(unreplied),
        "thread_ids": [str(thread.get("id")) for thread in unreplied],
    }


def find_unreplied_codex_threads_for_prs(
    open_prs: Sequence[Dict[str, Any]],
    repo: str = REPO,
    *,
    run_cmd: Callable[..., Completed] = run,
) -> Dict[int, Dict[str, Any]]:
    found: Dict[int, Dict[str, Any]] = {}
    for pr in open_prs:
        number = int(pr.get("number") or 0)
        threads = list_pr_review_threads(number, repo, run_cmd=run_cmd)
        finding = find_unreplied_codex_review_thread(pr, threads)
        if finding:
            found[number] = finding
    return found


def _item_url(item: Dict[str, Any]) -> str:
    return item.get("html_url") or item.get("url") or ((item.get("_links") or {}).get("html") or {}).get("href") or ""


def _has_actionable_must_fix(body: str) -> bool:
    text = body or ""
    # Low-noise no-blocking comments often mention the phrase "Must Fix" while
    # explicitly saying none were found. Treat those as non-actionable.
    negative_patterns = [
        r"(?i)\bno\s+blocking\s+findings\b",
        r"(?i)\bdid\s+not\s+identify\s+Must\s+Fix\b",
        r"(?i)\bno\s+Must\s+Fix\b",
        r"(?i)\b0\s+Must\s+Fix\b",
        r"ブロッカーなし",
        r"要修正\s*(?:\(Must\s+Fix\))?\s*(?:は|が)?\s*(?:ありません|なし|0件)",
        r"(?:検出|確認)され(?:てい)?ません",
    ]
    if any(re.search(pattern, text) for pattern in negative_patterns):
        return False
    positive_patterns = [
        r"(?im)^\s*#{2,4}\s*Must\s+Fix\b",
        r"(?im)^\s*Verdict:\s*.*\bMust\s+Fix\b",
        r"(?i)\b\d+\s+Must\s+Fix\b",
        r"(?im)^\s*#{2,4}\s*要修正(?:\s*\(Must\s+Fix\))?\b",
        r"要修正(?:\s*\(Must\s+Fix\))?.*(?:\d+\s*件|あり|必要)",
    ]
    return any(re.search(pattern, text) for pattern in positive_patterns)


def find_must_fix_review_for_pr(
    pr: Dict[str, Any],
    issue_comments: Sequence[Dict[str, Any]],
    reviews: Sequence[Dict[str, Any]],
) -> Dict[str, Any] | None:
    pr_number = int(pr.get("number") or 0)
    head_sha = pr.get("head_sha") or pr.get("head") or ""
    if not pr_number or not head_sha:
        return None
    combined: List[Dict[str, Any]] = []
    for item in issue_comments:
        combined.append({"kind": "comment", "body": item.get("body") or "", "url": _item_url(item), "created_at": item.get("created_at") or ""})
    for item in reviews:
        combined.append({"kind": "review", "body": item.get("body") or "", "url": _item_url(item), "created_at": item.get("submitted_at") or item.get("created_at") or ""})

    for item in reversed(combined):
        body = item.get("body") or ""
        match = PR_REVIEW_SENTINEL_RE.search(body)
        if not match:
            continue
        if int(match.group(1)) != pr_number:
            continue
        if match.group(2).lower() != head_sha.lower():
            continue
        if not _has_actionable_must_fix(body):
            continue
        return item
    return None


def find_must_fix_reviews_for_prs(
    open_prs: Sequence[Dict[str, Any]],
    repo: str = REPO,
    *,
    run_cmd: Callable[..., Completed] = run,
) -> Dict[int, Dict[str, Any]]:
    found: Dict[int, Dict[str, Any]] = {}
    for pr in open_prs:
        signals = list_pr_review_comments(int(pr.get("number") or 0), repo, run_cmd=run_cmd)
        review = find_must_fix_review_for_pr(pr, signals["comments"], signals["reviews"])
        if review:
            found[int(pr["number"])] = review
    return found


def pick_next_review_fix(
    open_prs: Sequence[Dict[str, Any]],
    must_fix_reviews_by_pr: Dict[int, Dict[str, Any]],
    tasks: Sequence[Dict[str, Any]],
) -> Dict[str, Any] | None:
    for pr in sorted(open_prs, key=lambda p: int(p.get("number") or 0)):
        number = int(pr.get("number") or 0)
        review = must_fix_reviews_by_pr.get(number)
        if not review:
            continue
        head_sha = pr.get("head_sha") or pr.get("head") or ""
        if has_review_fix_task_for_pr(tasks, number, head_sha):
            continue
        return {"pr": pr, "review": review}
    return None


def plan_next_action(
    issues: Sequence[Dict[str, Any]],
    open_prs: Sequence[Dict[str, Any]],
    tasks: Sequence[Dict[str, Any]],
    *,
    max_open_prs: int = 1,
    max_active_developer_tasks: int = 1,
    explicit_order: Sequence[int] = DEFAULT_ISSUE_ORDER,
    must_fix_reviews_by_pr: Dict[int, Dict[str, Any]] | None = None,
    codex_threads_by_pr: Dict[int, Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    active_dev = active_developer_tasks(tasks)
    if len(active_dev) >= max_active_developer_tasks:
        return {
            "action": "defer_developer_capacity",
            "active_developer_task_count": len(active_dev),
            "max_active_developer_tasks": max_active_developer_tasks,
            "active_developer_tasks": [t.get("id") for t in active_dev],
        }

    for pr in sorted(open_prs, key=lambda p: int(p.get("number") or 0)):
        number = int(pr.get("number") or 0)
        finding = (codex_threads_by_pr or {}).get(number)
        if not finding:
            continue
        head_sha = pr.get("head_sha") or pr.get("head") or ""
        if has_review_fix_task_for_pr(tasks, number, head_sha):
            continue
        return {"action": "create_codex_thread_reply_task", "pr": pr, "thread": finding["thread"]}

    review_fix = pick_next_review_fix(open_prs, must_fix_reviews_by_pr or {}, tasks)
    if review_fix:
        return {"action": "create_review_fix_task", **review_fix}

    if len(open_prs) >= max_open_prs:
        return {"action": "defer_open_pr_capacity", "open_pr_count": len(open_prs), "max_open_prs": max_open_prs}

    issue = pick_next_issue(issues, open_prs, tasks, explicit_order=explicit_order)
    if not issue:
        return {"action": "no_ready_issue"}
    return {"action": "create_developer_task", "issue": issue}


def build_task_body(issue: Dict[str, Any], repo: str = REPO) -> str:
    number = int(issue.get("number") or 0)
    title = safe_title(issue.get("title") or "")
    labels = ", ".join(label_names(issue.get("labels") or [])) or "(none)"
    url = issue.get("url") or f"https://github.com/{repo}/issues/{number}"
    branch_hint = f"feat/{number}"
    return f"""{PUBLIC_REPO_SAFETY}

{GITHUB_OUTPUT_LANGUAGE_POLICY}

{AUTONOMOUS_DEVELOPER_POLICY}

{STRICT_TDD}

Repo: {repo}
Issue: #{number} {title}
URL: {url}
Labels: {labels}
Suggested branch: {branch_hint}
Required PR closing keyword: Closes #{number}

Task:
1. Fetch and read Issue #{number}, its public comments, and relevant linked PRs/issues.
2. Start from latest `origin/main` in a new worktree branch, preferably `{branch_hint}`.
3. Follow strict TDD:
   - Add failing tests first for the executable behavior required by Issue #{number}.
   - Run the targeted test and confirm RED.
   - Implement the smallest safe change.
   - Confirm GREEN with targeted tests and relevant/full suite.
4. Verification before opening PR:
   - `python3 -m unittest discover -s tasks -p 'test_*.py'`
   - `python3 -m py_compile` for changed Python files
   - JSON/YAML syntax checks for changed metadata/workflow files if any
   - `git diff --check`
5. Commit with a conventional commit message and push the feature branch.
6. Open a GitHub PR against `main` with a Japanese, public-safe title/body including `Closes #{number}` and tests run. Keep `Closes #{number}` exactly for GitHub auto-close.
7. Complete this Kanban task in Japanese with the PR URL, summary, tests run, and known follow-ups.

Hard stops:
- Do not merge the PR yourself.
- Do not push to main.
- Do not expose secrets, tokens, credential file contents, local credential paths, private business information, or raw sensitive logs.
- Do not create unrelated broad refactors outside Issue #{number}'s scope.
""".strip()


def _must_fix_excerpt(body: str, limit: int = 1400) -> str:
    text = (body or "").strip()
    match = re.search(r"(?is)(#{2,4}\s*Must\s+Fix.*?)(?:\n#{2,4}\s+|\Z)", text)
    excerpt = (match.group(1) if match else text).strip()
    excerpt = re.sub(r"<!--.*?-->", "", excerpt, flags=re.S).strip()
    return excerpt if len(excerpt) <= limit else excerpt[: limit - 1] + "…"


def _thread_excerpt(thread: Dict[str, Any], limit: int = 1400) -> str:
    comments = _thread_comments(thread)
    codex_comments = [comment for comment in comments if _comment_author(comment) == CODEX_REVIEW_AUTHOR]
    source = codex_comments[-1] if codex_comments else (comments[0] if comments else {})
    text = str(source.get("body") or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_codex_review_thread_task_body(pr: Dict[str, Any], thread: Dict[str, Any], repo: str = REPO) -> str:
    number = int(pr.get("number") or 0)
    head_sha = pr.get("head_sha") or pr.get("head") or ""
    branch = pr.get("head_ref") or ""
    title = safe_title(pr.get("title") or "")
    pr_url = pr.get("url") or f"https://github.com/{repo}/pull/{number}"
    comments = _thread_comments(thread)
    first_url = (comments[0] or {}).get("url") if comments else ""
    excerpt = _thread_excerpt(thread)
    return f"""{PUBLIC_REPO_SAFETY}

{GITHUB_OUTPUT_LANGUAGE_POLICY}

{AUTONOMOUS_REVIEW_FIX_POLICY}

{STRICT_TDD}

Repo: {repo}
PR: #{number} {title}
URL: {pr_url}
Branch: {branch}
Head under review: {head_sha}
Review thread: {thread.get('id')}
Path/line: {thread.get('path') or 'n/a'}:{thread.get('line') or 'n/a'}
Source comment: {first_url}
Reviewer: {CODEX_REVIEW_AUTHOR}

Review finding summary:
{excerpt}

Task:
1. Check out PR #{number} branch `{branch}` from latest remote state.
2. Re-read the {CODEX_REVIEW_AUTHOR} review thread and current PR state.
3. If the finding is valid, fix it with strict TDD: write a failing regression test or docs/assertion check first, confirm RED, implement, then confirm GREEN.
4. If it is already fixed, outdated, duplicate, or intentionally not accepted, document the reason with evidence.
5. Run targeted tests, `python3 -m unittest discover -s tasks -p 'test_*.py'`, py_compile for changed Python files, and `git diff --check` as applicable.
6. Push only to the PR branch if code/docs changed.
7. 必ず review thread に日本語で返信し、対応 commit/PR、検証コマンド、または採用しない理由を明示する。
8. Complete this Kanban task in Japanese with the reply URL, commit SHA if any, and tests run.

Hard stops:
- Do not merge PR #{number} while a {CODEX_REVIEW_AUTHOR} thread lacks an explicit reply.
- Do not push to main.
- Do not expose secrets or sensitive raw logs.
- GitHub-facing prose must be Japanese.
""".strip()


def build_review_fix_task_body(pr: Dict[str, Any], review: Dict[str, Any], repo: str = REPO) -> str:
    number = int(pr.get("number") or 0)
    head_sha = pr.get("head_sha") or pr.get("head") or ""
    branch = pr.get("head_ref") or ""
    title = safe_title(pr.get("title") or "")
    pr_url = pr.get("url") or f"https://github.com/{repo}/pull/{number}"
    review_url = review.get("url") or ""
    excerpt = _must_fix_excerpt(review.get("body") or "")
    return f"""{PUBLIC_REPO_SAFETY}

{GITHUB_OUTPUT_LANGUAGE_POLICY}

{AUTONOMOUS_REVIEW_FIX_POLICY}

{STRICT_TDD}

Repo: {repo}
PR: #{number} {title}
URL: {pr_url}
Branch: {branch}
Head under review: {head_sha}
Hermes review comment: {review_url}

Must Fix summary:
{excerpt}

Task:
1. Check out PR #{number} branch `{branch}` from latest remote state.
2. Re-read the public review comment and current PR state.
3. Follow strict TDD: write a failing regression test for the Must Fix and confirm RED.
4. Implement the smallest safe fix and confirm GREEN.
5. Run targeted tests, `python3 -m unittest discover -s tasks -p 'test_*.py'`, py_compile for changed Python files, and `git diff --check`.
6. Commit with a conventional message and push to `{branch}`.
7. Complete this Kanban task in Japanese with pushed commit SHA, tests run, and any follow-up.

Hard stops:
- Do not merge PR #{number}.
- Do not push to main.
- Do not expose secrets or sensitive raw logs.
- If you post any GitHub comment/reply, write the prose in Japanese.
""".strip()


def create_developer_task(
    issue: Dict[str, Any],
    *,
    repo: str = REPO,
    board: str = BOARD,
    run_cmd: Callable[..., Completed] = run,
) -> Dict[str, Any]:
    number = int(issue.get("number") or 0)
    title = f"[developer] #{number} {safe_title(issue.get('title') or '')}"
    key = f"developer:auto:issue:{repo}:{number}:v1"
    body = build_task_body(issue, repo)
    cmd = [
        "hermes", "kanban", "--board", board, "create", title,
        "--assignee", "developer",
        "--workspace", "worktree",
        "--created-by", "pr-codex-developer-bridge",
        "--skill", "test-driven-development",
        "--skill", "github-pr-workflow",
        "--idempotency-key", key,
        "--max-runtime", "4h",
        "--priority", "100",
        "--body", body,
        "--json",
    ]
    p = run_cmd(cmd)
    if p.returncode != 0:
        raise RuntimeError(f"developer task create failed for #{number}: {p.stderr.strip()[:500]} {p.stdout.strip()[:300]}")
    data = parse_json(p.stdout)
    if not isinstance(data, dict):
        data = {"raw": p.stdout.strip()[:500]}
    data.setdefault("title", title)
    data.setdefault("idempotency_key", key)
    data.setdefault("issue", number)
    return data


def create_codex_thread_reply_task(
    pr: Dict[str, Any],
    thread: Dict[str, Any],
    *,
    repo: str = REPO,
    board: str = BOARD,
    run_cmd: Callable[..., Completed] = run,
) -> Dict[str, Any]:
    number = int(pr.get("number") or 0)
    head_sha = pr.get("head_sha") or pr.get("head") or ""
    thread_id = str(thread.get("id") or "unknown")
    title = f"[developer] PR #{number} Codex review reply required: {safe_title(pr.get('title') or '')}"
    key = f"developer:auto:codex-thread-reply:{repo}:{number}:{head_sha}:{thread_id}:v1"
    body = build_codex_review_thread_task_body(pr, thread, repo)
    cmd = [
        "hermes", "kanban", "--board", board, "create", title,
        "--assignee", "developer",
        "--workspace", "worktree",
        "--created-by", "pr-codex-developer-bridge",
        "--skill", "test-driven-development",
        "--skill", "github-pr-workflow",
        "--idempotency-key", key,
        "--max-runtime", "4h",
        "--priority", "130",
        "--body", body,
        "--json",
    ]
    p = run_cmd(cmd)
    if p.returncode != 0:
        raise RuntimeError(f"codex thread task create failed for PR #{number}: {p.stderr.strip()[:500]} {p.stdout.strip()[:300]}")
    data = parse_json(p.stdout)
    if not isinstance(data, dict):
        data = {"raw": p.stdout.strip()[:500]}
    data.setdefault("title", title)
    data.setdefault("idempotency_key", key)
    data.setdefault("pr", number)
    data.setdefault("head_sha", head_sha)
    data.setdefault("thread_id", thread_id)
    return data


def create_review_fix_task(
    pr: Dict[str, Any],
    review: Dict[str, Any],
    *,
    repo: str = REPO,
    board: str = BOARD,
    run_cmd: Callable[..., Completed] = run,
) -> Dict[str, Any]:
    number = int(pr.get("number") or 0)
    head_sha = pr.get("head_sha") or pr.get("head") or ""
    title = f"[developer] PR #{number} Must Fix repair: {safe_title(pr.get('title') or '')}"
    key = f"developer:auto:review-fix:{repo}:{number}:{head_sha}:v1"
    body = build_review_fix_task_body(pr, review, repo)
    cmd = [
        "hermes", "kanban", "--board", board, "create", title,
        "--assignee", "developer",
        "--workspace", "worktree",
        "--created-by", "pr-codex-developer-bridge",
        "--skill", "test-driven-development",
        "--skill", "github-pr-workflow",
        "--idempotency-key", key,
        "--max-runtime", "4h",
        "--priority", "120",
        "--body", body,
        "--json",
    ]
    p = run_cmd(cmd)
    if p.returncode != 0:
        raise RuntimeError(f"review fix task create failed for PR #{number}: {p.stderr.strip()[:500]} {p.stdout.strip()[:300]}")
    data = parse_json(p.stdout)
    if not isinstance(data, dict):
        data = {"raw": p.stdout.strip()[:500]}
    data.setdefault("title", title)
    data.setdefault("idempotency_key", key)
    data.setdefault("pr", number)
    data.setdefault("head_sha", head_sha)
    return data


def dispatch_board(board: str = BOARD, *, run_cmd: Callable[..., Completed] = run) -> Dict[str, Any]:
    p = run_cmd(["hermes", "kanban", "--board", board, "dispatch"])
    return {"returncode": p.returncode, "stdout": p.stdout.strip()[:1000], "stderr": p.stderr.strip()[:1000]}


def parse_issue_order(value: str) -> List[int]:
    if not value:
        return list(DEFAULT_ISSUE_ORDER)
    return [int(part.strip().lstrip("#")) for part in value.split(",") if part.strip()]


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description="Create the next autonomous pr-codex developer Kanban task from PR review feedback or open GitHub issues")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--board", default=BOARD)
    ap.add_argument("--max-open-prs", type=int, default=1)
    ap.add_argument("--max-active-developer-tasks", type=int, default=1)
    ap.add_argument("--issue-order", default=",".join(str(n) for n in DEFAULT_ISSUE_ORDER))
    ap.add_argument("--skip-review-fixes", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-dispatch", action="store_true")
    args = ap.parse_args(argv)

    explicit_order = parse_issue_order(args.issue_order)
    issues = list_open_issues(args.repo)
    open_prs = list_open_prs(args.repo)
    tasks = list_kanban_tasks(args.board)

    must_fix_reviews_by_pr: Dict[int, Dict[str, Any]] = {}
    codex_threads_by_pr: Dict[int, Dict[str, Any]] = {}
    # Avoid extra GitHub API calls when developer capacity is already full; the action cannot proceed anyway.
    if not args.skip_review_fixes and len(active_developer_tasks(tasks)) < args.max_active_developer_tasks:
        codex_threads_by_pr = find_unreplied_codex_threads_for_prs(open_prs, args.repo)
        must_fix_reviews_by_pr = find_must_fix_reviews_for_prs(open_prs, args.repo)

    plan = plan_next_action(
        issues,
        open_prs,
        tasks,
        max_open_prs=args.max_open_prs,
        max_active_developer_tasks=args.max_active_developer_tasks,
        explicit_order=explicit_order,
        must_fix_reviews_by_pr=must_fix_reviews_by_pr,
        codex_threads_by_pr=codex_threads_by_pr,
    )

    if args.dry_run:
        output = {
            "repo": args.repo,
            "board": args.board,
            "open_issue_count": len(issues),
            "open_pr_count": len(open_prs),
            "active_developer_task_count": len(active_developer_tasks(tasks)),
            "must_fix_prs": sorted(must_fix_reviews_by_pr.keys()),
            "unreplied_codex_thread_prs": sorted(codex_threads_by_pr.keys()),
            "plan": plan,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if plan.get("action") == "create_codex_thread_reply_task":
        created = create_codex_thread_reply_task(plan["pr"], plan["thread"], repo=args.repo, board=args.board)
        dispatch = None if args.no_dispatch else dispatch_board(args.board)
        result = {"plan": plan, "created": created, "dispatch": dispatch}
    elif plan.get("action") == "create_review_fix_task":
        created = create_review_fix_task(plan["pr"], plan["review"], repo=args.repo, board=args.board)
        dispatch = None if args.no_dispatch else dispatch_board(args.board)
        result = {"plan": plan, "created": created, "dispatch": dispatch}
    elif plan.get("action") == "create_developer_task":
        created = create_developer_task(plan["issue"], repo=args.repo, board=args.board)
        dispatch = None if args.no_dispatch else dispatch_board(args.board)
        result = {"plan": plan, "created": created, "dispatch": dispatch}
    else:
        # Script-only cron should stay silent when there is nothing safe to do.
        if args.json:
            print(json.dumps({"plan": plan}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        tid = result["created"].get("task_id") or result["created"].get("id") or result["created"].get("raw") or "?"
        if plan.get("action") == "create_codex_thread_reply_task":
            print(f"pr-codex developer bridge: created {tid} to reply to Codex thread on PR #{plan['pr'].get('number')}")
        elif plan.get("action") == "create_review_fix_task":
            print(f"pr-codex developer bridge: created {tid} to repair PR #{plan['pr'].get('number')}")
        else:
            print(f"pr-codex developer bridge: created {tid} for issue #{plan['issue'].get('number')}")
        if dispatch and dispatch.get("stdout"):
            print(dispatch["stdout"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print(f"pr-codex developer bridge ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
