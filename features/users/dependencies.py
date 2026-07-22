from . import config_api, runtime
from .sessions import client_manager


def configure_user_dependencies(**values: object) -> None:
    runtime.configure_runtime(**values)
    client_manager.config_manager = values["config_manager"]
    config_api.config_manager = values["config_manager"]
