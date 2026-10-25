from setu_x.common.config import load_runner_settings
from setu_x.services.experiments.app import build_app
from setu_x.services.runner import run

if __name__ == "__main__":
    run(load_runner_settings, build_app)
