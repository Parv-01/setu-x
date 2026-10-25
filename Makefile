# Convenience targets (Linux/macOS/Git Bash). Windows users can run the docker compose
# commands directly; see docs/testing_on_windows.md.
.PHONY: config up down ps logs test e2e acceptance docs-check lint

config:
	docker compose config --quiet && echo "compose.yaml OK"

up:
	docker compose up --build -d --wait

down:
	docker compose down

ps:
	docker compose ps

logs:
	docker compose logs --tail=100

test:
	docker compose --profile test run --rm --build tests

e2e:
	docker compose --profile test run --rm tests pytest -p no:cacheprovider -m e2e

acceptance:
	bash scripts/phase2_acceptance.sh

docs-check:
	python3 scripts/check_docs.py

lint:
	docker compose --profile test run --rm tests ruff check --no-cache src tests
