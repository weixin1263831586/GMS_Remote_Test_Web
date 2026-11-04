"""Reproduce the wizard-to-launcher USB transition before swipe command 19."""
from types import SimpleNamespace

import pytest

from foundation import aoa_profile
from worker_agent import aoa_actions
from worker_agent.aoa_usb import bind_usb


class UsbGoneError(OSError):
    backend_error_code = -4

    def __init__(self):
        super().__init__(19, 'No such device')


class UsbFixture:
    def __init__(self, monkeypatch, transition='native', fail_report=0):
        self.now = 100.0
        self.current_step = 0
        self.events = []
        self.disposed = []
        self.transition = transition
        self.fail_report = fail_report
        self.validate = lambda: None
        self.devices = []
        self.current = self.make_device(5, accessory=True)
        self.original = self.current
        self.core = SimpleNamespace(USBError=UsbGoneError, find=lambda **_: [self.current] if self.current else [])
        self.util = SimpleNamespace(dispose_resources=self.disposed.append)
        monkeypatch.setattr(aoa_actions, '_usb', lambda: (self.core, self.util))
        monkeypatch.setattr(aoa_actions.time, 'monotonic', lambda: self.now)
        monkeypatch.setattr(aoa_actions.time, 'sleep', self.sleep)

    def make_device(self, address, *, accessory=False, serial='SERIAL', ports=(6,)):
        fixture = self

        class Device:
            bus = 1
            port_numbers = ports
            serial_number = serial
            idVendor = 0x18D1 if accessory else 0x2207  # noqa: N815
            idProduct = 0x2D00 if accessory else 0x0007  # noqa: N815

            def __init__(self):
                self.address = address
                self.alive = True
                self.touch_reports = []
                self.registered = []
                self.resets = 0
                self.report_count = 0

            def get_active_configuration(self):
                return SimpleNamespace(bNumInterfaces=0)

            def reset(self):
                if not self.alive:
                    raise UsbGoneError()
                self.resets += 1
                self.alive = False
                fixture.current = fixture.make_device(self.address + 1)

            def ctrl_transfer(self, kind, request, value, index, data, timeout):
                if not self.alive:
                    raise UsbGoneError()
                if kind == 0x80 and request == 6:
                    return bytes([18, 1] + [0] * 16)
                if request == 51:
                    return b'\x02\x00'
                if request == 53:
                    self.alive = False
                    fixture.current = fixture.make_device(self.address + 1, accessory=True)
                if request == 54:
                    self.registered.append(value)
                if request == 57 and value == aoa_profile.AOA_HID_TOUCH_SCREEN:
                    if fixture.current_step == 19:
                        self.report_count += 1
                        if self.report_count == fixture.fail_report:
                            self.alive = False
                            fixture.current = fixture.make_device(self.address + 1)
                            raise UsbGoneError()
                    self.touch_reports.append(bytes(data))
                return len(data) if isinstance(data, bytes) else 0

        result = Device()
        self.devices.append(result)
        return result

    def sleep(self, seconds):
        self.now += seconds

    def event(self, event):
        self.events.append(event)
        self.current_step = event['index']
        if event['phase'] == 'completed' and event['index'] == 18 and self.transition:
            self.original.alive = False
            if self.transition == 'unplugged':
                self.current = None
            else:
                self.current = self.make_device(
                    5 if self.transition == 'same-address' else 6,
                    accessory=self.transition in {'accessory', 'same-address'},
                    serial='replacement' if self.transition == 'replacement' else 'SERIAL',
                    ports=(7,) if self.transition == 'wrong-port' else (6,))

    def run(self, control_id=''):
        commands = ['click 180 520', 'sleep 2000'] * 9 + ['swipe 180 480 500 180 100']
        with bind_usb('1-6', self.validate):
            return aoa_actions.run_aoa_sequence('SERIAL', commands, sleep=self.sleep,
                                               on_event=self.event, control_id=control_id)


@pytest.mark.parametrize('transition', ['native', 'accessory', 'same-address'])
def test_step_19_rebuilds_before_swipe_without_replaying_completed_clicks(monkeypatch, transition):
    usb = UsbFixture(monkeypatch, transition)
    result = usb.run()
    assert result['success'] is True and result['commands_executed'] == 19
    assert result['usb_reconnections'] == 1
    assert usb.original in usb.disposed and usb.original.resets == 0
    assert len(usb.original.touch_reports) == 18  # Nine clicks, each down and up.
    active = next(device for device in usb.devices[1:] if device.registered)
    expected = [aoa_profile.touch_report(down, x, y)
                for down, x, y in aoa_profile.swipe_points(180, 480, 180, 100, 500)]
    assert active.touch_reports[:len(expected)] == expected
    assert active.registered == [3]
    assert active.resets == 1
    assert any(e['phase'] == 'reconnecting' and e['index'] == 19 for e in usb.events)


@pytest.mark.parametrize('transition,reason', [
    ('unplugged', 'reconnect timed out'), ('wrong-port', 'reconnect timed out'),
    ('replacement', 'identity changed'),
])
def test_rebuild_never_follows_another_port_or_replacement_device(monkeypatch, transition, reason):
    usb = UsbFixture(monkeypatch, transition)
    with pytest.raises(RuntimeError, match=reason):
        usb.run()
    assert len(usb.original.touch_reports) == 18
    assert not any(device.touch_reports for device in usb.devices[1:])


def test_disconnect_mid_swipe_reports_partial_input_and_never_retries(monkeypatch):
    usb = UsbFixture(monkeypatch, transition=None, fail_report=6)
    with pytest.raises(aoa_actions.AoaError, match=r'step 19/19, report 6, 5 reports sent'):
        usb.run()
    assert len(usb.original.touch_reports) == 18 + 5
    assert not any(e['phase'] == 'reconnecting' for e in usb.events)
    assert not any(device.touch_reports for device in usb.devices[1:])


def test_claim_revoked_during_reconnect_blocks_setup_and_usb_reset(monkeypatch):
    usb = UsbFixture(monkeypatch)
    revoked = False
    original = usb.event

    def event(value):
        nonlocal revoked
        original(value)
        if value['phase'] == 'reconnecting':
            revoked = True

    def validate():
        if revoked:
            raise RuntimeError('claim revoked')

    usb.event = event
    usb.validate = validate
    with pytest.raises(RuntimeError, match='claim revoked'):
        usb.run()
    assert usb.original.resets == 0
    assert not any(device.registered or device.touch_reports or device.resets for device in usb.devices[1:])


def test_cancel_during_usb_rebuild_is_honored_before_new_input(monkeypatch):
    from worker_agent import aoa_control

    usb = UsbFixture(monkeypatch)
    original = usb.event

    def event(value):
        original(value)
        if value['phase'] == 'reconnecting':
            aoa_control.request('cancel-rebuild', 'cancel')

    usb.event = event
    with pytest.raises(aoa_control.AoaPlaybackCancelledError):
        usb.run(control_id='cancel-rebuild')
    assert len(usb.original.touch_reports) == 18
    assert not any(report[0] == aoa_profile.AOA_TOUCH_DOWN
                   for device in usb.devices[1:] for report in device.touch_reports)
