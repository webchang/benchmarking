import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KID = "test-key-1"
ISS = "http://keycloak.localtest.me:8080/realms/rossoctl"
BACKCHANNEL = "http://keycloak-service.keycloak:8080"
ROSSOCTL_URL = "http://rossoctl-backend.rossoctl-system:8000"

# Effective (backchannel-composed) URLs the Service will dial.
JWKS_URL = f"{BACKCHANNEL}/realms/rossoctl/protocol/openid-connect/certs"
TOKEN_URL = f"{BACKCHANNEL}/realms/rossoctl/protocol/openid-connect/token"


@pytest.fixture(scope="session")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def public_jwk(rsa_key) -> dict:
    pub_pem = rsa_key.public_key()
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(pub_pem))
    jwk["kid"] = KID
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    return jwk


@pytest.fixture(scope="session")
def jwks_doc(public_jwk) -> dict:
    return {"keys": [public_jwk]}


@pytest.fixture
def make_token(rsa_key):
    private_pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    def _make(
        iss: str = ISS,
        preferred_username: str = "alice",
        exp_offset: int = 3600,
        aud: str = "spiffe://example/agent",
        alg: str = "RS256",
        kid: str = KID,
    ) -> str:
        now = int(time.time())
        claims = {
            "iss": iss,
            "preferred_username": preferred_username,
            "aud": aud,
            "iat": now,
            "exp": now + exp_offset,
        }
        return jwt.encode(claims, private_pem, algorithm=alg, headers={"kid": kid})

    return _make


@pytest.fixture
def instance_dict() -> dict:
    return {
        "iss": ISS,
        "keycloak_backchannel_url": BACKCHANNEL,
        "rossoctl_base_url": ROSSOCTL_URL,
        "service_credential": {
            "client_id": "rossoctl",
            "username": "benchmarker",
            "password": "s3cr3t",
        },
        "mlflow": {"tracking_url": "http://mlflow.rossoctl-system.svc.cluster.local:5000"},
    }
