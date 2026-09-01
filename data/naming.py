"""
data/naming.py

Naming of measurement files: builds the name actually used on disk from
the entered measurement name, and derives the two file paths that belong
to it.

Background:
    The rules (optional date/time, ascending number suffix, and above
    all "never silently overwrite existing measurement data") used to
    live as a private method inside `gui/main_window.py` and read their
    parameters straight off the setup view's widgets. A script driving
    `core/measurement_runner.py` therefore had no way to reach them and
    had to invent its own naming - which meant the overwrite protection
    silently did not apply there.

    Hence this module: GUI-free, so both the GUI and a headless script
    resolve names through the exact same code.

    The scheme itself (`NamingScheme`) is a field of `MeasurementConfig`
    and lives in `data/models.py` with the other configuration
    dataclasses - naming is a storage option, and storage options belong
    to the configuration file, next to `save_to_disk` and
    `storage_format`. It is re-exported here so that callers can take
    the scheme and the function that applies it from one place.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from data.models import NamingScheme, StorageFormat

__all__ = [
    "MeasurementNameConflict",
    "NamingScheme",
    "measurement_data_path",
    "measurement_metadata_path",
    "measurement_name_exists",
    "resolve_measurement_name",
]


class MeasurementNameConflict(RuntimeError):
    """A measurement of that name already exists and the naming scheme
    has no way to resolve it automatically.

    Raised instead of overwriting: measurement data that has already
    been recorded is never silently replaced. The caller decides how to
    react - the GUI shows an error message and stays in the setup view,
    a script aborts.

    Attributes:
        name: The resolved name that collided.
        storage_dir: The directory it collided in.
    """

    def __init__(self, name: str, storage_dir: Path) -> None:
        super().__init__(
            f"Eine Messung namens '{name}' existiert bereits in {storage_dir}."
        )
        self.name = name
        self.storage_dir = storage_dir


def measurement_data_path(
    storage_dir: Path, name: str, storage_format: StorageFormat
) -> Path:
    """Path of the measurement data file for `name` in `storage_dir`.

    Single source of the file extension: the conflict check below is
    only worth anything as long as it looks at exactly the file the
    `StorageWriter` later writes.
    """
    extension = ".parquet" if storage_format == StorageFormat.PARQUET else ".csv"
    return storage_dir / f"{name}{extension}"


def measurement_metadata_path(storage_dir: Path, name: str) -> Path:
    """Path of the metadata file (`{name}_info.json`) belonging to `name`."""
    return storage_dir / f"{name}_info.json"


def measurement_name_exists(
    storage_dir: Path, name: str, storage_format: StorageFormat
) -> bool:
    """Whether a measurement of that name already occupies the folder.

    Data file OR metadata file is enough: an aborted measurement can
    leave one of the two behind, and reusing the name would produce a
    pair of files that do not belong together.
    """
    return (
        measurement_data_path(storage_dir, name, storage_format).exists()
        or measurement_metadata_path(storage_dir, name).exists()
    )


def resolve_measurement_name(
    base_name: str,
    storage_dir: Path,
    storage_format: StorageFormat,
    naming: NamingScheme,
    now: datetime | None = None,
) -> str:
    """Builds the name actually to be used from the entered one.

    Order of the optional components: Name_Date_Time_Number.

    Args:
        base_name: The entered measurement name.
        storage_dir: Folder the measurement will be written to. Existing
            files in it decide which number suffix is free.
        storage_format: Decides the extension the conflict check looks for.
        naming: The scheme to apply.
        now: Timestamp for the date/time components, for tests. Defaults
            to the current time.

    Returns:
        The resolved name, without extension.

    Raises:
        MeasurementNameConflict: if the name is taken and no number
            suffix is active to resolve it.
        RuntimeError: if the number suffix is active but every value in
            its digit range is already taken.
    """
    moment = now if now is not None else datetime.now()

    parts = [base_name]
    if naming.include_date:
        parts.append(moment.strftime("%Y%m%d"))
    if naming.include_time:
        parts.append(moment.strftime("%H%M%S"))

    if naming.use_number_suffix:
        digits = max(1, naming.number_suffix_digits)
        for index in range(1, 10**digits):
            candidate = "_".join(parts + [f"{index:0{digits}d}"])
            if not measurement_name_exists(storage_dir, candidate, storage_format):
                return candidate
        raise RuntimeError(
            "Konnte keinen eindeutigen Messnamen finden "
            f"(Basisname '{base_name}')."
        )

    candidate = "_".join(parts)
    if measurement_name_exists(storage_dir, candidate, storage_format):
        raise MeasurementNameConflict(candidate, storage_dir)
    return candidate
