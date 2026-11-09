import os
import subprocess

import pytest

import reset_demo_copy
from reset_demo_copy import ResetError, check_repository, main


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def demo_repo(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text("node_modules/\n")
    (repo / "index.js").write_text("buggy\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")

    (repo / "index.js").write_text("fixed\n")
    (repo / "test").mkdir()
    (repo / "test" / "generated.test.js").write_text("test\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "pkg.js").write_text("dep\n")
    (repo / ".env").write_text("SECRET=1\n")
    (repo / "config.env").write_text("ATLAS_URI=x\n")
    return repo


def test_preview_changes_nothing(demo_repo, capsys):
    assert main([str(demo_repo)]) == 0
    output = capsys.readouterr().out
    assert "restore  index.js" in output
    assert "remove   test/" in output
    assert ".env" not in output
    assert (demo_repo / "index.js").read_text() == "fixed\n"
    assert (demo_repo / "test").exists()


def test_yes_resets_but_keeps_dependencies_and_env_files(demo_repo):
    assert main([str(demo_repo), "--yes"]) == 0
    assert (demo_repo / "index.js").read_text() == "buggy\n"
    assert not (demo_repo / "test").exists()
    assert (demo_repo / "node_modules" / "pkg.js").exists()
    assert (demo_repo / ".env").exists()
    assert (demo_repo / "config.env").exists()


def test_clean_repo_reports_already_clean(demo_repo, capsys):
    main([str(demo_repo), "--yes"])
    capsys.readouterr()
    assert main([str(demo_repo)]) == 0
    assert "Already clean." in capsys.readouterr().out


def test_rejects_folder_that_is_not_a_repository(tmp_path):
    with pytest.raises(ResetError, match="Not a git repository"):
        check_repository(str(tmp_path))


def test_rejects_subfolder_of_repository(demo_repo):
    with pytest.raises(ResetError, match="repository root"):
        check_repository(str(demo_repo / "test"))


def test_rejects_workflow_project(tmp_path):
    with pytest.raises(ResetError, match="workflow project"):
        check_repository(reset_demo_copy.WORKFLOW_PROJECT)
    with pytest.raises(ResetError, match="workflow project"):
        check_repository(os.path.join(reset_demo_copy.WORKFLOW_PROJECT, "tests"))


def test_missing_folder_fails_with_exit_code(tmp_path):
    assert main([str(tmp_path / "missing")]) == 1
