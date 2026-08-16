.PHONY: start-etl test-etl start-server test-server

start-etl:
	docker compose up -d postgres
	docker compose up --build cd-etl

test-etl:
	docker compose run --rm -e PGDATABASE=congressional_app_test cd-etl uv run pytest tests/$(TEST)

start-server:
	docker compose up --build cd-server

test-server:
	docker compose run --rm cd-server uv run pytest tests/$(TEST)
