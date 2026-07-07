import logging

import keyring

from luminque.sender.constants import KEYRING_SERVICE_NAME

logger = logging.getLogger(__name__)

# The stored entry names predate the v1 ingestion contract; kept stable so
# already-enrolled machines don't need a credential migration.
CREDENTIAL_KEYS = {
    "auth_token":   "luminque_api_key",       # device token from /v1/enroll (sent as X-Device-Token)
    "endpoint_url": "luminque_endpoint_url",
}


def get_credential(key: str) -> str:
    credential_key = CREDENTIAL_KEYS.get(key, key)
    value = keyring.get_password(KEYRING_SERVICE_NAME, credential_key)
    if value is None:
        raise RuntimeError(
            f"Missing credential '{key}' in Windows Credential Manager. "
            f"Re-run onboarding (luminque.exe) to enroll this machine."
        )
    return value


def configure_credentials(auth_token: str, endpoint_url: str) -> None:
    keyring.set_password(KEYRING_SERVICE_NAME, CREDENTIAL_KEYS["auth_token"], auth_token)
    keyring.set_password(KEYRING_SERVICE_NAME, CREDENTIAL_KEYS["endpoint_url"], endpoint_url)
