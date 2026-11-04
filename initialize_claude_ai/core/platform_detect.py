"""Runtime platform detection.

The installer must work on any mainstream Linux distribution without a
distro-specific branch in the phase code.  Everything the phases need to know is
gathered here into a :class:`PlatformInfo`, and package-manager selection is
driven by the ``family`` / ``package_manager`` fields against data in
``initialize_claude_ai/data/components.toml``.
"""

from __future__ import annotations

import glob
import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import truthy
from .logging_setup import get_logger

log = get_logger("platform")

#: distro id (or ID_LIKE token) -> package-manager key used in components.toml
DISTRO_FAMILIES: dict[str, str] = {
    "debian": "apt",
    "ubuntu": "apt",
    "linuxmint": "apt",
    "pop": "apt",
    "raspbian": "apt",
    "elementary": "apt",
    "kali": "apt",
    "fedora": "dnf",
    "rhel": "dnf",
    "centos": "dnf",
    "rocky": "dnf",
    "almalinux": "dnf",
    "ol": "dnf",
    "amzn": "dnf",
    "opensuse": "zypper",
    "opensuse-leap": "zypper",
    "opensuse-tumbleweed": "zypper",
    "opensuse-microos": "zypper",
    "sles": "zypper",
    "sled": "zypper",
    "suse": "zypper",
    "arch": "pacman",
    "manjaro": "pacman",
    "endeavouros": "pacman",
    "garuda": "pacman",
    "alpine": "apk",
    "gentoo": "emerge",
    "void": "xbps",
    "nixos": "nix",
    "clear-linux-os": "swupd",
    "solus": "eopkg",
    "mageia": "urpmi",
}

#: Ordered probe list: (package-manager key, executable name).  The first one
#: found on PATH wins when /etc/os-release is unhelpful.
PACKAGE_MANAGER_BINARIES: tuple[tuple[str, str], ...] = (
    ("zypper", "zypper"),
    ("apt", "apt-get"),
    ("dnf", "dnf"),
    ("yum", "yum"),
    ("pacman", "pacman"),
    ("apk", "apk"),
    ("xbps", "xbps-install"),
    ("emerge", "emerge"),
    ("eopkg", "eopkg"),
    ("urpmi", "urpmi"),
    ("swupd", "swupd"),
    ("nix", "nix-env"),
    ("brew", "brew"),
)

#: Cross-distro managers usable as a fallback when the native one lacks a package.
UNIVERSAL_MANAGERS: tuple[tuple[str, str], ...] = (
    ("flatpak", "flatpak"),
    ("snap", "snap"),
    ("brew", "brew"),
)


def read_os_release(path: str | os.PathLike[str] = "/etc/os-release") -> dict[str, str]:
    """Parse /etc/os-release (falls back to /usr/lib/os-release)."""
    candidates = [Path(path), Path("/usr/lib/os-release")]
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        values: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
        if values:
            return values
    return {}


