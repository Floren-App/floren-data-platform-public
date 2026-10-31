"""Server-side git: only the control plane talks to git hosts. Executions get source archives from it.

Each registered repository has a bare cache at <git_cache_dir>/<repo id>.git; branches are
fetched into it on demand. Credentials (from OpenBao) go to git as an HTTP header, never in
URLs, argv or error messages.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

_SHA = re.compile(r"^[0-9a-f]{40}$")
_BRANCH = re.compile(r"^[\w][\w./-]{0,200}$")
TIMEOUT = 120


class GitError(Exception):
    pass


@dataclass
class Commit:
    sha: str
    subject: str
    author: str
    date: str


def valid_branch(branch: str) -> bool:
    return bool(_BRANCH.match(branch)) and ".." not in branch and not branch.endswith((".lock", "/"))


_REPO_LOCKS: Dict[tuple, "threading.Lock"] = {}
_LOCKS_GUARD = threading.Lock()


class GitClient:
    def __init__(self, cache_dir: str, allow_file_urls: bool = False):
        self.cache_dir = cache_dir
        self.allow_file_urls = allow_file_urls
        os.makedirs(cache_dir, exist_ok=True)

    def check_url(self, url: str) -> None:
        if url.startswith(("https://", "http://")):
            return
        if url.startswith("file://") and self.allow_file_urls:
            return
        raise GitError("repository URL must start with https:// (or http:// for an internal git server)")

    def _git(self, args: List[str], secret: Optional[Dict[str, str]] = None, cwd: Optional[str] = None,
             binary: bool = False):
        cmd = ["git", "-c", "credential.helper=", "-c", "core.askPass=true"]
        if secret and secret.get("token"):
            user = secret.get("username") or "x-access-token"
            basic = base64.b64encode(f"{user}:{secret['token']}".encode()).decode()
            cmd += ["-c", f"http.extraHeader=Authorization: Basic {basic}"]
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}
        try:
            res = subprocess.run(cmd + args, cwd=cwd, env=env, capture_output=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            raise GitError("git timed out") from None
        if res.returncode != 0:
            err = res.stderr.decode(errors="replace").strip().splitlines()
            msg = next((line for line in reversed(err) if line.startswith(("fatal", "error", "remote"))), err[-1] if err else "git failed")
            raise GitError(re.sub(r"Basic [A-Za-z0-9+/=]+", "Basic ***", msg))
        return res.stdout if binary else res.stdout.decode(errors="replace")

    def branches(self, url: str, secret: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """branch name -> head sha, straight from the remote."""
        self.check_url(url)
        out = self._git(["ls-remote", "--heads", url], secret)
        heads = {}
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/"):
                heads[ref[len("refs/heads/"):]] = sha
        return heads

    def head(self, url: str, branch: str, secret: Optional[Dict[str, str]] = None) -> str:
        heads = self.branches(url, secret)
        if branch not in heads:
            raise GitError(f"branch {branch!r} not found")
        return heads[branch]

    def _lock(self, repo_id: str) -> threading.Lock:
        with _LOCKS_GUARD:
            return _REPO_LOCKS.setdefault((self.cache_dir, repo_id), threading.Lock())

    def _cache(self, repo_id: str) -> str:
        path = os.path.join(self.cache_dir, f"{repo_id}.git")
        if not os.path.isdir(path):
            with self._lock(repo_id):
                if not os.path.isdir(path):
                    res = subprocess.run(["git", "init", "--bare", "-q", path], capture_output=True)
                    if res.returncode != 0:
                        raise GitError("could not prepare the repository cache")
        return path

    def fetch(self, repo_id: str, url: str, branch: str, secret: Optional[Dict[str, str]] = None) -> str:
        """Fetches a branch into the cache; returns its head sha.

        Fetches into one cache are serialized: concurrent deploys from the same repository
        (e.g. infrastructure-as-code applying several at once) would otherwise race on its refs."""
        self.check_url(url)
        if not valid_branch(branch):
            raise GitError("invalid branch name")
        cache = self._cache(repo_id)
        ref = f"refs/heads/{branch}"
        with self._lock(repo_id):
            self._git(["fetch", "--prune", "--no-tags", url, f"+{ref}:{ref}"], secret, cwd=cache)
            return self._git(["rev-parse", ref], cwd=cache).strip()

    def _sha(self, sha: str) -> str:
        if not _SHA.match(sha):
            raise GitError("invalid commit")
        return sha

    def commit(self, repo_id: str, sha: str) -> Commit:
        out = self._git(["log", "-1", "--format=%H%x00%s%x00%an%x00%cI", self._sha(sha)], cwd=self._cache(repo_id))
        full, subject, author, date = out.rstrip("\n").split("\x00")
        return Commit(full, subject, author, date)

    def log(self, repo_id: str, ref: str, limit: int = 30) -> List[Commit]:
        """The newest commits reachable from a fetched commit or branch, newest first (from the cache, no network)."""
        if not _SHA.match(ref) and not (ref.startswith("refs/heads/") and valid_branch(ref[len("refs/heads/"):])):
            raise GitError("invalid commit")
        out = self._git(["log", f"-{max(1, min(limit, 200))}", "--first-parent", "--format=%H%x00%s%x00%an%x00%cI", ref, "--"],
                        cwd=self._cache(repo_id))
        return [Commit(*line.split("\x00")) for line in out.splitlines() if line.count("\x00") == 3]

    def read_file(self, repo_id: str, sha: str, path: str) -> Optional[str]:
        try:
            return self._git(["show", f"{self._sha(sha)}:{path}"], cwd=self._cache(repo_id))
        except GitError:
            return None

    def archive(self, repo_id: str, sha: str) -> bytes:
        return self._git(["archive", "--format=tar.gz", self._sha(sha)], cwd=self._cache(repo_id), binary=True)
