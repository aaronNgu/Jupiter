import logging

import keyring

from luminque.sender.constants import KEYRING_SERVICE_NAME

logger = logging.getLogger(__name__)

CREDENTIAL_KEYS = {
    "api_key": "luminque_api_key",          # device auth_token (sent as X-Device-Token)
    "endpoint_url": "luminque_endpoint_url",
    "tenant_id": "luminque_tenant_id",
    "device_id": "luminque_device_id",      # server-assigned device id (echoed in payloads)
}


def get_credential(key: str) -> str:
    credential_key = CREDENTIAL_KEYS.get(key, key)
    value = keyring.get_password(KEYRING_SERVICE_NAME, credential_key)
    if value is None:
        raise RuntimeError(
            f"Missing credential '{key}' in Windows Credential Manager. "
            f"Run 'luminque configure' to set up credentials."
        )
    return value


def configure_credentials(api_key: str, endpoint_url: str, tenant_id: str) -> None:
    keyring.set_password(KEYRING_SERVICE_NAME, CREDENTIAL_KEYS["api_key"], api_key)
    keyring.set_password(KEYRING_SERVICE_NAME, CREDENTIAL_KEYS["endpoint_url"], endpoint_url)
    keyring.set_password(KEYRING_SERVICE_NAME, CREDENTIAL_KEYS["tenant_id"], tenant_id)
