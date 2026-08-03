import pytest

from benchmarking_service.auth.discovery import IssuerFormatError, endpoints_for
from benchmarking_service.models import InstanceConfig


def _instance(iss, backchannel=None):
    return InstanceConfig(
        iss=iss,
        keycloak_backchannel_url=backchannel,
        rossoctl_base_url="http://r:8000",
        service_credential={"client_id": "c", "username": "u", "password": "p"},
    )


def test_compose_from_iss_when_no_backchannel():
    ep = endpoints_for(_instance("https://kc.example.com/realms/kagenti"))
    assert ep.realm == "kagenti"
    assert ep.jwks_url == "https://kc.example.com/realms/kagenti/protocol/openid-connect/certs"
    assert ep.token_url == "https://kc.example.com/realms/kagenti/protocol/openid-connect/token"


def test_backchannel_overrides_iss_host():
    ep = endpoints_for(
        _instance(
            "http://keycloak.localtest.me:8080/realms/rossoctl",
            backchannel="http://keycloak-service.keycloak:8080",
        )
    )
    # iss host is NOT dialed; backchannel host is.
    assert ep.jwks_url.startswith("http://keycloak-service.keycloak:8080/realms/rossoctl/")
    assert ep.token_url.startswith("http://keycloak-service.keycloak:8080/realms/rossoctl/")


def test_base_path_preserved():
    ep = endpoints_for(_instance("https://kc.example.com/auth/realms/r"))
    assert ep.jwks_url == "https://kc.example.com/auth/realms/r/protocol/openid-connect/certs"


def test_bad_iss_raises():
    with pytest.raises(IssuerFormatError):
        endpoints_for(_instance("https://kc.example.com/no-realms-here"))
