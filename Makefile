.PHONY: build up down logs lint type test \
        verify-m0 verify-m1 verify-m2 verify-m3 verify-m4 verify-m5 verify-m6 verify-m7 verify-m8 verify-all

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

lint:
	ruff check .

type:
	mypy app

test:
	pytest -q

verify-m0:
	docker compose build
	docker compose run --rm web python -c "import app; print(app.__version__)"
	ruff check .
	mypy app/core

verify-m1:
	pytest -q tests/test_bootstrap.py

verify-m2:
	pytest -q tests/test_clouds.py

verify-m3:
	pytest -q tests/test_dedup.py tests/test_normalizer.py

verify-m4:
	@echo "M4 verify requires mocked Mgmt fixtures — see tests/test_mgmt_poller.py when authored."

verify-m5:
	@echo "M5 verify requires APScheduler integration test."

verify-m6:
	@echo "M6 verify requires web/OIDC integration tests."

verify-m7:
	@echo "M7 verify requires runs page + /metrics integration tests."

verify-m8:
	@bash -c 'curl -fsS http://localhost:$${WEB_PORT:-8080}/healthz'

verify-all: verify-m0 verify-m1 verify-m2 verify-m3 verify-m4 verify-m5 verify-m6 verify-m7 verify-m8
