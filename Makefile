.PHONY: start-etl test-etl

start-etl:
	docker compose up -d postgres
	docker compose up --build cd-etl-api-server cd-etl-scheduler cd-etl-triggerer cd-etl-dag-processor

test-etl:
	docker compose run --rm -e PGDATABASE=congressional_app_test cd-etl migrate
	docker compose run --rm -e PGDATABASE=congressional_app_test cd-etl uv run pytest tests/$(TEST)
