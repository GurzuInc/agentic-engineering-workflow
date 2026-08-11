from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

from engineering_policy.errors import PolicyError

_SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, raw: str) -> Version:
        if not isinstance(raw, str):
            raise PolicyError(f"invalid semantic version: {raw!r}")
        value = raw.removeprefix("v")
        match = _SEMVER.fullmatch(value)
        if not match:
            raise PolicyError(f"invalid semantic version: {raw}")
        prerelease = tuple((match.group("pre") or "").split("."))
        if prerelease == ("",):
            prerelease = ()
        if any(
            identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0")
            for identifier in prerelease
        ):
            raise PolicyError(f"invalid semantic version: {raw}")
        return cls(
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
            prerelease,
        )

    @property
    def stable(self) -> bool:
        return not self.prerelease

    @property
    def constraint(self) -> str:
        return f"{self.major}.x"

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base if self.stable else f"{base}-{'.'.join(self.prerelease)}"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        core = (self.major, self.minor, self.patch)
        other_core = (other.major, other.minor, other.patch)
        if core != other_core:
            return core < other_core
        if self.stable != other.stable:
            return not self.stable
        return _compare_prerelease(self.prerelease, other.prerelease) < 0


def _compare_prerelease(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    for a, b in zip(left, right, strict=False):
        if a == b:
            continue
        if a.isdigit() and b.isdigit():
            return -1 if int(a) < int(b) else 1
        if a.isdigit() != b.isdigit():
            return -1 if a.isdigit() else 1
        return -1 if a < b else 1
    return (len(left) > len(right)) - (len(left) < len(right))


def choose_latest(
    releases: list[str], *, major: int, channel: str, current: Version | None = None
) -> Version:
    candidates: list[Version] = []
    for raw in releases:
        try:
            version = Version.parse(raw)
        except PolicyError:
            continue
        if version.major != major:
            continue
        if channel == "stable" and not version.stable:
            continue
        candidates.append(version)
    if not candidates:
        raise PolicyError(f"no {channel} release found for {major}.x")
    selected = max(candidates)
    if current is not None and selected < current:
        return current
    return selected
