import os
import tempfile

# agent.agent opens its SQLite store at import time; keep that out of the working directory.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="forge-agent-test-"))
