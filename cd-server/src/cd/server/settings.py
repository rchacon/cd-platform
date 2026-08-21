import os

# "local" picks HttpApiClient (plain HTTP to a locally-running cd-api);
# anything else picks LambdaApiClient (direct boto3 invoke of the real
# deployed function -- no AWS credentials/function needed for local dev).
ENVIRONMENT = os.environ.get("CD_SERVER_ENVIRONMENT", "local")

# cd-api's own README runs it via `uv run uvicorn ... --app-dir src` on
# the host, not in docker-compose (it has no Dockerfile) -- reachable
# from cd-server's own container via host.docker.internal (see that
# service's extra_hosts in docker-compose.yml), but only if cd-api is
# started with --host 0.0.0.0, not uvicorn's 127.0.0.1-only default.
# Port 8001, not cd-api's own README default of 8000 -- that's cd-server's
# own published port (docker-compose.yml), so running cd-api on 8000 too
# collides with it.
CD_API_BASE_URL = os.environ.get("CD_API_BASE_URL", "http://host.docker.internal:8001")

CD_API_FUNCTION_NAME = os.environ.get("CD_API_FUNCTION_NAME", "")

# Browser origins allowed to call the GraphQL endpoint cross-origin --
# cd-webapp's deployed production domain, plus its local Vite dev server.
# See app.py's CORSMiddleware setup.
CORS_ALLOWED_ORIGINS = [
    "https://app.civicdog.com",
    "http://localhost:5183",
]

# cd_customers, the database cd-server alone reads/writes (see
# services/users_service.py) -- same PGHOST/PGPORT/PGDATABASE/PGUSER/
# PGPASSWORD convention as cd-etl's and cd-api's own PG_DSN, just a
# different default PGDATABASE.
PGHOST = os.environ.get("PGHOST", "localhost")
PGPORT = os.environ.get("PGPORT", "5432")
PGDATABASE = os.environ.get("PGDATABASE", "cd_customers")
PGUSER = os.environ.get("PGUSER", "postgres")
PGPASSWORD = os.environ.get("PGPASSWORD", "postgres")
PG_DSN = f"postgresql://{PGUSER}:{PGPASSWORD}@{PGHOST}:{PGPORT}/{PGDATABASE}"

# Both required to construct a real JWKS client; deliberately no
# hardcoded default for either, unlike CD_API_BASE_URL's
# host.docker.internal default above -- the real values are cd-infra
# Terraform outputs (cognito_user_pool_id, that pool's own region), not
# something a safe local default can guess. See
# services/users_service.py's get_users_service(): unset here disables
# JWT verification entirely when ENVIRONMENT is "local" (the default) --
# make start-server must keep working with zero AWS setup for
# representative-lookup-only local dev -- but is a hard RuntimeError,
# fail-fast at import, for any other environment, matching
# get_cd_api_service()'s own precedent.
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
COGNITO_REGION = os.environ.get("COGNITO_REGION", "")

# Comma-separated -- covers both cd-webapp App Clients that share the one
# User Pool (prod + local-dev-against-localhost:5183, cd-infra's
# cd_webapp_prod/cd_webapp_dev Terraform resources), since a token minted
# by either must verify here.
COGNITO_CLIENT_IDS = [
    client_id.strip()
    for client_id in os.environ.get("COGNITO_CLIENT_IDS", "").split(",")
    if client_id.strip()
]
