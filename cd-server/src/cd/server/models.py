from datetime import datetime

from pydantic import BaseModel


# cd-server-local, not cd-lib -- cd-lib is for models a second component
# genuinely needs to validate against (Member/MembersResponse, see
# AGENTS.md), and cd-api/cd-etl have no auth/billing concern. Mirrors
# where cd-api keeps its own service-local models (VersionResponse,
# ProblemDetail) in cd-api/src/cd/api/models.py.
class User(BaseModel):
    id: str
    email: str
    created_at: datetime
    last_seen: datetime
