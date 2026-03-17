"""
数据模型包
"""
from .config import *
from .device import *
from .test import *
from .report import *

__all__ = [
    # Config
    'ConfigUpdate',
    'ClientInfoRequest',
    'ClientDetectRequest',
    # Device
    'DeviceInfo',
    'DeviceLockRequest',
    'DeviceRebootRequest',
    'DeviceRemountRequest',
    'DeviceWifiRequest',
    'DeviceScreenRequest',
    # Test
    'TestStartRequest',
    'TestStopRequest',
    'TestSuiteAutocompleteRequest',
    # Report
    'ReportFileRequest',
    'ReportUploadRequest',
]
