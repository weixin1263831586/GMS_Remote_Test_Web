"""Android InputReader and DisplayManager expose different viewport formats."""
import pytest

from foundation.aoa_display import input_displays


PRIMARY = {"display_id": 0, "display_route": "local:0", "rotation": 0, "display": [1200, 1920]}


@pytest.mark.parametrize("route", ["local:0", "'local:0'", '"local:0"'])
@pytest.mark.parametrize("frame", [
    "[0, 0, 1200, 1920]", "[0,0][1200,1920]", "Rect(0, 0 - 1200, 1920)",
])
def test_input_reader_and_display_manager_formats(route, frame):
    dump = f"Viewport INTERNAL: displayId=0, uniqueId={route}, orientation=0, logicalFrame={frame}"
    assert input_displays(dump) == [PRIMARY]


def test_android_display_manager_rect_with_virtual_mirror_is_separated():
    # The primary viewport shape matches a read-only RK3576 / Android 17 dump.
    primary = ("DisplayViewport{type=INTERNAL, valid=true, isActive=true, displayId=0, "
               "mirrorDisplayId=0, uniqueId='local:0', physicalPort=0, orientation=0, "
               "densityDpi=320, logicalFrame=Rect(0, 0 - 1200, 1920), "
               "physicalFrame=Rect(0, 0 - 1200, 1920), deviceWidth=1200, deviceHeight=1920}")
    virtual = ("DisplayViewport{type=VIRTUAL, valid=true, isActive=true, displayId=3, "
               "uniqueId='virtual:scrcpy', orientation=0, logicalFrame=Rect(0, 0 - 640, 1024)}")
    assert input_displays(f"mViewports=[{virtual}, {primary}]") == [PRIMARY, {
        "display_id": 3, "display_route": "virtual:scrcpy", "rotation": 0, "display": [640, 1024]}]


@pytest.mark.parametrize("field", ["valid=false", "isActive=false"])
def test_inactive_or_invalid_viewport_cannot_establish_display(field):
    dump = (f"DisplayViewport{{{field}, displayId=0, uniqueId='local:0', "
            "orientation=0, logicalFrame=Rect(0, 0 - 1200, 1920)}")
    assert input_displays(dump) == []


@pytest.mark.parametrize("frame", ["Rect(10, 0 - 1200, 1920)", "[0, 0, -1200, 1920]", "Rect(bad)"])
def test_invalid_frames_are_not_replaced_by_physical_frame(frame):
    dump = (f"Viewport: displayId=0, uniqueId=local:0, orientation=0, logicalFrame={frame}, "
            "physicalFrame=[0, 0, 1200, 1920]")
    assert input_displays(dump) == []


def test_conflicting_primary_viewports_in_one_list_are_rejected():
    viewport = ("DisplayViewport{displayId=0, uniqueId='local:0', orientation=0, "
                "logicalFrame=Rect(0, 0 - 1200, 1920)}")
    assert input_displays(f"mViewports=[{viewport}, {viewport}]") == [PRIMARY]
    assert input_displays(f"mViewports=[{viewport}, {viewport.replace('orientation=0', 'orientation=1')}]") == []


@pytest.mark.parametrize("route", ["''", '""', "'local:0", '"local:0'])
def test_missing_or_unterminated_route_is_rejected(route):
    dump = f"Viewport: displayId=0, uniqueId={route}, orientation=0, logicalFrame=[0, 0, 1200, 1920]"
    assert input_displays(dump) == []
