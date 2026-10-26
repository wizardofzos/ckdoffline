"""Exception hierarchy; every exception carries a user-facing message."""


class CkdError(Exception):
    """Base class. str(exc) is the message shown to the user."""

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message


class FormatError(CkdError):
    """The image or a structure in it is malformed or unrecognised."""


class NoSpaceError(CkdError):
    """Not enough space (and the dataset cannot be extended)."""


class DirectoryFullError(CkdError):
    """The PDS directory has no room for another entry."""


class ValidationError(CkdError):
    """User input (name, line length, ...) was rejected."""


class ReadOnlyError(CkdError):
    """The image or session is read-only."""


class UnsupportedError(CkdError):
    """Valid but unsupported (PDSE, VBS edit, EAV, ...)."""


class NotFoundError(CkdError):
    """A dataset or member does not exist."""


class ImageChangedError(CkdError):
    """The image file was changed by someone else since this session opened it."""
