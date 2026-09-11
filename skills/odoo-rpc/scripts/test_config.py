#!/usr/bin/env python3
"""Tests for profile discovery, validation and scaffolding."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import config
from config import (
    ConfigError,
    Profile,
    derive_connection,
    find_project_config,
    init_project_config,
    list_profiles,
    load_profile,
)


def _git_init(path: Path) -> None:
    """Create a real repository; discovery shells out to git."""
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


def _write(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text)
    path.chmod(mode)
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    _git_init(root)
    return root


@pytest.fixture(autouse=True)
def _no_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real ~/.config out of every test."""
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "absent.toml")


PROJECT_TOML = """
default_profile = "dev"

[profiles.dev]
url = "http://localhost:8069/"
db = "proj_dev"
login = "admin"
api_key = "secret-key"
"""


def test_finds_project_config_from_a_subdirectory(repo: Path) -> None:
    _write(repo / ".odoo-rpc.toml", PROJECT_TOML)
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    assert find_project_config(nested) == repo / ".odoo-rpc.toml"


def test_search_stops_at_the_git_root(repo: Path, tmp_path: Path) -> None:
    """A config above the repository must not leak into it."""
    _write(tmp_path / ".odoo-rpc.toml", PROJECT_TOML)
    assert find_project_config(repo) is None


def test_loads_profile_and_strips_trailing_slash(repo: Path) -> None:
    _write(repo / ".odoo-rpc.toml", PROJECT_TOML)
    profile = load_profile(start=repo)
    assert profile.url == "http://localhost:8069"
    assert profile.db == "proj_dev"
    assert profile.secret == "secret-key"
    assert profile.secret_kind == "api_key"


def test_group_readable_config_is_refused(repo: Path) -> None:
    """The file holds credentials, so loose permissions are an error."""
    _write(repo / ".odoo-rpc.toml", PROJECT_TOML, mode=0o644)
    with pytest.raises(ConfigError, match="readable by group or other"):
        load_profile(start=repo)


