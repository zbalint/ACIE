import subprocess

from acie.daemon.repo_fingerprint import (
    compute_changed_relpaths,
    compute_repo_fingerprint,
    compute_repo_head_sha,
)



def test_compute_changed_relpaths_returns_tracked_and_untracked_paths_since_seed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    tracked = repo / "tracked.py"
    tracked.write_text("def old():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True)
    head_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    seed_fingerprint = compute_repo_fingerprint(str(repo))

    tracked.write_text("def new():\n    pass\n", encoding="utf-8")
    (repo / "untracked.py").write_text("def added():\n    pass\n", encoding="utf-8")

    assert compute_changed_relpaths(
        str(repo), previous_head_sha=head_sha, previous_fingerprint=seed_fingerprint
    ) == ["tracked.py", "untracked.py"]


def test_compute_changed_relpaths_returns_none_when_git_state_cannot_be_read(tmp_path):
    assert compute_changed_relpaths(str(tmp_path), previous_head_sha="missing") is None


def test_compute_repo_head_sha_returns_the_index_baseline(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "module.py").write_text("def target():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "module.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True)

    expected = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert compute_repo_head_sha(str(repo)) == expected


def test_compute_changed_relpaths_detects_a_seeded_untracked_file_removed_from_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "tracked.py").write_text("def tracked():\n    pass\n", encoding="utf-8")
    (repo / "untracked.py").write_text("def untracked():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True)
    head_sha = compute_repo_head_sha(str(repo))
    seed_fingerprint = compute_repo_fingerprint(str(repo))
    (repo / "untracked.py").unlink()

    assert compute_changed_relpaths(
        str(repo),
        previous_head_sha=head_sha,
        previous_fingerprint=seed_fingerprint,
        baseline_relpaths={"tracked.py", "untracked.py"},
    ) == ["untracked.py"]
