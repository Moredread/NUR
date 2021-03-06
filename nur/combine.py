import logging
import os
import shutil
import subprocess
from tempfile import TemporaryDirectory
from argparse import Namespace
from distutils.dir_util import copy_tree
from pathlib import Path
from typing import Dict, List, Optional

from .fileutils import chdir, write_json_file
from .manifest import Repo, load_manifest, update_lock_file
from .path import LOCK_PATH, MANIFEST_PATH, ROOT

logger = logging.getLogger(__name__)


def load_combined_repos(path: Path) -> Dict[str, Repo]:
    combined_manifest = load_manifest(
        path.joinpath("repos.json"), path.joinpath("repos.json.lock")
    )
    repos = {}
    for repo in combined_manifest.repos:
        repos[repo.name] = repo
    return repos


def repo_source(name: str) -> str:
    cmd = ["nix-build", str(ROOT), "--no-out-link", "-A", f"repo-sources.{name}"]
    out = subprocess.check_output(cmd)
    return out.strip().decode("utf-8")


def repo_changed() -> bool:
    diff_cmd = subprocess.Popen(["git", "diff", "--staged", "--exit-code"])
    return diff_cmd.wait() == 1


def commit_files(files: List[str], message: str) -> None:
    cmd = ["git", "add"]
    cmd.extend(files)
    subprocess.check_call(cmd)
    if repo_changed():
        subprocess.check_call(["git", "commit", "-m", message])


def commit_repo(repo: Repo, message: str, path: Path) -> Repo:
    repo_path = path.joinpath(repo.name).resolve()

    tmp: Optional[TemporaryDirectory] = TemporaryDirectory(prefix=str(repo_path.parent))
    assert tmp is not None

    try:
        copy_tree(repo_source(repo.name), tmp.name)
        shutil.rmtree(repo_path, ignore_errors=True)
        os.rename(tmp.name, repo_path)
        tmp = None
    finally:
        if tmp is not None:
            tmp.cleanup()

    with chdir(str(path)):
        commit_files([str(repo_path)], message)

    return repo


def update_combined_repo(
    combined_repo: Optional[Repo], repo: Repo, path: Path
) -> Optional[Repo]:
    if repo.locked_version is None:
        return None

    new_rev = repo.locked_version.rev
    if combined_repo is None:
        return commit_repo(repo, f"{repo.name}: init at {new_rev}", path)

    assert combined_repo.locked_version is not None
    old_rev = combined_repo.locked_version.rev

    if combined_repo.locked_version == repo.locked_version:
        return repo

    if new_rev != old_rev:
        message = f"{repo.name}: {old_rev} -> {new_rev}"
    else:
        message = f"{repo.name}: update"

    return commit_repo(repo, message, path)


def remove_repo(repo: Repo, path: Path) -> None:
    repo_path = path.joinpath("repos", repo.name)
    if repo_path.exists():
        shutil.rmtree(repo_path)
    commit_files([str(repo_path)], f"{repo.name}: remove")


def update_manifest(repos: List[Repo], path: Path) -> None:
    d = {}

    for repo in repos:
        d[repo.name] = repo.as_json()
    write_json_file(dict(repos=d), path)


def update_combined(path: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH, LOCK_PATH)

    combined_repos = load_combined_repos(path)

    repos_path = path.joinpath("repos")
    os.makedirs(repos_path, exist_ok=True)

    updated_repos = []

    for repo in manifest.repos:
        combined_repo = None
        if repo.name in combined_repos:
            combined_repo = combined_repos[repo.name]
            del combined_repos[repo.name]
        try:
            new_repo = update_combined_repo(combined_repo, repo, repos_path)
        except Exception:
            logger.exception(f"Failed to updated repository {repo.name}")
            continue

        if new_repo is not None:
            updated_repos.append(new_repo)

    for combined_repo in combined_repos.values():
        remove_repo(combined_repo, path)

    update_manifest(updated_repos, path.joinpath("repos.json"))

    update_lock_file(updated_repos, path.joinpath("repos.json.lock"))

    with chdir(path):
        commit_files(["repos.json", "repos.json.lock"], "update repos.json + lock")


def setup_combined() -> None:
    manifest_path = "repos.json"

    if not Path(".git").exists():
        cmd = ["git", "init", "."]
        subprocess.check_call(cmd)

    if not os.path.exists(manifest_path):
        write_json_file(dict(repos={}), manifest_path)

    manifest_lib = "lib"
    copy_tree(str(ROOT.joinpath("lib")), manifest_lib)
    default_nix = "default.nix"
    shutil.copy(ROOT.joinpath("default.nix"), default_nix)

    vcs_files = [manifest_path, manifest_lib, default_nix]

    commit_files(vcs_files, "update code")


def combine_command(args: Namespace) -> None:
    combined_path = Path(args.directory)

    with chdir(combined_path):
        setup_combined()
    update_combined(combined_path)
