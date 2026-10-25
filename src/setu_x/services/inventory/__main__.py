from setu_x.common.config import load_service_settings
from setu_x.services.inventory.app import build_app
from setu_x.services.runner import run

if __name__ == "__main__":
    run(lambda: load_service_settings("inventory"), build_app)
