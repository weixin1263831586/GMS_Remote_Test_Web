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
        config_shape(config_manager.load_config(force_reload=True)),
    )
    write_json('ui_controls.json', ui_controls(ui_source_groups()))


if __name__ == '__main__':
    main()
