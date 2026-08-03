import jwt

from ..models import InstanceConfig
from .discovery import endpoints_for
from .jwks import JWKSCache

# Only asymmetric algorithms — never "none" or HMAC (alg-confusion defense).
ALLOWED_ALGS = frozenset(
    {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"}
)


class CallerTokenError(Exception):
    """Caller JWT could not be read or its signature did not validate."""


def read_unverified_iss(token: str) -> str:
    """Read `iss` WITHOUT verifying the signature — only to select the instance.

    The instance's key then verifies the signature, so this unverified read is a
    routing hint, never a trust decision.
    """
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise CallerTokenError(f"malformed JWT: {exc}") from exc
    iss = claims.get("iss")
    if not iss:
        raise CallerTokenError("JWT has no iss claim")
    return iss


async def validate(token: str, instance: InstanceConfig, jwks: JWKSCache) -> dict:
    """Signature-only validation against the instance's JWKS.

    Per design: `aud` is NOT validated (real rossoctl tokens carry SPIFFE
    audiences) and `exp` is NOT checked (token is never forwarded, so freshness is
    immaterial). Returns the validated claims.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise CallerTokenError(f"malformed JWT header: {exc}") from exc

    alg = header.get("alg")
    if alg not in ALLOWED_ALGS:
        raise CallerTokenError(f"disallowed or missing alg: {alg!r}")
    kid = header.get("kid")
    if not kid:
        raise CallerTokenError("JWT header has no kid")

    endpoints = endpoints_for(instance)
    jwk = await jwks.get_key(endpoints.jwks_url, kid)
    if jwk is None:
        raise CallerTokenError(f"no JWKS key for kid {kid!r}")

    try:
        signing_key = jwt.PyJWK.from_dict(jwk).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=[alg],
            options={"verify_aud": False, "verify_exp": False},
        )
    except jwt.PyJWTError as exc:
        raise CallerTokenError(f"signature validation failed: {exc}") from exc

    # Defense in depth: the signed iss must equal the instance we selected.
    if claims.get("iss") != instance.iss:
        raise CallerTokenError("signed iss does not match selected instance")
    return claims
