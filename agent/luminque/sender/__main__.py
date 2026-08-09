"""`python -m luminque.sender` — dev/manual entry for one send cycle.

Delegates to luminque.sender.run() so logging is configured exactly once, the
same way the frozen exe's --send path does it (see run()/_setup_logging).
"""

from luminque.sender import run

if __name__ == "__main__":
    run()
