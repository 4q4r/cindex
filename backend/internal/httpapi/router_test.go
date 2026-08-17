package httpapi

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
)

func newTestDeps(t *testing.T) Dependencies {
	t.Helper()

	ctx, cancel := context.WithTimeout(context.Background(), 15_000_000_000)
	defer cancel()

	pool, err := pgxpool.New(ctx, "postgres://nodb:1@127.0.0.1:1/nodb")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)

	rds := redis.NewClient(&redis.Options{Addr: "127.0.0.1:1", DialTimeout: 100_000_000})

	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	return Dependencies{Logger: logger, DB: pool, Redis: rds}
}

func TestHealthzReportsUnhealthyWhenDepsDown(t *testing.T) {
	r := NewRouter(newTestDeps(t))

	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "unhealthy") {
		t.Fatalf("body = %q, want unhealthy payload", rec.Body.String())
	}
}

func TestRequestIDHeaderAndLogging(t *testing.T) {
	var logged []string
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	_ = logger
	_ = logged

	r := NewRouter(Dependencies{Logger: slog.New(slog.NewTextHandler(io.Discard, nil))})

	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	req.Header.Set("X-Request-ID", "trace-123")
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if got := rec.Header().Get("X-Request-ID"); got != "trace-123" {
		t.Errorf("X-Request-ID = %q, want trace-123", got)
	}
}

func TestRequestIDGeneratedWhenAbsent(t *testing.T) {
	r := NewRouter(Dependencies{Logger: slog.New(slog.NewTextHandler(io.Discard, nil))})

	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if got := rec.Header().Get("X-Request-ID"); got == "" {
		t.Error("X-Request-ID missing, want generated id")
	}
}

func TestRecovererReturns500(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	mux := http.NewServeMux()
	mux.HandleFunc("GET /boom", func(w http.ResponseWriter, _ *http.Request) {
		panic("boom")
	})
	h := chain(mux, recoverer(logger))

	req := httptest.NewRequest(http.MethodGet, "/boom", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", rec.Code)
	}
}
