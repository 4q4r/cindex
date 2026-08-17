GO ?= go
GOLANGCI_LINT ?= golangci-lint

.PHONY: build test lint vet tidy migrate-up run-server

build:
	$(GO) build ./...

test:
	$(GO) test ./...

test-integration:
	$(GO) test -tags integration ./...

vet:
	$(GO) vet ./...

lint:
	$(GOLANGCI_LINT) run

tidy:
	$(GO) mod tidy

migrate-up:
	$(GO) run ./cmd/cli migrate

run-server:
	$(GO) run ./cmd/server

run-worker:
	$(GO) run ./cmd/worker
