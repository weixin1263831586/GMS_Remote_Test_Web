import time

from features.devices.serial_console_subscribers import schedule_idle_stop


class _Service:
    def __init__(self):
        self.desired = False
        self.stopped = []

    def _desired(self, _port_key):
        return self.desired

    def _stop_worker(self, port_key):
        self.stopped.append(port_key)


def test_idle_stop_waits_for_refresh_reconnect():
    service = _Service()
    schedule_idle_stop(service, "port-1", delay=0.02)
    service.desired = True
    time.sleep(0.05)
    assert service.stopped == []


def test_idle_stop_closes_abandoned_console():
    service = _Service()
    schedule_idle_stop(service, "port-1", delay=0.02)
    time.sleep(0.05)
    assert service.stopped == ["port-1"]
