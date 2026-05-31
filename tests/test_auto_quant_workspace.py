import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_quant_workspace import workspace_paths


class WorkspacePathsTest(unittest.TestCase):
    def test_defaults_match_project_root_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "config.json").write_text("{}", encoding="utf-8")

            paths = workspace_paths(project)

            self.assertEqual(paths.workspace_dir, project.resolve())
            self.assertEqual(paths.user_data, (project / "user_data").resolve())
            self.assertEqual(paths.data_dir, (project / "user_data" / "data").resolve())
            self.assertEqual(
                paths.strategies_dir,
                (project / "user_data" / "strategies").resolve(),
            )
            self.assertEqual(paths.config, (project / "config.json").resolve())
            self.assertEqual(paths.results_tsv, (project / "results.tsv").resolve())

    def test_workspace_env_isolates_mutable_paths_and_allows_shared_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            workspace = root / "workspaces" / "lane-a"
            shared_data = root / "shared-data"
            project.mkdir()
            workspace.mkdir(parents=True)
            shared_data.mkdir()
            (project / "config.json").write_text("{}", encoding="utf-8")
            (workspace / "config.json").write_text("{}", encoding="utf-8")
            env = {
                "AUTO_QUANT_WORKSPACE": str(workspace),
                "AUTO_QUANT_DATA_DIR": str(shared_data),
            }

            with patch.dict(os.environ, env, clear=False):
                paths = workspace_paths(project)

            self.assertEqual(paths.workspace_dir, workspace.resolve())
            self.assertEqual(paths.user_data, (workspace / "user_data").resolve())
            self.assertEqual(paths.data_dir, shared_data.resolve())
            self.assertEqual(
                paths.strategies_dir,
                (workspace / "user_data" / "strategies").resolve(),
            )
            self.assertEqual(paths.config, (workspace / "config.json").resolve())
            self.assertEqual(paths.results_tsv, (workspace / "results.tsv").resolve())


if __name__ == "__main__":
    unittest.main()