@dataclass
class PlatformInfo:
    """What this machine is, as far as installing software is concerned.

    Detected once per run and then consulted rather than re-derived, so every
    phase agrees about the host. The distribution family and the package
    manager are what the component catalog keys off -- which is how one catalog
    covers the major Linux distributions without a branch per distribution.
    """

    system: str
    machine: str
    distro_id: str = ""
    distro_like: list[str] = field(default_factory=list)
    version_id: str = ""
    pretty_name: str = ""
    package_manager: str = ""
    universal_managers: list[str] = field(default_factory=list)
    is_root: bool = False
    privilege_escalation: list[str] = field(default_factory=list)
    has_display: bool = False
    session_type: str = ""
    desktop: str = ""
    #: The X display, if any. Separate from ``has_display`` because the
    #: input-method fallback that breaks typing in a browser is an X mechanism,
    #: and a machine can be non-headless without one -- WSL reaching a browser
    #: on the Windows side, or macOS.
    display: str = ""
    input_method: str = ""
    in_container: bool = False
    in_wsl: bool = False
    python_executable: str = sys.executable
    python_version: str = platform.python_version()
    libc: str = ""

    @property
    def is_linux(self) -> bool:
        return self.system == "Linux"

    @property
    def is_macos(self) -> bool:
        return self.system == "Darwin"

    @property
    def headless(self) -> bool:
        """True when no interactive browser can plausibly be launched."""
        return not self.has_display

    @property
    def desktop_session(self) -> bool:
        """True when a desktop is running, not merely an X display.

        The two come apart more often than one would expect: an X server
        started from a tty, an ``ssh -X`` forward, a VNC target, a container
        pointed at the host's socket. All of them can *draw* a browser, and
        none of them bring the session services a browser expects to find --
        no portal, no keyring, no input-method daemon. That gap is what
        :mod:`initialize_claude_ai.authenticate.browser` exists to paper over.
        """
        if self.desktop:
            return True
        return self.session_type.lower() in ("x11", "wayland", "mir")

    @property
    def arch(self) -> str:
        """Normalised architecture token used to pick download artifacts."""
        return {
            "x86_64": "x64",
            "amd64": "x64",
            "aarch64": "arm64",
            "arm64": "arm64",
            "armv7l": "armv7",
            "i686": "x86",
        }.get(self.machine.lower(), self.machine.lower())

    def traits(self) -> tuple[str, ...]:
        """Tokens a catalog entry can name in ``platforms`` beside an OS.

        "Linux" is too coarse for a few components: wslview belongs on WSL and
        is not in the archive anywhere else, and a component meant for a
        container is wrong on the host that runs it. These are the words for
        that, kept here so the catalog and the phase agree on the spelling.
        """
        return tuple(
            name
            for name, present in (
                ("wsl", self.in_wsl),
                ("container", self.in_container),
                ("root", self.is_root),
                ("headless", self.headless),
            )
            if present
        )

    def sudo_prefix(self) -> list[str]:
        """Command prefix needed to gain root, empty if already root."""
        if self.is_root or not self.privilege_escalation:
            return []
        tool = self.privilege_escalation[0]
        return [tool, "-n"] if tool == "sudo" else [tool]

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "machine": self.machine,
            "arch": self.arch,
            "distro_id": self.distro_id,
            "distro_like": self.distro_like,
            "version_id": self.version_id,
            "pretty_name": self.pretty_name,
            "package_manager": self.package_manager,
            "universal_managers": self.universal_managers,
            "is_root": self.is_root,
            "privilege_escalation": self.privilege_escalation,
            "has_display": self.has_display,
            "headless": self.headless,
            "session_type": self.session_type,
            "desktop": self.desktop,
            "desktop_session": self.desktop_session,
            "display": self.display,
            "input_method": self.input_method,
            "in_container": self.in_container,
            "in_wsl": self.in_wsl,
            "python_executable": self.python_executable,
            "python_version": self.python_version,
            "libc": self.libc,
        }


def _detect_container() -> bool:
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    if os.environ.get("container"):
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        return False
    return any(token in cgroup for token in ("docker", "containerd", "kubepods", "lxc", "podman"))


def _detect_wsl() -> bool:
    if "microsoft" in platform.release().lower():
        return True
    return bool(os.environ.get("WSL_DISTRO_NAME"))


def _detect_package_manager(os_release: dict[str, str]) -> str:
    """Pick the native package manager: os-release first, PATH probe second."""
    ids = [os_release.get("ID", "").strip().lower()]
    ids.extend(t for t in os_release.get("ID_LIKE", "").strip().lower().split() if t)
    for token in ids:
        family = DISTRO_FAMILIES.get(token)
        if family and shutil.which(dict(PACKAGE_MANAGER_BINARIES).get(family, family)):
            return family
    for key, binary in PACKAGE_MANAGER_BINARIES:
        if shutil.which(binary):
            return key
    # Recognised distro but its manager is not installed -- still report it so the
    # error message can be specific.
    for token in ids:
        if token in DISTRO_FAMILIES:
            return DISTRO_FAMILIES[token]
    return ""


