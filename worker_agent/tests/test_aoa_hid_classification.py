"""Validate touchscreen descriptor semantics and stable, minimal HID registration."""
import pytest

from foundation import aoa_profile
from worker_agent import aoa_actions
from worker_agent.tests.test_aoa_actions import _stall_usb


def input_fields(descriptor):
    """Decode the HID short items used here, including field offsets and collections."""
    global_state = {}
    usages, collections, fields = [], [], []
    offset = cursor = 0
    while cursor < len(descriptor):
        prefix = descriptor[cursor]
        size = (0, 1, 2, 4)[prefix & 3]
        kind, tag = (prefix >> 2) & 3, prefix >> 4
        value = int.from_bytes(descriptor[cursor + 1:cursor + 1 + size], 'little')
        cursor += 1 + size
        if kind == 1:
            global_state[tag] = value
        elif kind == 2 and tag == 0:
            usages.append((global_state[0], value))
        elif kind == 0:
            if tag == 10:
                collections.append((value, usages[-1]))
            elif tag == 12:
                collections.pop()
            elif tag == 8:
                width, count = global_state[7], global_state[9]
                for index in range(count):
                    fields.append({'usage': usages[min(index, len(usages) - 1)],
                                   'offset': offset, 'width': width, 'flags': value,
                                   'maximum': global_state[2], 'collections': tuple(collections)})
                    offset += width
            usages = []
    assert not collections
    return fields, offset


def test_touch_descriptor_declares_direct_single_contact_and_preserves_wire_format():
    fields, bits = input_fields(aoa_profile.TOUCH_SCREEN_HID_DESCRIPTOR)
    assert bits == 40  # Preserve existing five-byte reports and profile coordinates.
    assert {f['collections'][0] for f in fields} == {(1, (0x0D, 0x04))}
    assert {f['collections'][1] for f in fields} == {(2, (0x0D, 0x22))}
    by_usage = {f['usage']: f for f in fields}
    # Contact Identifier selects Linux hid-multitouch, whose touchscreen
    # application is direct input; Application collection alone is insufficient.
    contact = by_usage[(0x0D, 0x51)]
    assert contact['offset'] == 2 and contact['width'] == 6 and contact['flags'] == 2
    assert by_usage[(0x01, 0x30)]['maximum'] == 360
    assert by_usage[(0x01, 0x31)]['maximum'] == 640
    assert not any(f['flags'] & 4 for f in fields)  # No relative pointer axes.
    for down in (False, True):
        report = aoa_profile.touch_report(down, 180, 450)
        raw = int.from_bytes(report, 'little')
        values = {f['usage']: (raw >> f['offset']) & ((1 << f['width']) - 1) for f in fields}
        assert values == {(0x0D, 0x32): int(down), (0x0D, 0x42): int(down),
                          (0x0D, 0x51): 0, (0x01, 0x30): 180, (0x01, 0x31): 450}


@pytest.mark.parametrize('commands,channels', [
    (['click 180 450', 'sleep 100'], [3]),
    (['click 180 450', 'back', 'swipe 180 500 500 180 100'], [3, 2]),
    (['key TAB ENTER', 'write test'], [1]),
    (['wake'], [2]),
    (['sleep 100'], []),
])
def test_only_required_channels_are_registered_before_input(monkeypatch, commands, channels):
    state = {'registered': [], 'events': []}
    _stall_usb(monkeypatch, state, stall_channels=set())
    observed = []
    result = aoa_actions.run_aoa_sequence(
        'partial-hid-device', commands, sleep=lambda _: None,
        on_event=lambda event: observed.append(list(state['registered'])))
    assert result['commands_executed'] == len(commands)
    assert state['registered'] == channels
    assert all(value == channels for value in observed)  # No device added at step 53.


def test_old_pointer_calibration_is_rejected_before_record_input():
    with pytest.raises(ValueError, match='calibration_context'):
        aoa_profile.parse_record_input_action({
            'action': 'tap', 'x': 180, 'y': 450, 'scale_x': 2.2, 'scale_y': 2.2,
            'calibration_context': {'version': 1, 'model': 'Box', 'display': [800, 1280], 'rotation': 0},
        })
