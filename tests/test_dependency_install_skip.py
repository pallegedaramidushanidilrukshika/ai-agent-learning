import os
import time

from bug_reproduction_agent import node_dependencies_current


def make_project(path, installed=True, lock=True):
    path.mkdir(parents=True, exist_ok=True)
    (path / "package.json").write_text("{}", encoding="utf-8")
    if lock:
        (path / "package-lock.json").write_text("{}", encoding="utf-8")
    if installed:
        (path / "node_modules").mkdir()
        (path / "node_modules" / ".package-lock.json").write_text("{}", encoding="utf-8")
    return path


def set_mtime(path, seconds_from_now):
    moment = time.time() + seconds_from_now
    os.utime(path, (moment, moment))


def test_installed_tree_matching_the_lock_file_is_reused(tmp_path):
    project = make_project(tmp_path / "app")
    set_mtime(project / "node_modules" / ".package-lock.json", 60)
    assert node_dependencies_current(str(project)) is True


def test_newer_lock_file_reinstalls(tmp_path):
    project = make_project(tmp_path / "app")
    set_mtime(project / "package-lock.json", 60)
    assert node_dependencies_current(str(project)) is False


def test_newer_package_json_reinstalls(tmp_path):
    project = make_project(tmp_path / "app")
    set_mtime(project / "node_modules" / ".package-lock.json", 60)
    set_mtime(project / "package.json", 120)
    assert node_dependencies_current(str(project)) is False


def test_missing_install_always_installs(tmp_path):
    assert node_dependencies_current(str(make_project(tmp_path / "app", installed=False))) is False


def test_missing_lock_file_always_installs(tmp_path):
    project = make_project(tmp_path / "app", lock=False)
    set_mtime(project / "node_modules" / ".package-lock.json", 60)
    assert node_dependencies_current(str(project)) is False
