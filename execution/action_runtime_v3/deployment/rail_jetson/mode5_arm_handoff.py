"""Explicit handoff between the onboard arm action service and Mode 5 replay."""
import json
import sys
import time

from dds_authority import DdsAuthority, LOWCMD, arm_sdk_evidence, mode5_receiver
from inspect_robot_services import ARM_EXAMPLE, service_probe
from mode5_locked import ACTIVE_SERVICES, INACTIVE_SERVICES


def arm_service(rows):
    required = dict.fromkeys(ACTIVE_SERVICES, 0)
    required.update(dict.fromkeys(INACTIVE_SERVICES, 1))
    by_name = {}
    for row in rows:
        if isinstance(row, dict) and row.get('name') in (*required, ARM_EXAMPLE):
            if row['name'] in by_name:
                raise RuntimeError('Duplicate Robot ServiceList entry: ' + row['name'])
            if type(row.get('status')) is not int:
                raise RuntimeError('Invalid Robot ServiceList status: ' + row['name'])
            by_name[row['name']] = row
    for name, status in required.items():
        if by_name.get(name, {}).get('status') != status:
            raise RuntimeError('Require %s status=%d before Mode 5 operation' % (name, status))
    arm = by_name.get(ARM_EXAMPLE)
    if not arm or arm.get('status') not in (0, 1):
        raise RuntimeError('Unrecognized g1_arm_example state in Robot ServiceList')
    return arm


def require_service_released(rows):
    if arm_service(rows)['status'] != 1:
        raise RuntimeError('g1_arm_example is active; use --mode5-arm-service stop before Mode 5 replay')


def require_quiet_graph(node, state, *, stopping):
    reader = mode5_receiver(state)
    if node.count_publishers('/g1/arm_gateway/diagnostics'):
        raise RuntimeError('A gateway is still present; release output and exit it first')
    if state['messages'] or state['unknown_messages']:
        raise RuntimeError('Arm command traffic was observed; first stop the active task')
    writers = arm_sdk_evidence(state)['writers']
    candidate = (len(writers) == 1 and writers[0]['host'] == 'Unitree' and writers[0]['type'] == LOWCMD
                 and writers[0]['process'] in (ARM_EXAMPLE, 'python3'))
    if writers and not (stopping and candidate):
        raise RuntimeError('Unexpected /arm_sdk writer during handoff; resolve its ownership first')
    return reader


def handoff(root, *, enable):
    observer = DdsAuthority(root, mode5=True)
    try:
        with service_probe() as services:
            deadline = time.monotonic() + 6.
            while observer.snapshot()['discovery'].get('observed_seconds', 0.) < 4.:
                if time.monotonic() >= deadline or observer.process.poll() is not None:
                    raise RuntimeError('DDS observer did not report a complete handoff window')
                services.spin_once(services.node, timeout_sec=.1)

            def no_other_controller(*, stopping):
                return require_quiet_graph(services.node, observer.checked_state(4.), stopping=stopping)

            desired = 0 if enable else 1
            arm = arm_service(services.service_list())
            if arm['status'] != desired:
                if type(arm.get('protect')) is not int or arm['protect'] != 0:
                    raise RuntimeError('g1_arm_example is protected; no service change attempted')
                reader = no_other_controller(stopping=not enable)

                def before_send():
                    if no_other_controller(stopping=not enable) != reader:
                        raise RuntimeError('Onboard arm SDK reader changed; no service change attempted')

                services.switch_arm_example(enable=enable, before_send=before_send)
                deadline = time.monotonic() + 8.
                while arm_service(services.service_list())['status'] != desired:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Robot ServiceList did not confirm g1_arm_example status=%d' % desired)
                    time.sleep(.5)
            else:
                reader = no_other_controller(stopping=True)

            if enable:
                print('Robot ServiceList confirmed: g1_arm_example active (status=0).', flush=True)
                return 0

            deadline = time.monotonic() + 10.
            clear_since = None
            while time.monotonic() < deadline:
                state = observer.checked_state(4.)
                if require_quiet_graph(services.node, state, stopping=True) != reader:
                    raise RuntimeError('Onboard arm SDK reader changed during handoff')
                if state['writers'] or services.node.count_publishers('/arm_sdk'):
                    clear_since = None
                else:
                    clear_since = clear_since or time.monotonic()
                    if time.monotonic() - clear_since >= 1.:
                        observer.check()
                        require_service_released(services.service_list())
                        observer.check()
                        if no_other_controller(stopping=False) != reader or services.node.count_publishers('/arm_sdk'):
                            raise RuntimeError('Arm SDK graph changed while confirming the stopped service')
                        print('Mode 5 handoff clear: g1_arm_example stopped (status=1); no /arm_sdk writers.',
                              flush=True)
                        return 0
                services.spin_once(services.node, timeout_sec=.1)
            raise RuntimeError('g1_arm_example is stopped, but an /arm_sdk writer remains')
    except (RuntimeError, ValueError) as error:
        evidence = arm_sdk_evidence(observer.snapshot()['discovery'])
        print('ARM SDK DDS evidence: ' + json.dumps(evidence, sort_keys=True), flush=True)
        print('Mode 5 arm handoff blocked: ' + str(error), file=sys.stderr, flush=True)
        return 2
    finally:
        observer.close()
