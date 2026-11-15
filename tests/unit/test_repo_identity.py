"""Repository names become filesystem paths, so they are validated first."""

from pathlib import Path

import pytest

from app.core.repo_identity import InvalidRepoName, checkout_dir, validate_repo_full_name


@pytest.mark.parametrize(
    "name",
    # GitHub owners are letters, digits and hyphens; repository names may also
    # contain dots and underscores.
    ["octocat/hello-world", "a/b", "Org-Name/repo.name_1", "user123/a-b_c.d"],
)
def test_accepts_real_repository_names(name):
    assert validate_repo_full_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "..",
        "../../etc",
        "owner/../../etc/passwd",
        "owner/..",
        "/absolute/path",
        "owner",
        "owner/repo/extra",
        "owner/re po",
        "-owner/repo",
        "owner/repo;rm -rf /",
        "owner/repo\nname",
    ],
)
def test_rejects_anything_that_could_escape_or_inject(name):
    with pytest.raises(InvalidRepoName):
        validate_repo_full_name(name)


def test_checkout_dir_is_always_a_direct_child_of_the_root(tmp_path):
    path = checkout_dir(tmp_path, "octocat/hello-world")

    assert path.parent == tmp_path.resolve()
    assert path.name == "octocat__hello-world"


def test_checkout_dir_rejects_traversal(tmp_path):
    with pytest.raises(InvalidRepoName):
        checkout_dir(tmp_path, "../escape")


def test_checkout_dir_of_a_lone_dotdot_cannot_reach_the_parent(tmp_path):
    # ".." has no slash, so a naive replace("/", "__") would leave it intact
    # and resolve to the parent directory — which a wipe-then-clone would delete.
    with pytest.raises(InvalidRepoName):
        checkout_dir(tmp_path, "..")


def test_checkout_dir_accepts_a_relative_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = checkout_dir(Path("./repos"), "octocat/hello")

    assert path == (tmp_path / "repos" / "octocat__hello").resolve()
