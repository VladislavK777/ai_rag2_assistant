.PHONY: help certs bootstrap test lint up down

help:
	@echo "RAG2 команды:"
	@echo "  make up        - поднять весь стек"
	@echo "  make down      - остановить"
	@echo "  make bootstrap - создать администратора и справочники"
	@echo "  make certs     - сгенерировать self-signed TLS"
	@echo "  make test      - тесты backend (unit + RBAC-критерий)"
	@echo "  make lint      - ruff + mypy"
	@echo "  make load      - нагрузочный прогон k6 (PROFILE=target)"

up:
	cd infra && docker compose up -d
	@echo "Стек поднимается. GPU-модели скачаются при первом старте (~12GB)."
	@echo "Далее: make bootstrap, затем открой http://localhost:5173"

down:
	cd infra && docker compose down

bootstrap:
	cd infra && docker compose exec backend python -m app.db.bootstrap

certs:
	mkdir -p infra/nginx/certs
	openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
		-keyout infra/nginx/certs/tls.key -out infra/nginx/certs/tls.crt \
		-subj "/CN=rag2.local"

test:
	cd backend && python -m pytest -v

lint:
	cd backend && ruff check app tests && mypy app --ignore-missing-imports

load: 
	k6 run backend/tests/load/k6_chat.js
