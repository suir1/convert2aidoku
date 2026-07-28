class C2AError(Exception):
    """Base exception for expected user-facing failures."""


class ConfigurationError(C2AError):
    """Raised when configuration is incomplete or unsafe."""


class InputError(C2AError):
    """Raised when a Tachi source cannot be loaded."""


class UnsupportedSourceError(C2AError):
    """Raised when a source is outside the MVP scope."""


class AIProviderError(C2AError):
    """Raised when the compatible model endpoint cannot produce valid output."""

    def __init__(
        self,
        message: str,
        *,
        usage: object | None = None,
        warnings: list[str] | tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.warnings = list(warnings)


class SecurityError(C2AError):
    """Raised when generated output violates a security boundary."""


class ToolchainError(C2AError):
    """Raised when an external build tool fails or is unavailable."""