#: musl's dynamic loader, any architecture. Matched rather than named, because
#: the file is named after the architecture and a fixed name fits only one.
MUSL_LOADER_GLOB = "/lib/ld-musl-*.so.1"

#: Commands that reach a browser this side of the machine cannot see: the
#: Windows half of a WSL install. Listed here rather than in the auth data
#: because detection needs them before any configuration is read.
WSL_OPENERS: tuple[str, ...] = ("wslview", "wslview.exe", "explorer.exe")

#: Variables that say an input method is configured for graphical toolkits.
#: Their *absence* on a machine with an X display is the signal that typing
#: into a browser may not reach the page -- see
#: :mod:`initialize_claude_ai.authenticate.browser`.
IM_VARIABLES: tuple[str, ...] = ("GTK_IM_MODULE", "QT_IM_MODULE", "XMODIFIERS")


def _detect_display(environ: dict[str, str], *, in_wsl: bool = False) -> bool:
    if truthy(environ.get("INITIALIZE_CLAUDE_AI_FORCE_HEADLESS")):
        return False
    if truthy(environ.get("INITIALIZE_CLAUDE_AI_FORCE_DISPLAY")):
        return True
    if platform.system() == "Darwin":
        return True
    if environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"):
        return True
    # WSL with no X server is not headless. The browser lives on the Windows
    # side and wslview hands it the URL, so the sign-in URL can be opened for
    # the operator. Calling this headless would leave every WSL user to carry
    # the URL across by hand for no reason.
    return in_wsl and any(shutil.which(opener) for opener in WSL_OPENERS)


def _detect_libc() -> str:
    try:
        name, version = platform.libc_ver()
    except Exception:  # pragma: no cover
        return ""
    if name:
        return f"{name} {version}".strip()
    # musl names its loader after the architecture -- ld-musl-x86_64,
    # ld-musl-aarch64, ld-musl-powerpc64le -- so any of them means musl. Naming
    # one architecture here left Alpine on aarch64 and ppc64le undetected.
    return "musl" if glob.glob(MUSL_LOADER_GLOB) else ""


def detect(environ: dict[str, str] | None = None) -> PlatformInfo:
    """Gather everything the phases need to know about this machine."""
    env = dict(environ if environ is not None else os.environ)
    os_release = read_os_release()
    in_wsl = _detect_wsl()
    info = PlatformInfo(
        system=platform.system(),
        machine=platform.machine(),
        distro_id=os_release.get("ID", ""),
        distro_like=[t for t in os_release.get("ID_LIKE", "").split() if t],
        version_id=os_release.get("VERSION_ID", ""),
        pretty_name=os_release.get("PRETTY_NAME", "") or platform.platform(),
        package_manager=_detect_package_manager(os_release),
        universal_managers=[key for key, binary in UNIVERSAL_MANAGERS if shutil.which(binary)],
        is_root=hasattr(os, "geteuid") and os.geteuid() == 0,
        privilege_escalation=[t for t in ("sudo", "doas", "pkexec") if shutil.which(t)],
        has_display=_detect_display(env, in_wsl=in_wsl),
        session_type=env.get("XDG_SESSION_TYPE", ""),
        desktop=env.get("XDG_CURRENT_DESKTOP", "") or env.get("DESKTOP_SESSION", ""),
        display=env.get("DISPLAY", ""),
        input_method=next(
            (env[n] for n in IM_VARIABLES if env.get(n)),
            "",
        ),
        in_container=_detect_container(),
        in_wsl=in_wsl,
        libc=_detect_libc(),
    )
    log.debug(
        "platform: %s (%s) pkg=%s universal=%s root=%s display=%s container=%s",
        info.pretty_name,
        info.arch,
        info.package_manager or "none",
        ",".join(info.universal_managers) or "none",
        info.is_root,
        info.has_display,
        info.in_container,
    )
    return info
