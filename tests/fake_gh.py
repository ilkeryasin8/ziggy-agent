#!/usr/bin/env python3
"""Stand-in for `gh` in the publish and listener tests.

Records argv, whether GH_TOKEN was set and the body (`--body-file F` or `-F body=@F`) to $FAKE_GH_LOG.
`auth token --user U` prints a fake token; `pr list` prints nothing (no open PR) unless the branch
already has one recorded; `pr create` checks the head branch exists on the bare origin
($FAKE_GH_ORIGIN) and prints a URL numbered by creation order (the first PR is #1); `api --method PATCH
repos/O/R/pulls/N` (the PR update or a base change) prints nothing (`--silent`); `pr comment` prints a comment
URL; `pr list` without --head and `pr view <number>` answer from $FAKE_GH_PULLS (open PRs as JSON); `pr view
<url>` answers that PR's state ($FAKE_GH_PR_STATES, {"<n>": state}, else $FAKE_GH_PR_STATE, default MERGED) and
head; `repo create OWNER/NAME` makes a bare repository under $FAKE_GH_REMOTES.

PR feedback (feedback.py) reads from $FAKE_GH_FEEDBACK, a JSON object: `api repos/O/R/pulls/N` answers
{"head": {"sha": head_sha}}; its `/comments`, `issues/N/comments` and `/reviews` answer review_comments,
issue_comments and reviews; `commits/<sha>/check-runs` and `commits/<sha>/status` answer check_runs and
statuses; `actions/jobs/<job id>/logs` prints logs[<job id>] as plain text (404 without one). POSTs
(`api --method POST …`, the replies) print nothing; the log has them.

The lead (lead.py) reads from $FAKE_GH_LEAD, a JSON object: `api repos/O/R/actions/runs?branch=B` answers
{"workflow_runs": runs} (only the runs whose head_branch is B), and `api repos/O/R/issues?…&assignee=U` /
`…&labels=L` answer the open issues assigned to U / labelled L ("error": "<text>" makes every lead call fail).
"""
from __future__ import annotations  # the panel may spawn this with an older python3 than the suite's

import json
import os
import subprocess
import sys

args = sys.argv[1:]
log_path = os.environ["FAKE_GH_LOG"]
body_file = (args[args.index("--body-file") + 1] if "--body-file" in args
             else next((a.removeprefix("body=@") for a in args if a.startswith("body=@")), None))
body = open(body_file, encoding="utf-8").read() if body_file else None
with open(log_path, "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": args, "gh_token": os.environ.get("GH_TOKEN"), "body": body}) + "\n")


def creates() -> list[list[str]]:
    """argv of every `pr create` so far, in order: PR n is the n-th."""
    return [c for c in (json.loads(line)["argv"] for line in open(log_path, encoding="utf-8")) if c[:2] == ["pr", "create"]]


if args[:2] == ["auth", "status"]:
    print("github.com\n  ✓ Logged in to github.com account tester (keyring)\n  ✓ Logged in to github.com account work-me (keyring)")
elif args[:2] == ["api", "user/orgs"]:
    print('["acme-org"]' if os.environ.get("GH_TOKEN", "").endswith("work-me") else "[]")
elif args[:2] == ["auth", "token"]:
    print("fake-token-for-" + args[args.index("--user") + 1])
elif args[:2] == ["pr", "list"] and "--head" not in args:
    # The repository's open PRs: $FAKE_GH_PULLS (a JSON list as `gh pr list --json` prints it).
    print(json.dumps([{k: v for k, v in pr.items() if k not in ("body", "state")} for pr in json.loads(os.environ.get("FAKE_GH_PULLS", "[]"))]))
