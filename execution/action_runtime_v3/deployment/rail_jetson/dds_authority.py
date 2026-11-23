"""Confirm the onboard arm SDK endpoint and reject competing publishers."""
import json
from pathlib import Path
import subprocess
import threading
import time


LOWCMD = 'unitree_hg::msg::dds_::LowCmd_'


def arm_sdk_evidence(state):
    peers = state.get('participants', {})
    messages = state.get('messages', {})
    readers = state.get('readers', ())

    def describe(endpoint):
        peer = peers.get(endpoint['participant'], {})
        return dict(endpoint, **{key: peer.get(key) or None for key in ('host', 'process', 'pid')})

    writers = []
    for writer in state.get('writers', ()):
        paired = [r['guid'] for r in readers if r['participant'] == writer['participant'] and r['type'] == LOWCMD]
        writers.append(dict(describe(writer), matching_readers=paired, frames=messages.get(writer['guid'], 0)))
    return dict(observed_seconds=state.get('observed_seconds'), writers=writers,
                readers=[describe(reader) for reader in readers],
                messages=messages, unknown_messages=state.get('unknown_messages'))


def native_endpoints(state):
    peers = state['participants']
    result = set()
    for writer in state['writers']:
        peer = peers.get(writer['participant'], {})
        receives = any(r['participant'] == writer['participant'] and r['type'] == LOWCMD
                       for r in state['readers'])
        if (peer.get('process') == 'g1_arm_example' and peer.get('host') == 'Unitree'
                and writer['type'] == LOWCMD and receives):
            result.add(writer['guid'])
    if len(result) != 1:
        raise RuntimeError('Expected exactly one identified onboard g1_arm_example endpoint')
    return result


def mode5_receiver(state):
    peers = state['participants']
    readers = []
    for reader in state['readers']:
        peer = peers.get(reader['participant'], {})
        if (peer.get('host') == 'Unitree' and peer.get('process') == 'humanoid'
                and reader['type'] == LOWCMD):
            readers.append(reader['guid'])
    if len(readers) != 1:
        raise RuntimeError('Expected exactly one onboard Unitree/humanoid /arm_sdk reader')
    return readers[0]


def validate(state, pinned, own=(), past_own=()):
    if native_endpoints(state) != pinned:
        raise RuntimeError('Onboard SDK identity changed; restart only after confirming ownership')
    validate_writers(state, pinned, own, past_own)


def validate_writers(state, idle_writers, own, past_own):
    allowed = idle_writers | set(own)
    foreign = {w['guid'] for w in state['writers']} - allowed
    if foreign:
        writers = (w for w in arm_sdk_evidence(state)['writers'] if w['guid'] in foreign)
        raise RuntimeError('Another /arm_sdk writer is present: ' + ', '.join(
            '%s/%s pid=%s guid=%s' % (w['host'] or '?', w['process'] or '?', w['pid'] or '?', w['guid'])
            for w in writers))
    if state.get('unknown_messages') or any(state['messages'].get(g, 0) for g in idle_writers):
        raise RuntimeError('Onboard/unknown publisher sent arm commands; competing output is blocked')
    historical = set(state['messages']) - allowed - set(past_own)
    if historical:
        raise RuntimeError('Another /arm_sdk writer sent commands during discovery: ' + ', '.join(sorted(historical)))


class DdsAuthority:
    def __init__(self, root, *, mode5=False):
        self.lock = threading.Lock()
        self.state, self.at, self.error, self.pinned = {}, 0., '', None
        self.mode5, self.pinned_reader = mode5, None
        self.past_own = set()
        self.closed = False
        self.process = subprocess.Popen([str(Path(root) / 'build/task_dds_observer')],
                                        stdout=subprocess.PIPE, text=True, bufsize=1)
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()

    def read(self):
        try:
            for line in self.process.stdout:
                state = json.loads(line)
                with self.lock:
                    self.state, self.at = state, time.monotonic()
        except Exception as error:
            with self.lock:
                self.error = str(error)

    def checked_state(self, min_seconds=3.):
        with self.lock:
            return self._checked_state_locked(min_seconds)

    def _checked_state_locked(self, min_seconds):
        if self.error or self.process.poll() is not None or time.monotonic() - self.at > 1.:
            raise RuntimeError('DDS source observer unavailable: ' + self.error)
        if self.state['observed_seconds'] < min_seconds:
            raise RuntimeError('DDS source observation warming up')
        return self.state

    def check(self, own=()):
        with self.lock:
            self._checked_state_locked(3.)
            if self.mode5:
                reader = mode5_receiver(self.state)
                if self.pinned_reader is not None and reader != self.pinned_reader:
                    raise RuntimeError('Mode 5 arm SDK reader changed; restart only after confirming ownership')
                # A receiver is never a permitted command source.
                pinned = set()
                validate_writers(self.state, pinned, own, self.past_own)
                self.pinned_reader = reader
            else:
                pinned = self.pinned if self.pinned is not None else native_endpoints(self.state)
                validate(self.state, pinned, own, self.past_own)
                self.pinned = pinned
            self.past_own.update(own)
            return set(pinned)

    def snapshot(self):
        with self.lock:
            return dict(pinned_native=sorted(self.pinned or ()), pinned_reader=self.pinned_reader,
                        observer_alive=self.process.poll() is None,
                        age_s=time.monotonic() - self.at if self.at else None, discovery=self.state)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3.)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.thread.join(timeout=2.)
        self.process.stdout.close()
