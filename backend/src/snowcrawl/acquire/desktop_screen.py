"""The desktop screen a site profile's browser presents (owner 2026-10-23): one of the common desktop screens of the
browser's platform, the same for the profile on every launch, in every process and on every machine, rather than one
size for every profile.

The screen is Chrome headless mode's virtual screen (`--screen-info`, Chromium components/headless/screen_info), its
work area that screen less the platform's desktop bar (Windows' taskbar at the bottom, the GNOME Shell top bar of a
Linux desktop at the top), and the window maximised in it (`--window-size`, at the work area's origin): the page reads
`screen.width` >= `outerWidth` >= `innerWidth`, `screen.availHeight` = `screen.height` less the bar = `outerHeight`,
`screen.availTop` = `screenY` = the top bar, and `outerHeight` - `innerHeight` = the height of the browser's own
toolbars, which Chrome reserves itself. Nothing is emulated (no Playwright viewport,
`Emulation.setDeviceMetricsOverride`): the page lays out in the window as in a user's browser.
"""

import hashlib
from bisect import bisect_right
from dataclasses import dataclass
from itertools import accumulate
from typing import Final

__all__ = ["DesktopScreen", "desktop_screen"]


@dataclass(frozen=True)
class DesktopScreen:
    """A desktop screen as a page reads it (CSSOM View `screen.width`/`height`, in CSS pixels), its
    `devicePixelRatio`, and the desktop bars at its top and bottom edges that its work area leaves out (CSS pixels)."""

    width: int
    height: int
    scale: float = 1.0
    top: int = 0
    bottom: int = 0

    def launch_args(self) -> list[str]:
        """Chrome's switches for this screen: `--screen-info` takes physical pixels, `--window-size` and
        `--window-position` CSS pixels."""
        physical = f"{round(self.width * self.scale)}x{round(self.height * self.scale)}"
        top, bottom = round(self.top * self.scale), round(self.bottom * self.scale)
        return [
            f"--screen-info={{{physical} devicePixelRatio={self.scale:g} workAreaTop={top} workAreaBottom={bottom}}}",
            f"--window-position=0,{self.top}",
            f"--window-size={self.width},{self.height - self.top - self.bottom}",
        ]


# The Windows 11 taskbar's height in CSS pixels at any scaling, at the bottom of the screen.
_TASKBAR: Final = 48
# GNOME Shell's top bar in its default theme, in CSS pixels at 100%: the desktop of the most used Linux distributions.
_TOP_BAR: Final = 32
# StatCounter Global Stats, desktop screen resolution worldwide, September 2026: the five most common screens by share
# (in hundredths of a percent), as pages read them in CSS pixels. 1280x1200 (3.64%, sixth) is left out: no desktop
# display has its 16:15 shape. 1536x864 and 1280x720 are 1920x1080 panels at Windows' 125% and 150% display scaling
# (CSSOM View: CSS pixels are device pixels divided by `devicePixelRatio`); the others are read at 100%.
_WINDOWS: Final = (
    (DesktopScreen(1920, 1080, bottom=_TASKBAR), 2807),
    (DesktopScreen(1536, 864, 1.25, bottom=_TASKBAR), 1000),
    (DesktopScreen(1366, 768, bottom=_TASKBAR), 795),
    (DesktopScreen(1280, 720, 1.5, bottom=_TASKBAR), 471),
    (DesktopScreen(2560, 1440, bottom=_TASKBAR), 395),
)
# The same panels on a Linux desktop, which runs them at 100% (GNOME's fractional scaling is off by default): the
# 1920x1080 panels Windows scales are read at their own size, so their shares join 1920x1080's.
_LINUX: Final = (
    (DesktopScreen(1920, 1080, top=_TOP_BAR), 2807 + 1000 + 471),
    (DesktopScreen(1366, 768, top=_TOP_BAR), 795),
    (DesktopScreen(2560, 1440, top=_TOP_BAR), 395),
)
# By the browser's platform as User-Agent Client Hints name it (`Sec-CH-UA-Platform`); any other is Linux's: a bar at
# the top and no scaling, as the desktops the engine runs on (Windows and Linux) leave it.
_PLATFORMS: Final = {"Windows": _WINDOWS, "Linux": _LINUX}


def desktop_screen(profile: str, platform: str) -> DesktopScreen:
    """The screen of the profile named `profile` (`acquire.browser_profiles.profile_name`) on the browser's `platform`,
    drawn by the screens' shares from a SHA-256 digest of the name, so a profile has the same screen wherever it is
    opened."""
    screens = _PLATFORMS.get(platform, _LINUX)
    bounds = tuple(accumulate(weight for _, weight in screens))
    point = int.from_bytes(hashlib.sha256(profile.encode()).digest()[:8], "big") % bounds[-1]
    return screens[bisect_right(bounds, point)][0]