elif args[:2] == ["pr", "view"] and args[2].isdigit():
    found = [pr for pr in json.loads(os.environ.get("FAKE_GH_PULLS", "[]")) if pr["number"] == int(args[2])]
    if not found:
        print(f"no pull request {args[2]}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"state": "OPEN", "body": "", **found[0]}))
elif args[:2] == ["pr", "list"]:
    head = args[args.index("--head") + 1]
    numbers = [n for n, c in enumerate(creates(), 1) if c[c.index("--head") + 1] == head]
    if numbers:
        print(f"https://github.example.test/acme/demo/pull/{numbers[0]}")
elif args[:2] == ["pr", "create"]:
    head = args[args.index("--head") + 1]
    refs = subprocess.run(["git", "--git-dir", os.environ["FAKE_GH_ORIGIN"], "show-ref", f"refs/heads/{head}"], capture_output=True)
    if refs.returncode != 0:
        print(f"head {head} not on origin", file=sys.stderr)
        sys.exit(1)
    print("Creating draft pull request")
    print(f"https://github.example.test/acme/demo/pull/{len(creates())}")
elif args[:2] == ["pr", "view"]:
    # The PR's state and its head: the branch it was opened from, as it is on the bare origin now.
    made = creates()
    number = int(args[2].rstrip("/").rsplit("/", 1)[-1])
    argv = made[number - 1] if 0 < number <= len(made) else made[-1]
    head = argv[argv.index("--head") + 1]
    oid = subprocess.run(["git", "--git-dir", os.environ["FAKE_GH_ORIGIN"], "rev-parse", f"refs/heads/{head}"],
                         capture_output=True, text=True).stdout.strip()
    states = json.loads(os.environ.get("FAKE_GH_PR_STATES") or "{}")
    print(json.dumps({"state": states.get(str(number), os.environ.get("FAKE_GH_PR_STATE", "MERGED")), "headRefOid": oid}))
elif args[:3] == ["api", "--method", "PATCH"] and "/pulls/" in args[3]:
    number = args[3].rstrip("/").rsplit("/", 1)[-1]
    if json.loads(os.environ.get("FAKE_GH_PR_STATES") or "{}").get(number) in ("MERGED", "CLOSED") and "base=" in " ".join(args):
        print("gh: Validation Failed (HTTP 422)", file=sys.stderr)  # GitHub refuses a closed PR's base change
        sys.exit(1)
    if "--silent" not in args:
        print(json.dumps({"html_url": "https://github.example.test/" + args[3].removeprefix("repos/").replace("/pulls/", "/pull/")}))
elif args[:3] == ["api", "--method", "POST"] and ("/replies" in args[3] or args[3].endswith("/comments")):
    pass  # a reply to PR feedback; recorded in the log
elif args[:1] == ["api"] and not args[1].startswith("--method"):
    feedback = json.loads(os.environ.get("FAKE_GH_FEEDBACK", "{}"))
    endpoint = next(a for a in args[1:] if not a.startswith("--")).split("?")[0]
    query = dict(p.split("=", 1) for p in next(a for a in args[1:] if not a.startswith("--")).partition("?")[2].split("&") if "=" in p)
    parts = endpoint.split("/")  # repos O R pulls N [comments|reviews] | repos O R issues N comments | repos O R commits SHA …
    lead = json.loads(os.environ.get("FAKE_GH_LEAD", "{}"))
    if (parts[3:5] == ["actions", "runs"] or parts[3:] == ["issues"]) and lead.get("error"):
        print(f"gh: {lead['error']}", file=sys.stderr)
        sys.exit(1)
    if parts[3:5] == ["actions", "runs"] and len(parts) == 5:
        print(json.dumps({"workflow_runs": [r for r in lead.get("runs", []) if r.get("head_branch") == query.get("branch")]}))
    elif parts[3:] == ["issues"]:
        found = [i for i in lead.get("issues", []) if i.get("state", "open") == query.get("state", "open")
                 and ("assignee" not in query or query["assignee"] in [a["login"] for a in i.get("assignees", [])])
                 and ("labels" not in query or query["labels"] in [lb["name"] for lb in i.get("labels", [])])]
        print(json.dumps(found))
    elif parts[3] == "pulls" and len(parts) == 5:
        pr_state = json.loads(os.environ.get("FAKE_GH_PR_STATES") or "{}").get(parts[4], "OPEN")
        if "--jq" in args and args[args.index("--jq") + 1] == ".state":
            print("closed" if pr_state in ("MERGED", "CLOSED") else "open")
        else:
            print(json.dumps({"number": int(parts[4]), "head": {"sha": feedback.get("head_sha", "0" * 40)}}))
    elif parts[3] == "pulls" and parts[5] == "comments":
        print(json.dumps(feedback.get("review_comments", [])))
    elif parts[3] == "pulls" and parts[5] == "reviews":
        print(json.dumps(feedback.get("reviews", [])))
    elif parts[3] == "issues" and parts[5] == "comments":
        print(json.dumps(feedback.get("issue_comments", [])))
    elif parts[3] == "commits" and parts[5] == "check-runs":
        print(json.dumps({"total_count": len(feedback.get("check_runs", [])), "check_runs": feedback.get("check_runs", [])}))
    elif parts[3] == "commits" and parts[5] == "status":
        print(json.dumps({"state": "failure" if feedback.get("statuses") else "success", "statuses": feedback.get("statuses", [])}))
    elif parts[3:5] == ["actions", "jobs"] and parts[6:] == ["logs"]:
        logs = feedback.get("logs", {})
        if parts[5] not in logs:
            print("gh: Not Found (HTTP 404)", file=sys.stderr)
            sys.exit(1)
        print(logs[parts[5]])
    else:
        print(f"fake gh: unsupported api {endpoint}", file=sys.stderr)
        sys.exit(2)
elif args[:2] == ["pr", "comment"]:
    print(args[2] + "#issuecomment-1")
elif args[:2] == ["repo", "create"]:
    bare = os.path.join(os.environ["FAKE_GH_REMOTES"], args[2] + ".git")
    os.makedirs(os.path.dirname(bare), exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True)
    print(f"https://github.example.test/{args[2]}")
else:
    print(f"fake gh: unsupported {args}", file=sys.stderr)
    sys.exit(2)
