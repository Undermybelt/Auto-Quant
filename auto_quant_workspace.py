"""Workspace path resolution for parallel Auto-Quant runs.

Default behavior intentionally matches the upstream repo: config, user_data,
strategies, data, and results.tsv live next to run.py. Set
AUTO_QUANT_WORKSPACE to move mutable run state into a per-agent workspace.
Set AUTO_QUANT_DATA_DIR when several workspaces should share read-only data.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

WORKSPACE_ENV = "AUTO_QUANT_WORKSPACE"
CONFIG_ENV = "AUTO_QUANT_CONFIG"
USER_DATA_ENV = "AUTO_QUANT_USER_DATA"
DATA_DIR_ENV = "AUTO_QUANT_DATA_DIR"
STRATEGIES_DIR_ENV = "AUTO_QUANT_STRATEGIES_DIR"
RESULTS_TSV_ENV = "AUTO_QUANT_RESULTS_TSV"


@dataclass(frozen=True)
class AutoQuantPaths:
    project_dir: Path
    workspace_dir: Path
    user_data: Path
    data_dir: Path
    strategies_dir: Path
    config: Path
    results_tsv: Path


def _resolve_path(raw: str, base: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _env_path(name: str, base: Path) -> Path | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return _resolve_path(raw, base)


def workspace_paths(
    project_dir: str | Path,
    *,
    strategies_subdir: str = "strategies",
    config_filename: str = "config.json",
) -> AutoQuantPaths:
    """Resolve all mutable Auto-Quant paths for one run.

    Relative env overrides are resolved inside AUTO_QUANT_WORKSPACE, except
    AUTO_QUANT_WORKSPACE itself, which follows the caller's current directory.
    If a workspace-specific config exists it wins; otherwise the project
    config keeps the upstream zero-config behavior.
    """
    project = Path(project_dir).expanduser().resolve()
    workspace = _env_path(WORKSPACE_ENV, Path.cwd()) or project
    user_data = _env_path(USER_DATA_ENV, workspace) or workspace / "user_data"
    data_dir = _env_path(DATA_DIR_ENV, workspace) or user_data / "data"
    strategies_dir = (
        _env_path(STRATEGIES_DIR_ENV, workspace) or user_data / strategies_subdir
    )
    config = _env_path(CONFIG_ENV, workspace)
    if config is None:
        workspace_config = workspace / config_filename
        config = workspace_config if workspace_config.exists() else project / config_filename
    results_tsv = _env_path(RESULTS_TSV_ENV, workspace) or workspace / "results.tsv"
    return AutoQuantPaths(
        project_dir=project,
        workspace_dir=workspace,
        user_data=user_data,
        data_dir=data_dir,
        strategies_dir=strategies_dir,
        config=config,
        results_tsv=results_tsv,
    )
