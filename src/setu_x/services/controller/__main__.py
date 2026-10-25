from setu_x.common.config import load_controller_settings
from setu_x.services.controller.app import build_app
from setu_x.services.runner import run

if __name__ == "__main__":
    run(load_controller_settings, build_app)
