MAX_BATCH_SCREENSHOTS = 200  # per-cycle upload cap; sized so one cycle drains a
# real backlog, decoupling upload throughput from the sender's run interval —
# otherwise a busy machine can capture faster than the cap * (1/interval) and
# the 6h retention / 8h capture guard evicts unsent frames (data loss).
RETENTION_SECONDS = 6 * 60 * 60  # null png_data after 6h (capture-side guard backstops at 8h)
REQUEST_TIMEOUT_SECONDS = 60
KEYRING_SERVICE_NAME = "luminque-sender"
API_SCREENSHOTS_PATH = "/v1/screenshots"
API_HEARTBEAT_PATH = "/v1/heartbeat"
DB_FILENAME = "recording.db"
STATE_FILENAME = "sender_state.json"
LOG_DIR = "logs"
