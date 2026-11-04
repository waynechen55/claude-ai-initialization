"""Exception hierarchy shared by every phase."""

from __future__ import annotations


class InitializeClaudeAiError(Exception):
    """Base class for all errors raised by initialize-claude-ai."""

    #: Short machine-readable code, surfaced in JSON reports.
    code = "error"

    def __init__(self, message: str, *, remediation: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation


class ConfigError(InitializeClaudeAiError):
    code = "config_error"


class MissingSettingError(ConfigError):
    """A value the run cannot honestly proceed without, and could not derive.

    Distinct from :class:`ConfigError` because of what it means downstream: a
    phase that raises this has not merely hit a snag, it has been asked to
    write a configuration with a hole in the middle of it. Carrying on would
    produce a ``settings.json`` that looks finished and does not work, so the
    phase is failed outright rather than reported as partially successful, and
    the run stops there with a non-zero exit code.
    """

    code = "missing_setting"


class SecretResolutionError(InitializeClaudeAiError):
    code = "secret_error"


class SecretPolicyError(SecretResolutionError):
    """A reference was refused on principle, not because it held no value.

    Kept distinct so that "try the next reference" logic does not quietly step
    over a credential the operator forbade -- a policy refusal must be heard.
    """

    code = "secret_policy"


class CommandError(InitializeClaudeAiError):
    code = "command_error"

    def __init__(
        self,
        message: str,
        *,
        argv: list[str] | None = None,
        returncode: int | None = None,
        stderr: str = "",
        remediation: str | None = None,
    ) -> None:
        super().__init__(message, remediation=remediation)
        self.argv = argv or []
        self.returncode = returncode
        self.stderr = stderr
