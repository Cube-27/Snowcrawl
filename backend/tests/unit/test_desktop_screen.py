"""A site profile's screen (owner 2026-10-23): drawn from the profile's name alone, so the same profile presents the
same screen on every launch, in every process and on every machine, and the profiles together present every common
screen of the browser's platform, with that platform's desktop bar and scaling. What a page reads on it:
`test_browser_fetch.py`."""

import os
import subprocess
import sys

import pytest

from snowcrawl.acquire import desktop_screen as module
from snowcrawl.acquire.desktop_screen import desktop_screen

_COUNT = 500


def _draw(platform: str) -> str:
    """The draws of `_COUNT` profile names, as another process prints them."""
    return f"""from snowcrawl.acquire.desktop_screen import desktop_screen
print([desktop_screen(f"profile-{{i}}", {platform!r}) for i in range({_COUNT})])"""


@pytest.mark.parametrize("platform", sorted(module._PLATFORMS))
def test_a_profile_has_one_screen_everywhere_and_the_profiles_cover_every_screen(platform: str) -> None:
    drawn = [desktop_screen(f"profile-{i}", platform) for i in range(_COUNT)]

    assert set(drawn) == {screen for screen, _ in module._PLATFORMS[platform]}
    # Another process, with another salt for Python's per-process `hash()`, draws the same screens.
    seed = "2" if os.environ.get("PYTHONHASHSEED") == "1" else "1"
    other = subprocess.run(  # noqa: S603  (this interpreter, a constant script)
        [sys.executable, "-c", _draw(platform)],
        env={**os.environ, "PYTHONHASHSEED": seed},
        capture_output=True,
        text=True,
    )
    assert (other.returncode, other.stdout.strip()) == (0, str(drawn))


def test_a_linux_desktop_is_unscaled_with_its_bar_at_the_top_and_windows_has_its_taskbar_at_the_bottom() -> None:
    linux = {desktop_screen(f"profile-{i}", "Linux") for i in range(_COUNT)}
    windows = {desktop_screen(f"profile-{i}", "Windows") for i in range(_COUNT)}

    assert all(screen.scale == 1 and screen.top > 0 and screen.bottom == 0 for screen in linux)
    assert all(screen.top == 0 and screen.bottom > 0 for screen in windows)
    assert {screen.scale for screen in windows} > {1}
    # The window fills the work area, from its origin (`--window-position` and `--window-size` in CSS pixels).
    for screen in linux | windows:
        args = screen.launch_args()
        assert f"--window-position=0,{screen.top}" in args
        assert f"--window-size={screen.width},{screen.height - screen.top - screen.bottom}" in args
