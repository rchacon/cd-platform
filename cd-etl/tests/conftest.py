import os

# members_etl reads this at import time; tests exercise pure data
# transforms and never make real API calls, so a placeholder is fine.
os.environ.setdefault("CONGRESS_API_KEY", "test-key")
