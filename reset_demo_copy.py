"""Reset a demo project copy back to its committed (buggy) state.

Usage:
    python reset_demo_copy.py <repo> [<repo> ...]         # preview only
    python reset_demo_copy.py <repo> [<repo> ...] --yes   # apply

Only use this on demo copies made for workflow runs. It:
- restores tracked files changed by an AI fix (git checkout -- .)
- removes new untracked files and folders, such as generated tests (git clean -fd)

It keeps commits, branches, ignored files (node_modules) and environment
files (.env, config.env).
"""

import argparse
import os
import subprocess
import sys

WORKFLOW_PROJECT = os.path.dirname(os.path.abspath(__file__))
KEEP_PATTERNS = [".env", ".env.*", "config.env"]


class ResetError(Exception):
    pass


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, check=False,
    )


def same_path(left, right):
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def is_inside(path, parent):
    path = os.path.normcase(os.path.realpath(path))
    parent = os.path.normcase(os.path.realpath(parent))
    return path == parent or path.startswith(parent + os.sep)


def check_repository(repo):
    repo = os.path.abspath(repo)
    if not os.path.isdir(repo):
        raise ResetError(f"Not a folder: {repo}")
    if is_inside(repo, WORKFLOW_PROJECT):
        raise ResetError(f"Refusing to reset the workflow project itself: {repo}")
    top = git(repo, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise ResetError(f"Not a git repository: {repo}")
    if not same_path(top.stdout.strip(), repo):
        raise ResetError(f"Give the repository root, not a subfolder: {repo}")
    return repo


def clean_args():
    args = []
    for pattern in KEEP_PATTERNS:
        args += ["-e", pattern]
    return args


def planned_changes(repo):
    changed = git(repo, "diff", "--name-only", "HEAD").stdout.split()
    removed = [
        line.removeprefix("Would remove ").strip()
        for line in git(repo, "clean", "-nd", *clean_args()).stdout.splitlines()
        if line.startswith("Would remove ")
    ]
    return changed, removed


def reset_repository(repo):
    for args in (["checkout", "--", "."], ["clean", "-fd", *clean_args()]):
        result = git(repo, *args)
        if result.returncode != 0:
            raise ResetError(f"git {args[0]} failed in {repo}: {result.stderr.strip()}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reset demo project copies to their committed state.")
    parser.add_argument("repos", nargs="+", help="demo copy folders (repository roots)")
    parser.add_argument("--yes", action="store_true", help="apply the reset (default: preview only)")
    options = parser.parse_args(argv)

    failed = False
    for raw in options.repos:
        try:
            repo = check_repository(raw)
            changed, removed = planned_changes(repo)
            print(f"== {repo}")
            if not changed and not removed:
                print("   Already clean.")
                continue
            for name in changed:
                print(f"   restore  {name}")
            for name in removed:
                print(f"   remove   {name}")
            if options.yes:
                reset_repository(repo)
                print("   Reset done.")
            else:
                print("   Preview only. Add --yes to apply.")
        except ResetError as error:
            failed = True
            print(f"!! {error}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
