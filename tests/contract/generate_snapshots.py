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
        config_shape(config_manager._load_static_config()),
    )
    write_json('ui_controls.json', ui_controls(ui_source_groups()))


if __name__ == '__main__':
    main()
