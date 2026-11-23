"""Wait for the local production API without submitting robot commands."""
import json
import time
from urllib.request import ProxyHandler, build_opener


def validate(state):
    if (state.get('backend') != 'recorded-native'
            or state.get('simulation') is not False):
        raise RuntimeError('Expected the real native execution backend')
    if (state.get('controllerRunning') is not False
            or state.get('native', {}).get('phase') != 'DETACHED'
            or state.get('initialized') is not False
            or state.get('activeTaskId') is not None):
        raise RuntimeError('Expected passive DETACHED startup without an active task')


def wait_ready(timeout=120.):
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    last_error = 'No response'
    while time.monotonic() < deadline:
        try:
            with opener.open('http://127.0.0.1:8795/health', timeout=3.) as response:
                state = json.load(response)
        except (OSError, ValueError) as error:
            last_error = str(error)
            time.sleep(.5)
            continue
        validate(state)
        print('ELN API ready: recorded-native, DETACHED, controllerRunning=false', flush=True)
        return
    raise TimeoutError('ELN API readiness timed out: ' + last_error)


if __name__ == '__main__':
    wait_ready()
