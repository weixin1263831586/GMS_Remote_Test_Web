"""Wait for explicit single-step inputs without dropping registered HID devices."""
from foundation.aoa_profile import parse_profile_commands, required_hid_channels


def debug_steps(source, frozen, *, tick, sleep, clock, error_type, idle_seconds=120, transform=None):
    channels = required_hid_channels(frozen)
    last_input = clock()
    while True:
        tick()
        closing = clock() - last_input >= idle_seconds
        entry = source(close=closing)
        if entry is None:
            if closing:
                return
            sleep(0.2)
            continue
        text = transform(entry["command"]) if transform else entry["command"]
        parsed = parse_profile_commands([text])
        if not required_hid_channels(parsed).issubset(channels):
            raise error_type("debug input requires another HID channel; restart the session")
        yield entry["sequence"], parsed[0], entry["command"]
        last_input = clock()
