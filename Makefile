.PHONY: start-etl test-etl

start-etl:
	docker compose up -d postgres
	docker compose up --build cd-etl

test-etl:
	docker compose run --rm cd-etl uv run pytest tests/$(TEST)
