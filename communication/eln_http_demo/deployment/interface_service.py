"""ELN service entry point; explicit deployment config selects the real relay."""

import argparse
from dataclasses import replace
import importlib.util
import json
import math
from pathlib import Path

import uvicorn

from robot_gateway.app import create_app
from robot_gateway.config import Settings
from deployment.request_history import RequestHistory


def create_interface_app(settings=None):
    settings = settings or Settings.from_env()
    settings = replace(settings, backend="mock", simulation=True)
    if not math.isfinite(settings.mock_step_seconds) or settings.mock_step_seconds <= 0:
        raise ValueError("step seconds must be finite and positive")
    app = create_app(settings)
    app.title = "ELN Robot Interface Simulator V1.5"
    app.description = "HTTP task acceptance and status callbacks only. No robot hardware operations."
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--step-seconds", type=float, default=0.5)
    parser.add_argument("--database", default=str(
        Path(__file__).resolve().parents[1] / "data/interface-v1.5.sqlite3"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3] / 'execution/action_runtime_v3'
    config = root / 'config/interface_relay.json'
    if config.exists():
        settings = json.loads(config.read_text())
        require_client_api_key = settings.pop('require_client_api_key', True)
        if not isinstance(require_client_api_key, bool):
            raise RuntimeError('require_client_api_key must be a boolean')
        if settings != {'backend': 'recorded-native-relay', 'upstream': 'http://127.0.0.1:8795'}:
            raise RuntimeError('Invalid real relay configuration; refusing simulator fallback')
        spec = importlib.util.spec_from_file_location('jetson_http_relay', root / 'deployment/rail_jetson/http_relay.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        upstream_api_key = None
        if not require_client_api_key:
            upstream_api_key = (root / 'config/api.key').read_text().strip()
            if len(upstream_api_key) < 32:
                raise RuntimeError('A valid internal API key is required for the real relay')
        history = RequestHistory(root / 'data/http_requests.sqlite3', recover=True)
        app = module.create_app(settings['upstream'], upstream_api_key=upstream_api_key, history=history)
        print('REAL ROBOT RELAY on 8090: no simulated execution, no automatic retries', flush=True)
        print('ELN client API key required: %s' % require_client_api_key, flush=True)
    else:
        settings = Settings.from_env()
        settings.database = args.database
        settings.mock_step_seconds = args.step_seconds
        app = create_interface_app(settings)
    uvicorn.run(app, host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
