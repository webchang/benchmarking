import httpx
import pytest
import respx

from benchmarking_service.auth import caller_jwt
from benchmarking_service.auth.caller_jwt import CallerTokenError
from benchmarking_service.auth.jwks import JWKSCache
from benchmarking_service.models import InstanceConfig

from conftest import ISS, JWKS_URL


def _instance(instance_dict) -> InstanceConfig:
    return InstanceConfig.model_validate(instance_dict)


async def _jwks_cache(jwks_doc):
    client = httpx.AsyncClient()
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_doc))
    return JWKSCache(client, ttl_seconds=300), client


def test_read_unverified_iss(make_token):
    assert caller_jwt.read_unverified_iss(make_token()) == ISS


def test_read_unverified_iss_rejects_garbage():
    with pytest.raises(CallerTokenError):
        caller_jwt.read_unverified_iss("not.a.jwt")


@respx.mock
async def test_valid_signature_accepted(make_token, jwks_doc, instance_dict):
    cache, client = await _jwks_cache(jwks_doc)
    claims = await caller_jwt.validate(make_token(), _instance(instance_dict), cache)
    assert claims["preferred_username"] == "alice"
    await client.aclose()


@respx.mock
async def test_expired_token_still_accepted(make_token, jwks_doc, instance_dict):
    # exp is intentionally NOT enforced.
    cache, client = await _jwks_cache(jwks_doc)
    claims = await caller_jwt.validate(
        make_token(exp_offset=-3600), _instance(instance_dict), cache
    )
    assert claims["preferred_username"] == "alice"
    await client.aclose()


@respx.mock
async def test_aud_not_validated(make_token, jwks_doc, instance_dict):
    cache, client = await _jwks_cache(jwks_doc)
    claims = await caller_jwt.validate(
        make_token(aud="whatever-spiffe-id"), _instance(instance_dict), cache
    )
    assert claims["iss"] == ISS
    await client.aclose()


@respx.mock
async def test_tampered_signature_rejected(make_token, jwks_doc, instance_dict):
    cache, client = await _jwks_cache(jwks_doc)
    token = make_token()
    tampered = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    with pytest.raises(CallerTokenError):
        await caller_jwt.validate(tampered, _instance(instance_dict), cache)
    await client.aclose()


@respx.mock
async def test_unknown_kid_rejected(make_token, jwks_doc, instance_dict):
    cache, client = await _jwks_cache(jwks_doc)
    with pytest.raises(CallerTokenError):
        await caller_jwt.validate(make_token(kid="other"), _instance(instance_dict), cache)
    await client.aclose()


async def test_none_alg_rejected(instance_dict, jwks_doc):
    import jwt as pyjwt

    token = pyjwt.encode({"iss": ISS}, key=None, algorithm="none", headers={"kid": "test-key-1"})
    client = httpx.AsyncClient()
    cache = JWKSCache(client, ttl_seconds=300)
    with pytest.raises(CallerTokenError):
        await caller_jwt.validate(token, _instance(instance_dict), cache)
    await client.aclose()
