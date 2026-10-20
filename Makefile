.PHONY: start-etl test-etl start-server test-server eval-agent mcp-server

start-etl:
	docker compose up -d postgres
	docker compose up --build cd-etl

test-etl:
	docker compose run --rm -e PGDATABASE=congressional_app_test cd-etl uv run pytest tests/$(TEST)

start-server:
	docker compose up -d postgres
	docker compose up --build cd-server

test-server:
	CD_SERVER_GRAPHIQL_ENABLED=false docker compose run --rm -e PGDATABASE=cd_customers_test -e CD_API_PGDATABASE=congressional_app_test cd-server uv run pytest tests/$(TEST)

# The research agent's evals against real Bedrock and the dev
# congressional_app data -- costs real Bedrock calls. See
# cd-server/evals/run.py. ARGS passes options, e.g.
# make eval-agent ARGS="--case vt-senators --no-judge"
eval-agent:
	docker compose up -d postgres
	docker compose run --rm cd-server uv run python -m evals.run $(ARGS)

# The research agent's tools as an MCP server over stdio, for Claude
# Desktop / Claude Code (see cd-server/README.md's "MCP server"). -T and
# --entrypoint "" keep stdout clean for the protocol: no TTY, and no
# entrypoint migration output.
mcp-server:
	@docker compose up -d --quiet-pull postgres >&2
	@docker compose run --rm -T --no-deps --entrypoint "" cd-server uv run python -m cd.server.mcp_server
