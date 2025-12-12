#!/usr/bin/env python3
"""
Sync script for mirroring facebook/react PRs to greptileai/react-mirror.

This script:
1. Creates mirror PRs for new upstream PRs (with labels, draft status)
2. Updates mirror branches when upstream PRs are updated
3. Syncs PR metadata (title, body, labels, draft status) when upstream changes
4. Closes mirror PRs when upstream PRs are closed/merged

Note: PRs are for visibility only. Code sync happens via branch force-push.
When upstream PRs close/merge, mirror PRs are simply closed (not merged)
since the code is already in the mirror via branch sync.
"""

import json
import subprocess
import sys
import time
from typing import Dict, List, Optional, Set

UPSTREAM_REPO = "facebook/react"
FORK_REPO = "greptileai/react-mirror"


def run_cmd(cmd: List[str], capture: bool = True, check: bool = True) -> Optional[str]:
    """Run a command and return stdout."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            check=check
        )
        return result.stdout.strip() if capture else None
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {' '.join(cmd)}")
        if e.stderr:
            print(f"Error: {e.stderr}")
        if check:
            raise
        return None


def run_gh(args: List[str], check: bool = True) -> Optional[str]:
    """Run a gh CLI command."""
    return run_cmd(["gh"] + args, check=check)


def get_upstream_prs() -> List[Dict]:
    """Get all open PRs from upstream repo."""
    print("Fetching open PRs from upstream...")
    result = run_gh([
        "pr", "list",
        "--repo", UPSTREAM_REPO,
        "--state", "open",
        "--limit", "500",
        "--json", "number,title,baseRefName,headRefName,headRefOid,body,author,labels,isDraft"
    ])
    return json.loads(result) if result else []


def get_fork_prs() -> Dict[str, Dict]:
    """Get all open PRs from fork, indexed by head branch."""
    print("Fetching open PRs from fork...")
    result = run_gh([
        "pr", "list",
        "--repo", FORK_REPO,
        "--state", "open",
        "--limit", "1000",
        "--json", "number,title,headRefName,headRefOid,body,labels,isDraft,id"
    ])
    prs = json.loads(result) if result else []
    return {pr["headRefName"]: pr for pr in prs}


def get_branch_name(pr: Dict, all_prs: List[Dict]) -> str:
    """Get the branch name for a PR, handling duplicates."""
    head_ref = pr["headRefName"]
    # Count how many PRs have this same head ref name
    count = sum(1 for p in all_prs if p["headRefName"] == head_ref)
    if count > 1:
        return f"{head_ref}-{pr['number']}"
    return head_ref


def branch_exists_on_origin(branch: str) -> bool:
    """Check if a branch exists on origin."""
    result = run_cmd(
        ["git", "ls-remote", "--heads", "origin", branch],
        check=False
    )
    return bool(result and result.strip())


def ensure_base_branch_exists(base_ref: str) -> bool:
    """Ensure the base branch exists on origin, fetch from upstream if needed."""
    if branch_exists_on_origin(base_ref):
        return True

    # Try to fetch from upstream
    print(f"  Fetching missing base branch: {base_ref}")
    try:
        run_cmd(["git", "fetch", "upstream", f"{base_ref}:{base_ref}"])
        run_cmd(["git", "push", "origin", base_ref])
        return True
    except:
        print(f"  WARNING: Could not fetch base branch {base_ref}")
        return False


def get_label_names(pr: Dict) -> List[str]:
    """Extract label names from PR labels."""
    labels = pr.get("labels", [])
    return [label["name"] for label in labels]

# Do not tag author with @
def build_pr_body(pr_num: int, author: str, body: str) -> str:
    """Build the mirror PR body with upstream reference."""
    return f"""**Mirror of [{UPSTREAM_REPO}#{pr_num}](https://github.com/{UPSTREAM_REPO}/pull/{pr_num})**
**Original author:** {author} 

---

{body}"""


def mark_pr_ready(fork_pr_num: int) -> bool:
    """Mark a draft PR as ready for review."""
    try:
        run_gh([
            "pr", "ready", str(fork_pr_num),
            "--repo", FORK_REPO
        ])
        return True
    except:
        return False


def convert_pr_to_draft(pr_node_id: str) -> bool:
    """Convert a ready PR back to draft using GraphQL."""
    try:
        run_gh([
            "api", "graphql",
            "-f", f"query=mutation {{ convertPullRequestToDraft(input: {{pullRequestId: \"{pr_node_id}\"}}) {{ pullRequest {{ isDraft }} }} }}"
        ])
        return True
    except:
        return False


def sync_labels(fork_pr_num: int, upstream_labels: List[str], fork_labels: List[str]) -> None:
    """Sync labels between upstream and fork PRs."""
    upstream_set = set(upstream_labels)
    fork_set = set(fork_labels)

    # Labels to add
    to_add = upstream_set - fork_set
    if to_add:
        run_gh([
            "pr", "edit", str(fork_pr_num),
            "--repo", FORK_REPO,
            "--add-label", ",".join(to_add)
        ], check=False)  # Don't fail if labels don't exist on fork

    # Labels to remove
    to_remove = fork_set - upstream_set
    if to_remove:
        run_gh([
            "pr", "edit", str(fork_pr_num),
            "--repo", FORK_REPO,
            "--remove-label", ",".join(to_remove)
        ], check=False)


def update_pr_metadata(fork_pr_num: int, title: str, body: str, upstream_labels: List[str], fork_labels: List[str], is_draft: bool, fork_is_draft: bool, pr_node_id: str) -> bool:
    """Update PR title, body, labels, and draft status."""
    try:
        # Update title and body
        run_gh([
            "pr", "edit", str(fork_pr_num),
            "--repo", FORK_REPO,
            "--title", title,
            "--body", body
        ])

        # Sync labels (add new, remove old)
        sync_labels(fork_pr_num, upstream_labels, fork_labels)

        # Update draft status if changed
        if is_draft and not fork_is_draft:
            # Convert to draft
            print(f"    Converting PR #{fork_pr_num} to draft")
            convert_pr_to_draft(pr_node_id)
        elif not is_draft and fork_is_draft:
            # Mark as ready
            print(f"    Marking PR #{fork_pr_num} as ready")
            mark_pr_ready(fork_pr_num)

        return True
    except Exception as e:
        print(f"  Failed to update PR metadata: {e}")
        return False


def create_or_update_pr(pr: Dict, branch_name: str, fork_prs: Dict[str, Dict]) -> str:
    """
    Create a new PR or update existing one.
    Returns: 'created', 'updated', 'unchanged', or 'failed'
    """
    pr_num = pr["number"]
    title = pr["title"]
    base = pr["baseRefName"]
    body = pr.get("body") or ""
    author = pr["author"]["login"]
    upstream_sha = pr["headRefOid"]
    upstream_labels = get_label_names(pr)
    is_draft = pr.get("isDraft", False)

    # Build the expected mirror PR body
    expected_body = build_pr_body(pr_num, author, body)

    # Check if PR already exists on fork
    existing = fork_prs.get(branch_name)

    if existing:
        fork_pr_num = existing["number"]
        fork_sha = existing.get("headRefOid", "")
        fork_title = existing.get("title", "")
        fork_body = existing.get("body", "")
        fork_labels = get_label_names(existing)
        fork_is_draft = existing.get("isDraft", False)
        fork_node_id = existing.get("id", "")

        # Check if branch update needed
        branch_updated = False
        if fork_sha != upstream_sha:
            print(f"  [{pr_num}] Updating branch: {branch_name}")
            try:
                run_cmd(["git", "fetch", "upstream", f"pull/{pr_num}/head:{branch_name}", "--force"])
                run_cmd(["git", "push", "origin", branch_name, "--force"])
                branch_updated = True
            except Exception as e:
                print(f"  [{pr_num}] Failed to update branch: {e}")
                return "failed"

        # Check if metadata update needed (title, body, labels, or draft status differ)
        metadata_changed = (
            fork_title != title or
            fork_body != expected_body or
            set(fork_labels) != set(upstream_labels) or
            fork_is_draft != is_draft
        )

        if metadata_changed:
            print(f"  [{pr_num}] Updating metadata: {branch_name}")
            update_pr_metadata(fork_pr_num, title, expected_body, upstream_labels, fork_labels, is_draft, fork_is_draft, fork_node_id)

        if branch_updated or metadata_changed:
            return "updated"
        return "unchanged"

    # New PR - ensure base branch exists
    if not ensure_base_branch_exists(base):
        print(f"  [{pr_num}] Skipping - base branch {base} not available")
        return "failed"

    # Create new branch
    draft_label = " [DRAFT]" if is_draft else ""
    print(f"  [{pr_num}] Creating{draft_label}: {title[:50]}...")
    try:
        run_cmd(["git", "fetch", "upstream", f"pull/{pr_num}/head:{branch_name}"])
        run_cmd(["git", "push", "origin", branch_name])
    except Exception as e:
        print(f"  [{pr_num}] Failed to create branch: {e}")
        return "failed"

    # Create PR with labels and draft status
    try:
        create_args = [
            "pr", "create",
            "--repo", FORK_REPO,
            "--head", branch_name,
            "--base", base,
            "--title", title,
            "--body", expected_body
        ]

        # Add labels if present
        if upstream_labels:
            create_args.extend(["--label", ",".join(upstream_labels)])

        # Create as draft if upstream is draft
        if is_draft:
            create_args.append("--draft")

        result = run_gh(create_args)
        print(f"  [{pr_num}] Created: {result}")
        return "created"
    except Exception as e:
        print(f"  [{pr_num}] Failed to create PR: {e}")
        return "failed"


def close_stale_prs(upstream_branches: Set[str], fork_prs: Dict[str, Dict]) -> int:
    """
    Close PRs on fork that no longer exist on upstream.

    Note: We just close PRs instead of merging them because:
    - The code is already in the mirror via branch sync (force push)
    - Attempting to merge would fail or create duplicates
    - PRs are for visibility only, not for code integration
    """
    print("\n=== Closing stale PRs ===")
    closed = 0

    for branch_name, pr in fork_prs.items():
        if branch_name not in upstream_branches:
            pr_num = pr["number"]
            print(f"  Closing PR #{pr_num}: {branch_name}")
            try:
                run_gh([
                    "pr", "close", str(pr_num),
                    "--repo", FORK_REPO,
                    "--delete-branch",
                    "--comment", "Upstream PR was closed or merged. Code is synced via branch mirror."
                ], check=False)
                closed += 1
            except:
                print(f"  Failed to close PR #{pr_num}")

            time.sleep(0.3)  # Rate limiting

    return closed


def sync_prs():
    """Sync all PRs from upstream to fork."""
    print("\n=== Syncing PRs ===")

    # Get current state
    upstream_prs = get_upstream_prs()
    fork_prs = get_fork_prs()

    print(f"Found {len(upstream_prs)} open PRs on upstream")
    print(f"Found {len(fork_prs)} open PRs on fork")

    # Build set of expected branch names
    upstream_branches: Set[str] = set()

    # Counters
    created = 0
    updated = 0
    unchanged = 0
    failed = 0

    # Sort PRs by number (oldest first) to maintain consistent ordering
    upstream_prs_sorted = sorted(upstream_prs, key=lambda x: x["number"])

    # Process each upstream PR
    for pr in upstream_prs_sorted:
        branch_name = get_branch_name(pr, upstream_prs)
        upstream_branches.add(branch_name)

        result = create_or_update_pr(pr, branch_name, fork_prs)

        if result == "created":
            created += 1
        elif result == "updated":
            updated += 1
        elif result == "unchanged":
            unchanged += 1
        else:
            failed += 1

        # Small delay to avoid rate limiting
        time.sleep(0.3)

    # Close stale PRs (code is already synced via branches)
    closed = close_stale_prs(upstream_branches, fork_prs)

    print(f"\n=== PR Sync Summary ===")
    print(f"Created: {created}")
    print(f"Updated: {updated}")
    print(f"Unchanged: {unchanged}")
    print(f"Closed: {closed}")
    print(f"Failed: {failed}")

    return failed == 0


def main():
    print("=" * 60)
    print("React Mirror PR Sync")
    print("=" * 60)

    success = sync_prs()

    print("\n" + "=" * 60)
    print("Sync complete!" if success else "Sync completed with errors")
    print("=" * 60)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
