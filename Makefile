PACKAGES := packages/core packages/cms packages/templates packages/growth packages/observe \
            packages/gate packages/builder packages/platform admin packages/cli

.PHONY: dev test lint gate
dev:
	pip install $(foreach p,$(PACKAGES),-e $(p)) pytest ruff bandit pip-audit

test:
	pytest

lint:
	ruff check .

gate:
	redblue gate scan --path . --config .redblue.yml
