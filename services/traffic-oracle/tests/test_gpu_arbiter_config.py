"""veer_models.yaml resolution for the GPU arbiter (WP-16).

The arbiter used to look for the models-owned config only under
``VVIP/models/configs`` — inside the retired-tree symlink — relative to the
working directory.  It now resolves ONE file: ``VEER_MODELS_CONFIG``, else
the tracked ``models-config/configs/veer_models.yaml`` (anchored at this
tree's VVIP root, then the working directory, then /opt/veer), else the
legacy symlink path — so the symlink can be deleted without changing what
is read.  The tracked file's planned pool (``memory_budget_gib.total_pool``)
now feeds the budget.  Everything runs on tmp files; one check reads this
repository's own tracked config.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs

from traffic_oracle.runtime import gpu_arbiter as ga
from traffic_oracle.runtime.gpu_arbiter import (
    LEGACY_MODELS_CONFIG_RELPATH,
    MODELS_CONFIG_ENV,
    MODELS_CONFIG_RELPATH,
    GpuArbiter,
    models_config_candidates,
    resolve_models_config,
)

TRACKED_BUDGET = "memory:\n  unified_budget_mb: 7777\n"
LEGACY_BUDGET = "memory:\n  unified_budget_mb: 1111\n"


def fake_meminfo(total_kb=127_533_976, available_kb=100_000_000):
    def reader(path="/proc/meminfo"):
        return {"MemTotal": total_kb, "MemAvailable": available_kb}

    return reader


def _put(root: Path, rel: Path, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ── resolution order ──────────────────────────────────────────────────────────


def test_explicit_env_pointer_wins(tmp_path):
    explicit = _put(tmp_path / "elsewhere", Path("my.yaml"), TRACKED_BUDGET)
    _put(tmp_path, MODELS_CONFIG_RELPATH, TRACKED_BUDGET)
    env = {MODELS_CONFIG_ENV: str(explicit)}
    assert resolve_models_config(env, roots=[tmp_path]) == explicit
    assert models_config_candidates(env, roots=[tmp_path])[0] == explicit


def test_missing_env_pointer_is_reported_and_the_tracked_home_is_next(tmp_path):
    tracked = _put(tmp_path, MODELS_CONFIG_RELPATH, TRACKED_BUDGET)
    env = {MODELS_CONFIG_ENV: str(tmp_path / "gone.yaml")}
    with capture_logs() as logs:
        assert resolve_models_config(env, roots=[tmp_path]) == tracked
    assert any(e["event"] == "gpu_arbiter.models_config_missing" for e in logs)


def test_tracked_home_beats_the_legacy_symlink_path(tmp_path):
    tracked = _put(tmp_path, MODELS_CONFIG_RELPATH, TRACKED_BUDGET)
    _put(tmp_path, LEGACY_MODELS_CONFIG_RELPATH, LEGACY_BUDGET)
    assert resolve_models_config({}, roots=[tmp_path]) == tracked
    arb = GpuArbiter(env={}, meminfo_reader=fake_meminfo(), models_config_roots=[tmp_path])
    assert arb.unified_budget_mb == 7777
    assert arb.budget_source == f"yaml:{tracked}" and arb.models_config_path == tracked


def test_every_tracked_root_is_tried_before_any_legacy_path(tmp_path):
    first, second = tmp_path / "vvip", tmp_path / "cwd"
    _put(first, LEGACY_MODELS_CONFIG_RELPATH, LEGACY_BUDGET)
    tracked = _put(second, MODELS_CONFIG_RELPATH, TRACKED_BUDGET)
    assert resolve_models_config({}, roots=[first, second]) == tracked
    order = models_config_candidates({}, roots=[first, second])
    assert order == [
        first / MODELS_CONFIG_RELPATH, second / MODELS_CONFIG_RELPATH,
        first / LEGACY_MODELS_CONFIG_RELPATH, second / LEGACY_MODELS_CONFIG_RELPATH,
    ]


def test_legacy_path_still_works_until_the_symlink_is_deleted(tmp_path):
    legacy = _put(tmp_path, LEGACY_MODELS_CONFIG_RELPATH, LEGACY_BUDGET)
    with capture_logs() as logs:
        assert resolve_models_config({}, roots=[tmp_path]) == legacy
    assert any(e["event"] == "gpu_arbiter.models_config_legacy_path" for e in logs)
    arb = GpuArbiter(env={}, meminfo_reader=fake_meminfo(), models_config_roots=[tmp_path])
    assert arb.unified_budget_mb == 1111


def test_no_config_anywhere_falls_back_to_meminfo(tmp_path):
    assert resolve_models_config({}, roots=[tmp_path]) is None
    arb = GpuArbiter(env={}, meminfo_reader=fake_meminfo(), models_config_roots=[tmp_path])
    assert arb.budget_source == "proc_meminfo" and arb.models_config_path is None
    assert arb.unified_budget_mb == 124_544


def test_a_stale_legacy_copy_is_never_read_behind_the_tracked_file(tmp_path):
    """The tracked file is the one in force even when it declares no
    budget: the chain moves on to /proc/meminfo, not to the legacy copy."""
    _put(tmp_path, MODELS_CONFIG_RELPATH, "services:\n  model_host: http://127.0.0.1:8083\n")
    _put(tmp_path, LEGACY_MODELS_CONFIG_RELPATH, LEGACY_BUDGET)
    arb = GpuArbiter(env={}, meminfo_reader=fake_meminfo(), models_config_roots=[tmp_path])
    assert arb.models_config_path == tmp_path / MODELS_CONFIG_RELPATH
    assert arb.budget_source == "proc_meminfo"


def test_env_budget_and_explicit_yaml_path_keep_their_precedence(tmp_path):
    _put(tmp_path, MODELS_CONFIG_RELPATH, TRACKED_BUDGET)
    arb = GpuArbiter(env={"VEER_UNIFIED_BUDGET_MB": "8888"}, meminfo_reader=fake_meminfo(),
                     models_config_roots=[tmp_path])
    assert (arb.unified_budget_mb, arb.budget_source) == (8888, "env")
    explicit = _put(tmp_path / "x", Path("pinned.yaml"), "memory:\n  unified_budget_mb: 4242\n")
    arb = GpuArbiter(env={}, yaml_path=explicit, meminfo_reader=fake_meminfo(),
                     models_config_roots=[tmp_path])
    assert arb.unified_budget_mb == 4242


def test_roots_are_this_trees_vvip_root_then_the_working_directory(tmp_path):
    roots = ga.config_roots(cwd=tmp_path)
    vvip = Path(ga.__file__).resolve().parents[5]
    assert roots[0] == vvip and (vvip / "services" / "traffic-oracle").is_dir()
    assert roots[1:] == (tmp_path / "VVIP", tmp_path, Path("/opt/veer"))


# ── the budget a config declares ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, expected",
    [
        ("memory:\n  unified_budget_mb: 7777\n", 7777),
        ("memory:\n  unified_budget_gib: 121.44\n", 124_354),
        ("unified_budget_mb: 6000\n", 6000),  # top-level form
        ("memory_budget_gib:\n  total_pool: 121.44\n", 124_354),
        # the explicit budget outranks the planned pool table
        ("memory:\n  unified_budget_mb: 9000\nmemory_budget_gib:\n  total_pool: 121.44\n", 9000),
        ("memory_budget_gib:\n  total_pool: true\n", None),   # bools are not sizes
        ("memory_budget_gib:\n  total_pool: -4\n", None),
        ("memory:\n  unified_budget_mb: .inf\n", None),
        ("- just\n- a list\n", None),
        (": not yaml : [", None),
    ],
)
def test_budget_from_yaml(tmp_path, text, expected):
    pytest.importorskip("yaml")
    path = _put(tmp_path, Path("veer_models.yaml"), text)
    assert ga._budget_from_yaml(path) == expected


def test_a_broken_config_never_takes_the_oracle_down(tmp_path):
    """yaml.YAMLError is not a ValueError: an unparseable tracked file used
    to escape the arbiter's constructor — i.e. the oracle's startup."""
    pytest.importorskip("yaml")
    _put(tmp_path, MODELS_CONFIG_RELPATH, "memory: [unclosed\n")
    with capture_logs() as logs:
        arb = GpuArbiter(env={}, meminfo_reader=fake_meminfo(), models_config_roots=[tmp_path])
    assert arb.budget_source == "proc_meminfo" and arb.unified_budget_mb == 124_544
    assert any(e["event"] == "gpu_arbiter.models_config_unreadable" for e in logs)


def test_this_repositorys_tracked_config_is_the_one_in_force():
    """In this tree the tracked file resolves (ahead of any legacy copy the
    retired-tree symlink may still hold) and its planned GB10 pool —
    121.44 GiB — is the budget."""
    pytest.importorskip("yaml")
    vvip = Path(ga.__file__).resolve().parents[5]
    tracked = vvip / MODELS_CONFIG_RELPATH
    if not tracked.is_file():
        pytest.skip("models-config/configs/veer_models.yaml not in this checkout")
    assert resolve_models_config({}) == tracked
    arb = GpuArbiter(env={}, meminfo_reader=fake_meminfo())
    assert arb.models_config_path == tracked
    assert arb.unified_budget_mb == int(121.44 * 1024)
