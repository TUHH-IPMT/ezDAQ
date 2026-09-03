"""
core/update_checker.py

Checks GitHub Releases for a newer ezDAQ version than the one currently
running (see `gui/main_window.py::_start_update_check` for the manual
"Nach Updates suchen..." menu action and the automatic once-per-startup
check).

Deliberately plain stdlib (`urllib.request`) instead of `requests`: the
only thing this module needs is a single GET request, and adding a new
dependency for that (see `requirements.txt`) would be disproportionate.
Qt's own networking (`QtNetwork`) would work too, but would pull Qt into
a module that otherwise has nothing to do with the GUI and is easier to
unit-test without it - see `tests/test_update_checker.py`.

`check_for_update()` is a plain blocking function; it is meant to run
inside `gui/workers.py::BackgroundWorker` so the network round-trip
never blocks the GUI thread.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass

from config.settings import APP_VERSION

# GitHub Releases API for the public ezDAQ repository (see
# `packaging/ezDAQ.iss::AppURL`). "latest" is defined by GitHub as the
# most recent release that is neither a draft nor a prerelease - exactly
# what this check should offer; a prerelease must not nag users to
# update to it.
_LATEST_RELEASE_API_URL = "https://api.github.com/repos/TUHH-IPMT/ezDAQ/releases/latest"

# GitHub's API rejects requests without a User-Agent header (403).
_USER_AGENT = "ezDAQ-update-check"

_REQUEST_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class UpdateCheckResult:
    """Result of a single `check_for_update()` call.

    `release_url` points at the GitHub release page (human-readable,
    with changelog and download link), not at the API URL - it is what
    gets opened in the browser if the user chooses to update (see
    `gui/main_window.py::_show_update_available_dialog`).
    """

    update_available: bool
    current_version: str
    latest_version: str
    release_url: str


def parse_version(text: str) -> tuple[int, ...]:
    """Parses a version string like "v0.5.0" or "0.5.0" into a tuple of
    ints, for comparison by `is_newer_version()`.

    Deliberately lenient: a leading "v"/"V" (GitHub tags carry one,
    `APP_VERSION` does not) and any non-numeric suffix (e.g. a stray
    "-beta") are simply ignored rather than raising - a malformed tag
    should not crash the check.
    """
    parts = re.findall(r"\d+", text)
    return tuple(int(part) for part in parts) or (0,)


def is_newer_version(candidate: str, reference: str) -> bool:
    """True if `candidate` (e.g. a GitHub release tag) is a newer
    version than `reference` (e.g. `APP_VERSION`).

    Compares as (zero-)padded int tuples, so "0.5.10" > "0.5.9" (a plain
    string compare would get that backwards, "10" < "9") and
    "0.5" == "0.5.0".
    """
    candidate_parts = parse_version(candidate)
    reference_parts = parse_version(reference)
    length = max(len(candidate_parts), len(reference_parts))
    candidate_padded = candidate_parts + (0,) * (length - len(candidate_parts))
    reference_padded = reference_parts + (0,) * (length - len(reference_parts))
    return candidate_padded > reference_padded


def check_for_update(
    current_version: str = APP_VERSION,
    timeout: float = _REQUEST_TIMEOUT_SECONDS,
) -> UpdateCheckResult:
    """Fetches the latest published GitHub release and compares it
    against `current_version`.

    Raises on any failure (no network, DNS, GitHub unreachable,
    unexpected response shape: `urllib.error.URLError` - includes
    `HTTPError` -, `TimeoutError`, `json.JSONDecodeError`, `KeyError`).
    Deliberately not swallowed here: the caller
    (`gui/workers.py::BackgroundWorker`) turns an exception into its
    `failed` signal, and a silent "no update available" on a network
    error would be indistinguishable from an actual up-to-date result -
    the manual menu action needs to tell the two apart to report the
    failure instead of a false all-clear.
    """
    request = urllib.request.Request(
        _LATEST_RELEASE_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": _USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    latest_version = payload["tag_name"]
    release_url = payload["html_url"]
    return UpdateCheckResult(
        update_available=is_newer_version(latest_version, current_version),
        current_version=current_version,
        latest_version=latest_version,
        release_url=release_url,
    )
