from app import app
from foundation.config import config_manager
from tests.contract.snapshot_tools import (
    config_shape,
    normalized_openapi,
    normalized_routes,
    ui_controls,
    ui_source_groups,
    write_json,
)


def main() -> None:
    write_json('routes.json', normalized_routes(app))
    write_json('openapi.json', normalized_openapi(app))
    write_json(
        'config_shape.json',
        # Runtime config contains deployment-specific/dynamic assignment keys.
        # Freeze only the versioned static configuration contract here; runtime
        # persistence has dedicated tests and must not make this snapshot vary
        # with the Controller's current devices or users.
        # The contract source MUST be the checked-in example —
        # generating from the deployment-local configs/config.json made the
        # snapshot depend on which machine ran generate_snapshots.py (the
        # example carries _comment/jadx_java_home keys the deployment copy
        # does not, so the frozen snapshot and the contract test disagreed).
        config_shape(_static_example_shape_source()),
    )
    write_json('ui_controls.json', ui_controls(ui_source_groups()))


def _static_example_shape_source() -> dict:
    """Load configs/examples/config.example.json the same way the contract test does.

    The test copies the example into an isolated project root and loads it
    through ConfigManager (placeholder expansion + AI-config validation),
    so the snapshot must run the identical path — never the deployment's
    own configs/config.json.
    """
    import shutil
    import tempfile
    from pathlib import Path

    from foundation.config import ConfigManager

    with tempfile.TemporaryDirectory() as directory:
        project_root = Path(directory)
        configs = project_root / "configs"
        configs.mkdir()
        (project_root / "foundation").mkdir()
        shutil.copy2(
            config_manager.config_fallback_path, configs / "config.json"
        )
        isolated = ConfigManager(project_root=project_root)
        return isolated._load_static_config()


if __name__ == '__main__':
    main()
