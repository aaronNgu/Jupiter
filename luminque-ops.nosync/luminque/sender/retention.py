import logging
import time

from luminque.sender.constants import RETENTION_SECONDS

logger = logging.getLogger(__name__)


def enforce_retention_cap(session, chunk_size: int = 500) -> int:
    """Null out blobs older than the retention cap, in bounded chunks.

    Chunked because a single UPDATE over a large backlog (e.g. machine
    offline >24h) holds SQLite's write lock for longer than the capture
    process's 5s busy_timeout, making capture's inserts fail. Each chunk
    commits independently, so capture can interleave writes.
    """
    from luminque.sender.models import Screenshot

    cutoff_timestamp = time.time() - RETENTION_SECONDS
    total = 0
    while True:
        ids = [
            row[0]
            for row in session.query(Screenshot.id)
            .filter(Screenshot.timestamp < cutoff_timestamp)
            .filter(Screenshot.png_data != None)
            .limit(chunk_size)
            .all()
        ]
        if not ids:
            break
        session.query(Screenshot).filter(Screenshot.id.in_(ids)).update(
            {
                "png_data": None,
                "png_diff_data": None,
                "png_diff_mask_data": None,
            },
            synchronize_session=False,
        )
        session.commit()
        total += len(ids)
    return total