def test_project_config_wins_over_global(repo: Path, tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    glob = _write(tmp_path / "global.toml", """
[profiles.dev]
url = "http://global:8069"
""")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", glob)
    _write(repo / ".odoo-rpc.toml", PROJECT_TOML)
    assert load_profile("dev", start=repo).url == "http://localhost:8069"


def test_global_config_is_the_fallback(repo: Path, tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    glob = _write(tmp_path / "global.toml", """
[profiles.other]
url = "http://global:8069"
""")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", glob)
    _write(repo / ".odoo-rpc.toml", PROJECT_TOML)
    assert load_profile("other", start=repo).url == "http://global:8069"
    assert [n for n, _ in list_profiles(repo)] == ["dev", "other"]


def test_api_key_env_indirection(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(repo / ".odoo-rpc.toml", """
[profiles.dev]
url = "http://localhost:8069"
api_key_env = "TEST_ODOO_KEY"
""")
    monkeypatch.setenv("TEST_ODOO_KEY", "from-env")
    profile = load_profile("dev", start=repo)
    assert profile.secret == "from-env"
    assert profile.secret_kind == "api_key"


def test_api_key_env_unset_is_an_error(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(repo / ".odoo-rpc.toml", """
[profiles.dev]
url = "http://localhost:8069"
api_key_env = "TEST_ODOO_KEY"
""")
    monkeypatch.delenv("TEST_ODOO_KEY", raising=False)
    with pytest.raises(ConfigError, match="empty or unset"):
        load_profile("dev", start=repo)


def test_unknown_profile_lists_the_known_ones(repo: Path) -> None:
    _write(repo / ".odoo-rpc.toml", PROJECT_TOML)
    with pytest.raises(ConfigError, match="Known profiles: dev"):
        load_profile("nope", start=repo)


def test_invalid_transport_is_rejected(repo: Path) -> None:
    _write(repo / ".odoo-rpc.toml", """
[profiles.dev]
url = "http://localhost:8069"
transport = "carrier-pigeon"
""")
    with pytest.raises(ConfigError, match="expected one of"):
        load_profile("dev", start=repo)


def test_url_override_without_any_config(repo: Path) -> None:
    """--url alone must work: the one-off worktree path."""
    profile = load_profile(start=repo, url="http://localhost:9999/", db="adhoc")
    assert profile == Profile(name="<ad-hoc>", url="http://localhost:9999", db="adhoc")


def test_no_profile_and_no_url_explains_the_fix(repo: Path) -> None:
    with pytest.raises(ConfigError, match="odoo-rpc init"):
        load_profile(start=repo)


def test_overrides_replace_profile_fields(repo: Path) -> None:
    _write(repo / ".odoo-rpc.toml", PROJECT_TOML)
    profile = load_profile(start=repo, url="http://other:1", db="d2", login="bob",
                           api_key="k2")
    assert (profile.url, profile.db, profile.login, profile.secret) == (
        "http://other:1", "d2", "bob", "k2")


def test_redacted_never_exposes_the_secret(repo: Path) -> None:
    _write(repo / ".odoo-rpc.toml", PROJECT_TOML)
    assert "secret-key" not in str(load_profile(start=repo).redacted())


# --- derive_connection / init -------------------------------------------------

def _with_wt(repo: Path, conf_body: str, base_port: int = 8400) -> Path:
    conf = repo / "odoo.conf"
    _write(conf, conf_body)
    _write(repo / ".wt.toml", f"""
[odoo]
base_port = {base_port}
conf = "{conf}"
""")
    return conf


def test_wt_toml_permissions_are_not_policed(repo: Path) -> None:
    """.wt.toml belongs to wt, ships at 0664 everywhere, and holds no API keys."""
    _with_wt(repo, "[options]\nhttp_port = 8569\ndb_name = proj_test\n")
    (repo / ".wt.toml").chmod(0o664)
    url, db, _why = derive_connection(repo)
    assert (url, db) == ("http://localhost:8569", "proj_test")


def test_our_own_config_permissions_are_still_policed(repo: Path) -> None:
    """The distinction matters: .odoo-rpc.toml does hold the API key."""
    _write(repo / ".odoo-rpc.toml", PROJECT_TOML, mode=0o664)
    with pytest.raises(ConfigError, match="readable by group or other"):
        load_profile(start=repo)


def test_derive_reads_port_and_db_from_odoo_conf(repo: Path) -> None:
    _with_wt(repo, "[options]\nhttp_port = 8569\ndb_name = cft_delivers_test\n")
    url, db, why = derive_connection(repo)
    assert (url, db) == ("http://localhost:8569", "cft_delivers_test")
    assert "main checkout" in why


def test_derive_fails_loudly_without_wt_toml(repo: Path) -> None:
    """Never default to a port: wt's silent fallback hits someone else's DB."""
    with pytest.raises(ConfigError, match="not found"):
        derive_connection(repo)


def test_derive_fails_when_odoo_conf_is_missing(repo: Path) -> None:
    _write(repo / ".wt.toml", '[odoo]\nconf = "does-not-exist.conf"\n')
    with pytest.raises(ConfigError, match="does not exist"):
        derive_connection(repo)


def test_derive_fails_when_conf_has_no_port(repo: Path) -> None:
    _with_wt(repo, "[options]\ndb_name = only_db\n")
    with pytest.raises(ConfigError, match="no http_port"):
        derive_connection(repo)


def test_init_writes_a_loadable_profile_at_0600(repo: Path) -> None:
    _with_wt(repo, "[options]\nhttp_port = 8569\ndb_name = proj_test\n")
    target, _ = init_project_config(repo)
    assert target.stat().st_mode & 0o077 == 0
    profile = load_profile(start=repo)
    assert (profile.url, profile.db) == ("http://localhost:8569", "proj_test")


def test_init_excludes_the_file_from_git(repo: Path) -> None:
    _with_wt(repo, "[options]\nhttp_port = 8569\ndb_name = proj_test\n")
    init_project_config(repo)
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert "/.odoo-rpc.toml" in exclude


def test_init_refuses_to_clobber_without_force(repo: Path) -> None:
    _with_wt(repo, "[options]\nhttp_port = 8569\ndb_name = proj_test\n")
    init_project_config(repo)
    with pytest.raises(ConfigError, match="already exists"):
        init_project_config(repo)
    init_project_config(repo, force=True)


def test_init_outside_a_repository_is_an_error(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ConfigError, match="not inside a git repository"):
        init_project_config(plain)


def test_falls_back_to_a_sibling_odoo_conf_for_the_port(repo: Path) -> None:
    """.wt.toml usually names the worktree conf, which carries no http_port."""
    run_dir = repo / "run"
    run_dir.mkdir()
    _write(run_dir / "odoo-wt.conf", "[options]\ndb_user = odoo\n")
    _write(run_dir / "odoo.conf", "[options]\nhttp_port = 8369\ndb_name = ctc_test\n")
    _write(repo / ".wt.toml", f'[odoo]\nconf = "{run_dir / "odoo-wt.conf"}"\n')
    url, db, why = derive_connection(repo)
    assert (url, db) == ("http://localhost:8369", "ctc_test")
    assert "odoo.conf" in why


def test_no_port_in_either_conf_names_both_paths(repo: Path) -> None:
    run_dir = repo / "run"
    run_dir.mkdir()
    _write(run_dir / "odoo-wt.conf", "[options]\ndb_user = odoo\n")
    _write(run_dir / "odoo.conf", "[options]\ndb_user = odoo\n")
    _write(repo / ".wt.toml", f'[odoo]\nconf = "{run_dir / "odoo-wt.conf"}"\n')
    with pytest.raises(ConfigError, match="odoo-wt.conf or .*odoo.conf"):
        derive_connection(repo)


def test_worktree_port_is_base_port_plus_position(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """wt assigns base_port + the worktree's position in `git worktree list`."""
    monkeypatch.setattr(config, "_worktree_index", lambda repo, main: 2)
    monkeypatch.setattr(config, "_run_git", lambda args, cwd: "feature/x")
    url, db, why = config._derive_worktree(
        tmp_path / "feature_x", tmp_path / "proj", {"base_port": 8400})
    assert url == "http://localhost:8402"
    assert db == "dev_proj_feature_x"
    assert "position 2" in why


def test_worktree_db_includes_the_client_when_set(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tree = tmp_path / "feature_x"
    tree.mkdir()
    (tree / ".wt-client").write_text("internal-crm\n")
    monkeypatch.setattr(config, "_worktree_index", lambda repo, main: 1)
    monkeypatch.setattr(config, "_run_git", lambda args, cwd: "feature/y")
    _url, db, _why = config._derive_worktree(
        tree, tmp_path / "proj", {"base_port": 8400},
        {"internal-crm": {"base_port": 8471}})
    assert db == "dev_proj_internal_crm_feature_y"


def test_worktree_without_base_port_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="base_port"):
        config._derive_worktree(tmp_path / "wt", tmp_path / "proj", {})
