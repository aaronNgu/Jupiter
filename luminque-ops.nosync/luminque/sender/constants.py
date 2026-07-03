from luminque.sender.__version__ import SENDER_VERSION

MAX_BATCH_EVENTS = 5000
MAX_BATCH_SCREENSHOTS = 50  # ~1 MB each base64; 50 ≈ 50 MB per batch, enough to stay ahead of ~1/s capture rate
RETENTION_SECONDS = 6 * 60 * 60  # null png_data after 6h (capture-side guard backstops at 8h)
REQUEST_TIMEOUT_SECONDS = 60
KEYRING_SERVICE_NAME = "luminque-sender"
API_SESSIONS_PATH = "/api/v1/sessions"
API_EVENTS_PATH = "/api/v1/sessions/{session_id}/events"
API_HEALTH_PATH = "/api/v1/devices/health"
DB_FILENAME = "recording.db"
STATE_FILENAME = "sender_state.json"
MACHINE_ID_FILENAME = "machine_id"
LOG_DIR = "logs"
