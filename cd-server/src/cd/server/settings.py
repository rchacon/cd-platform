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
