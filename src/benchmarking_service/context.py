from dataclasses import dataclass

from .models import InstanceConfig


@dataclass(frozen=True)
class RequestContext:
    """Request-scoped binding produced by caller-JWT validation.

    Holds the validated claims and the instance they route to. The caller JWT
    itself is never carried past this point (never forwarded to Rossoctl).
    """

    instance: InstanceConfig
    claims: dict

    @property
    def preferred_username(self) -> str | None:
        return self.claims.get("preferred_username")

    @property
    def is_benchmarker(self) -> bool:
        return self.preferred_username == "benchmarker"
