# syntax=docker/dockerfile:1.7

FROM golang:1.26.6-bookworm AS builder

WORKDIR /src
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download

COPY . .
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/cindex-server ./cmd/server \
    && CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/cindex-worker ./cmd/worker \
    && CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/cindex-cli ./cmd/cli \
    && CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/cindex-healthcheck ./cmd/healthcheck

FROM gcr.io/distroless/static-debian13:nonroot

WORKDIR /app
COPY --from=builder /out/ /app/

USER nonroot:nonroot
EXPOSE 8000
ENTRYPOINT ["/app/cindex-server"]
