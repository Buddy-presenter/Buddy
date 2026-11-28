"""Keep Windows from power-throttling Buddy while the microphone is on (#255).

Windows 11 may throttle a process without a visible foreground window ("efficiency mode",
EcoQoS) and coalesce its timers. In Buddy Mini the main window is hidden, and a slowed GUI
thread drops microphone audio. Audio apps opt out with the documented
``SetProcessInformation(ProcessPowerThrottling)`` call while they capture; this restores the
system default when the microphone stops. Other platforms and failures are a no-op.
"""

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

PROCESS_POWER_THROTTLING = 4  # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
CURRENT_VERSION = 1
EXECUTION_SPEED = 0x1
IGNORE_TIMER_RESOLUTION = 0x4


class _ThrottlingState(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_ulong),
        ("ControlMask", ctypes.c_ulong),
        ("StateMask", ctypes.c_ulong),
    ]


def keep_full_speed(enabled: bool) -> bool:
    """Opt out of (or back into) Windows power throttling. Returns True when applied."""
    if sys.platform != "win32":
        return False
    state = _ThrottlingState(
        CURRENT_VERSION, EXECUTION_SPEED | IGNORE_TIMER_RESOLUTION if enabled else 0, 0
    )
    try:
        kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009 - Windows-only attribute.
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetProcessInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        applied = bool(
            kernel32.SetProcessInformation(
                kernel32.GetCurrentProcess(),
                PROCESS_POWER_THROTTLING,
                ctypes.byref(state),
                ctypes.sizeof(state),
            )
        )
    except (AttributeError, OSError):
        applied = False
    logger.warning(
        "presentation_speech event=power_throttling_%s applied=%s",
        "disabled" if enabled else "restored",
        applied,
    )
    return applied
