"""Tunables for captureV2. See design-docs/luminque-capture-p3.md §5."""

# Sampling cadence
ACTIVE_INTERVAL_SECONDS = 0.25  # ~4 fps while the user is active
IDLE_THRESHOLD_SECONDS = 5.0    # no input for this long → stop sampling
IDLE_POLL_SECONDS = 0.5         # wake latency when coming out of idle

# Image processing
MAX_IMAGE_WIDTH = 1280          # stored screenshot width cap (aspect preserved)
THUMB_WIDTH = 64                # thumbnail used for brightness + hash
DHASH_DISTANCE_THRESHOLD = 2    # hamming bits differing to count as "changed"
BLANK_BRIGHTNESS_THRESHOLD = 8.0  # mean grayscale below this → lock screen / display sleep
PNG_COMPRESS_LEVEL = 1          # speed over ratio

# Disk guard (capture-side, runs independently of the sender)
# The sender normally nulls png_data (on upload, and via its retention cap),
# but it returns early on credential failure and may not run at all
# (unenrolled, --send task missing, exe quarantined). This guard bounds local
# disk regardless, on the always-running capture process.
MAINTENANCE_INTERVAL_SECONDS = 300         # run the guard every 5 min
LOCAL_MAX_BLOB_BYTES = 2 * 1024**3         # hard size bound: 2 GiB of blobs
LOCAL_MAX_BLOB_AGE_SECONDS = 8 * 60 * 60   # age bound: 8h (backstops sender's 6h RETENTION_SECONDS)

# Data layer
DB_FILENAME = "recording.db"
TASK_DESCRIPTION = "luminque-background"
