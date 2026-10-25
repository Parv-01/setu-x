from setu_x.common.config import load_gateway_settings
from setu_x.services.gateway.app import build_app
from setu_x.services.runner import run

if __name__ == "__main__":
    run(load_gateway_settings, build_app)
