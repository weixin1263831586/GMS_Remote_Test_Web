from __future__ import annotations

from typing import Any


def calculate_window_positions(
    items: list[str],
    screen_width: int = 1920,
    screen_height: int = 1080,
    max_window_width: int = 350,
) -> dict[str, Any]:
    """Return shared tiled-window layout dimensions and offsets."""
    total = len(items)
    gap = 20

    max_available = screen_width - gap * (total + 1)
    width = (
        min(max_window_width, max_available // total)
        if total
        else max_window_width
    )
    height = int(width * 16 / 9)

    max_height = int(screen_height * 0.7)
    if height > max_height:
        height = max_height
        width = int(height * 9 / 16)

    total_width = total * width + (total - 1) * gap
    start_x = max(gap, (screen_width - total_width) // 2)
    start_y = max(50, (screen_height - height) // 2)

    return {
        'window_width': width,
        'window_height': height,
        'start_x': start_x,
        'start_y': start_y,
        'horizontal_gap': gap,
    }


def calculate_device_window_position(
    device_index: int,
    window_width: int,
    window_height: int,
    start_x: int,
    start_y: int,
    horizontal_gap: int,
    screen_width: int = 1920,
    screen_height: int = 1080,
    vertical_margin: int = 50,
) -> dict[str, int]:
    """Return one window position with screen-boundary checks."""
    x = start_x + device_index * (window_width + horizontal_gap)
    y = start_y
    if x + window_width > screen_width:
        x = max(0, screen_width - window_width - horizontal_gap)
    if y + window_height > screen_height:
        y = max(0, screen_height - window_height - vertical_margin)
    return {'x_offset': x, 'y_offset': y}
