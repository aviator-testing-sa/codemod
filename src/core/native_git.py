from __future__ import annotations

import dataclasses
import datetime
import io
import random
import shutil
import subprocess
import tempfile
from typing import Any, Optional

import structlog

from core import pygithub
from main import app
from util import time_util

logger = structlog.stdlib.get_logger()
SCRIPT_TIMEOUT_SEC = 120


class NativeGit:
    token: str
    pull: pygithub.PullRequest | None
    tmp_dir: str

    def __init__(self, token: str, *, origin_repo: str, upstream_repo: str) -> None:
        """
        Initialize a new instance of NativeGit.
        :param token: The GitHub access token to use.
        :param origin_repo: The name of the origin repository (e.g. "github/github").
        :param upstream_repo: The name of the upstream repository (e.g., "github/github").
            This is useful when rebasing a PR that is created from a fork (the
            fork is the origin and the original repository is the upstream).
        """
        if not upstream_repo:
            upstream_repo = origin_repo

        self.token = token
        self.tmp_dir = tempfile.mkdtemp(prefix="mq")
        logger.info("created rebase directory: %s for %s", self.tmp_dir, origin_repo)
        self.run("git", "init")
        self.run("git", "config", "user.name", "aviator-bot")
        self.run("git", "config", "user.email", app.config["NOREPLY_EMAIL"])
        self.run("git", "config", "credential.useHttpPath", "true")
        self.run(
            "git",
            "config",
            "credential.helper",
            "cache --timeout=500 --socket=%s/.git/mq/socket" % self.tmp_dir,
        )

        stdin = "url=https://x-access-token:{}@github.com/{}.git".format(
            token, origin_repo
        )
        self.run("git", "credential", "approve", input=stdin)

        self.run("git", "remote", "add", "origin", "https://github.com/" + origin_repo)
        self.run(
            "git", "remote", "add", "upstream", "https://github.com/" + upstream_repo
        )
        self.run(
            "git",
            "remote",
            "set-url",
            "origin",
            f"https://x-access-token:{token}@github.com/{origin_repo}.git",
        )
        self.run(
            "git",
            "remote",
            "set-url",
            "upstream",
            f"https://x-access-token:{token}@github.com/{upstream_repo}.git",
        )

    def create_git_ref(self, new_branch_name: str, sha: str) -> None:
        # fetch the sha, and create a new ref out of it
        self.run("git", "fetch", "--quiet", "--depth=1", "origin", sha)
        self.run("git", "push", "origin", f"{sha}:refs/heads/{new_branch_name}")

    def rebase(self, pull: pygithub.PullRequest, base_branch: str) -> None:
        head_branch = pull.head.ref
        depth = int(pull.commits)
        self.fetch_references(base_branch, head_branch, depth)

        # this will throw error if rebase fails
        self.run("git", "rebase", "upstream/" + base_branch)
        self.run("git", "push", "origin", head_branch, "-f")

    def fetch_references(
        self, source_branch: str, target_branch: str, depth: int
    ) -> None:
        """
        The goal of this method is to fetch enough references from source and target branches,
        to be able to find a common "base". This is achieved using merge-base method.

        We start with fetching all the commits in the given PR (<code>depth</code>) to set the baseline.
        Our eventual goal it to get "merge-base" succeed. If it succeeds, that means we have found a
        common base reference that both source and target branch know of.

        Basically this is an extended version of this:
        https://stackoverflow.com/questions/27059840/how-to-fetch-enough-commits-to-do-a-merge-in-a-shallow-clone
        """
        try:
            self.run(
                "git", "fetch", "--quiet", "--depth=%d" % depth, "origin", target_branch
            )
        except subprocess.CalledProcessError as e:
            if e.returncode == 128:
                # if failed, fetch the entire head branch
                self.run("git", "fetch", "--quiet", "origin", target_branch)
            else:
                raise
        self.run(
            "git", "checkout", "-q", "-b", target_branch, "origin/" + target_branch
        )
        result = self.run("git", "log", "--format=%cI").stdout
        start_date = [dt for dt in result.split("\n") if dt.strip()][-1]
        success = False
        pastdate = time_util.now()
        for _ in range(10):
            try:
                self.run(
                    "git",
                    "fetch",
                    "-q",
                    "upstream",
                    source_branch,
                    "--shallow-since='%s'" % start_date,
                )
                success = True
                break
            except subprocess.CalledProcessError as e:
                if e.returncode == 128:
                    # run with a different date to avoid no fetch error
                    pastdate = pastdate - datetime.timedelta(days=random.randint(1, 7))
                    start_date = pastdate.strftime("%Y-%m-%dT%H:%M:%S")

        # If we still failed to fetch, just try to fetch everything from source branch. This is dangerous
        # as source branch very commonly can be main or master.
        if not success:
            self.run("git", "fetch", "-q", "upstream", source_branch)

        # This logic is the main part of fetching references. here we try to find a common ancestor of
        # the source and target branches. If we fail, we fetch more of the source branch and try again.
        for _ in range(10):
            self.run("git", "repack", "-d")
            try:
                self.run(
                    "git",
                    "merge-base",
                    "upstream/" + source_branch,
                    "origin/" + target_branch,
                )
                break
            except Exception as e:
                # fetch more and try again
                self.run(
                    "git", "fetch", "-q", "--deepen=100", "upstream", source_branch
                )

    def parse_commit(self, sha: str) -> CommitInfo:
        res = self.run("git", "show", f"--format={CommitInfo.GIT_SHOW_FORMAT}", sha)
        return CommitInfo.from_git_output(res.stdout)

    def run(self, *args: str, input: str | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                args=args,
                cwd=self.tmp_dir,
                timeout=SCRIPT_TIMEOUT_SEC,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=True,
                input=input,
            )
        except subprocess.CalledProcessError as e:
            logger.info("failed to run on %s error: %s", self.tmp_dir, str(e.output))
            raise
        finally:
            printable_args = [a if "token" not in a else "redacted" for a in args]
            logger.info(
                "finished processing on %s, command: %s",
                self.tmp_dir,
                str(printable_args),
            )

    def destroy(self) -> None:
        logger.info("cleaning up rebase %s", self.tmp_dir)
        try:
            self.run(
                "git",
                "credential-cache",
                "--socket=%s/.git/creds/socket" % self.tmp_dir,
                "exit",
            )
        except Exception as e:
            logger.error(
                "Failed to delete credential-cache", tmp_dir=self.tmp_dir, exc_info=e
            )
        shutil.rmtree(self.tmp_dir)

    def __enter__(self) -> NativeGit:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.destroy()


@dataclasses.dataclass
class CommitInfo:
    hash: str
    tree: str
    parents: list[str]
    title: str

    # See `git show` manpage for details.
    #   - %n = newline
    #   - %H = commit hash
    #   - %T = tree hash
    #   - %P = parent hashes (space separated)
    #   - %s = subject (first line of commit message)
    GIT_SHOW_FORMAT = "%H%n%T%n%P%n%s%n"

    @classmethod
    def from_git_output(cls, output: str) -> CommitInfo:
        """
        Parse the output of `git show` command.

        The output should be generated with
          git show --format=CommitInfo.GIT_SHOW_FORMAT
        for this method to work.
        """
        buf = io.StringIO(output)
        commit = cls(
            hash=buf.readline().strip(),
            tree=buf.readline().strip(),
            parents=buf.readline().strip().split(" "),
            title=buf.readline().rstrip("\n"),
        )
        assert commit.hash, "failed to parse commit: hash is empty"
        assert commit.tree, "failed to parse commit: tree is empty"
        # Technically, parents can be empty but only for the initial commit of
        # the repository, which shouldn't be an issue in practice.
        assert commit.parents, "failed to parse commit: parents is empty"
        # We don't assert the commit title here because it *technically* can be
        # empty.
        return commit
