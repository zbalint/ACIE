import os
import subprocess

from acie.repo_id import (
    resolve_git_common_dir,
    resolve_index_db_path,
    resolve_repo_id,
    resolve_repo_state_dir,
)


def test_resolves_common_dir_for_a_plain_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    common_dir = resolve_git_common_dir(str(repo))

    assert common_dir == os.path.realpath(str(repo / ".git"))


def test_returns_none_for_a_directory_that_is_not_a_git_repo(tmp_path):
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()

    assert resolve_git_common_dir(str(plain_dir)) is None


def test_worktree_resolves_to_same_common_dir_as_main_checkout(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(
        ["git", "-C", str(main), "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"},
    )
    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", str(worktree)],
        check=True,
    )

    main_common = resolve_git_common_dir(str(main))
    worktree_common = resolve_git_common_dir(str(worktree))

    assert main_common == worktree_common


def test_repo_id_is_deterministic_for_the_same_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    first = resolve_repo_id(str(repo))
    second = resolve_repo_id(str(repo))

    assert first == second
    assert first is not None


def test_repo_id_returns_none_for_a_directory_that_is_not_a_git_repo(tmp_path):
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()

    assert resolve_repo_id(str(plain_dir)) is None


def test_repo_id_differs_across_distinct_repos(tmp_path):
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_a)], check=True)
    subprocess.run(["git", "init", "-q", str(repo_b)], check=True)

    assert resolve_repo_id(str(repo_a)) != resolve_repo_id(str(repo_b))


def test_worktree_shares_repo_id_with_main_checkout(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(
        ["git", "-C", str(main), "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"},
    )
    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", str(worktree)],
        check=True,
    )

    assert resolve_repo_id(str(main)) == resolve_repo_id(str(worktree))


def test_resolve_repo_state_dir_creates_and_returns_the_directory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    base = tmp_path / "acie-home"

    state_dir = resolve_repo_state_dir(str(repo), base_dir=str(base))

    repo_id = resolve_repo_id(str(repo))
    assert state_dir == str(base / "repos" / repo_id)
    assert os.path.isdir(state_dir)


def test_resolve_repo_state_dir_returns_none_for_a_directory_that_is_not_a_git_repo(tmp_path):
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    base = tmp_path / "acie-home"

    assert resolve_repo_state_dir(str(plain_dir), base_dir=str(base)) is None
    assert not base.exists()


def test_resolve_repo_state_dir_is_idempotent_across_calls(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    base = tmp_path / "acie-home"

    first = resolve_repo_state_dir(str(repo), base_dir=str(base))
    second = resolve_repo_state_dir(str(repo), base_dir=str(base))

    assert first == second
    assert os.path.isdir(second)


def test_resolve_index_db_path_points_inside_the_repos_state_dir(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    base = tmp_path / "acie-home"

    db_path = resolve_index_db_path(str(repo), base_dir=str(base))
    state_dir = resolve_repo_state_dir(str(repo), base_dir=str(base))

    assert db_path == os.path.join(state_dir, "index.sqlite")


def test_resolve_index_db_path_returns_none_for_a_directory_that_is_not_a_git_repo(tmp_path):
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    base = tmp_path / "acie-home"

    assert resolve_index_db_path(str(plain_dir), base_dir=str(base)) is None
