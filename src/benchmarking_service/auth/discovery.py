from dataclasses import dataclass

from ..models import InstanceConfig


class IssuerFormatError(ValueError):
    pass


@dataclass(frozen=True)
class KeycloakEndpoints:
    """Effective Keycloak URLs for one instance.

    Composed from `keycloak_backchannel_url` when present (issuer host unreachable,
    e.g. in-cluster kind), else from the origin of `iss`. We do NOT trust the OIDC
    discovery doc's `jwks_uri`: with a backchannel it advertises the unreachable
    public host. So JWKS/token paths are composed against the effective base.
    """

    realm: str
    jwks_url: str
    token_url: str


def endpoints_for(instance: InstanceConfig) -> KeycloakEndpoints:
    iss = instance.iss
    if "/realms/" not in iss:
        raise IssuerFormatError(f"iss missing '/realms/' segment: {iss!r}")
    iss_base, _, tail = iss.partition("/realms/")
    realm = tail.split("/", 1)[0]
    if not realm:
        raise IssuerFormatError(f"iss has empty realm: {iss!r}")

    base = (instance.keycloak_backchannel_url or iss_base).rstrip("/")
    prefix = f"{base}/realms/{realm}/protocol/openid-connect"
    return KeycloakEndpoints(
        realm=realm,
        jwks_url=f"{prefix}/certs",
        token_url=f"{prefix}/token",
    )
