"""Shared, bounded execution options; no free-form commands or shell fragments."""

from __future__ import annotations


PREPARATION_TEMPLATES = {
    'stay_awake': {'name': '充电时保持常亮', 'commands': [['shell', 'svc', 'power', 'stayon', 'true']]},
    'animations_off': {'name': '关闭系统动画', 'commands': [
        ['shell', 'settings', 'put', 'global', key, '0']
        for key in ('window_animation_scale', 'transition_animation_scale', 'animator_duration_scale')
    ]},
    'wifi_on': {'name': '启用 Wi-Fi（仅启用，不保证连接指定网络）', 'commands': [['shell', 'svc', 'wifi', 'enable']]},
}


def validate_execution_options(value: dict, *, device_count: int = 0) -> dict:
    result = {}
    limits = {'shard_count': (0, 32), 'queue_timeout_seconds': (0, 604800),
              'execution_timeout_seconds': (0, 604800), 'output_idle_timeout_seconds': (0, 86400)}
    for key, (minimum, maximum) in limits.items():
        number = value.get(key, 0)
        if type(number) is not int or not minimum <= number <= maximum:
            raise ValueError(f'{key} must be an integer between {minimum} and {maximum}')
        if key.endswith('timeout_seconds') and 0 < number < 60:
            raise ValueError(f'{key} must be zero or at least 60 seconds')
        result[key] = number
    if device_count and result['shard_count'] > device_count:
        raise ValueError('shard_count cannot exceed the number of leased devices')
    actions = value.get('preparations', [])
    if not isinstance(actions, list) or len(actions) > 8:
        raise ValueError('preparations must contain at most 8 templates')
    if any(not isinstance(action, str) or action not in PREPARATION_TEMPLATES for action in actions):
        raise ValueError('Unknown preparation template')
    if len(actions) != len(set(actions)):
        raise ValueError('Duplicate preparation template')
    result['preparations'] = list(actions)
    return result


def matches_device_selector(device: dict, selector: dict) -> bool:
    properties = device.get('properties') or {}
    if not isinstance(properties, dict):
        properties = {}
    for field, aliases in {
        'product': ('product', 'ro.product.name'),
        'variant': ('variant', 'product_variant', 'device', 'ro.product.device'),
        'build_id': ('build_id', 'ro.build.id'),
        'android_version': ('android_version', 'ro.build.version.release'),
    }.items():
        expected = str(selector.get(field) or '').strip().lower()
        actual = next((str(properties[key]) for key in aliases if properties.get(key)), str(device.get(field) or ''))
        if expected and actual.lower() != expected:
            return False
    prefix = str(selector.get('serial_prefix') or '')
    if prefix and not str(device.get('serial') or '').startswith(prefix):
        return False
    board = str(selector.get('board') or '').lower()
    actual_board = str(properties.get('board') or properties.get('product') or device.get('board') or '').lower()
    return not board or board in actual_board
