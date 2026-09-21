"""External rot: does the libretro buildbot still publish our cores?

The core names in systems.py have to match what the buildbot publishes, or
`[p]retroset download` quietly installs nothing. Nothing in the repository
can go stale here -- the *buildbot* can -- so this is a scheduled check
rather than a unit test, and it only runs when RETRO_TEST_NETWORK=1.
"""

import io
import urllib.error
import urllib.request
import zipfile

import pytest

from .loader import load_standalone

pytestmark = [pytest.mark.network, pytest.mark.slow]

S = load_standalone("retro_systems_for_buildbot", "systems.py")

BASE = "https://buildbot.libretro.com/nightly/linux/x86_64/latest"
TIMEOUT = 120


@pytest.mark.parametrize("core", sorted(S.CORES))
def test_every_recommended_core_is_published_for_linux_x86_64(core, tmp_path):
    name = f"{core}_libretro.so"
    try:
        with urllib.request.urlopen(f"{BASE}/{name}.zip", timeout=TIMEOUT) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        pytest.fail(f"{name}.zip is not on the buildbot any more ({error.code})")
    except urllib.error.URLError as error:  # pragma: no cover - network weather
        pytest.skip(f"the buildbot could not be reached: {error}")

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        member = next((e for e in archive.namelist() if e.endswith(name)), None)
        assert member, f"{name} is not inside its own zip: {archive.namelist()}"
        # A core that suddenly weighs nothing is a broken build, not a core.
        assert archive.getinfo(member).file_size > 64 * 1024


def test_the_buildbot_url_is_built_for_this_platform():
    pytest.importorskip("discord", reason="Retro builds the URL")
    from retro.Retro import Retro

    built = Retro._buildbot_url("gambatte")
    assert built is not None, "this platform has no buildbot mapping"
    url, filename = built
    assert url.startswith("https://buildbot.libretro.com/nightly/")
    assert url.endswith(f"{filename}.zip")
