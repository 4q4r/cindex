package config

import (
	"errors"
	"testing"
)

func TestLoadMissingDatabaseURL(t *testing.T) {
	t.Setenv("DATABASE_URL", "")
	t.Setenv("REDIS_URL", "redis://localhost:6379/0")
	_, err := Load()
	if err == nil {
		t.Fatal("expected error for missing DATABASE_URL")
	}
	var missing *MissingEnvError
	if !errors.As(err, &missing) {
		t.Fatalf("expected MissingEnvError, got %T", err)
	}
}

func TestLoadDefaults(t *testing.T) {
	t.Setenv("DATABASE_URL", "postgres://u:p@localhost:5432/db")
	t.Setenv("REDIS_URL", "redis://localhost:6379/0")
	c, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if c.HTTPAddr != "127.0.0.1:8001" {
		t.Errorf("HTTPAddr = %q", c.HTTPAddr)
	}
	if c.Search.DefaultFreshnessDays != 14 {
		t.Errorf("DefaultFreshnessDays = %d", c.Search.DefaultFreshnessDays)
	}
	if c.LLM.Model != "glm-4.5-air" {
		t.Errorf("LLM.Model = %q", c.LLM.Model)
	}
}

func TestLoadOverrides(t *testing.T) {
	t.Setenv("DATABASE_URL", "postgres://u:p@localhost:5432/db")
	t.Setenv("REDIS_URL", "redis://localhost:6379/0")
	t.Setenv("CINDEX_ENV", "test")
	t.Setenv("CINDEX_HTTP_ADDR", "0.0.0.0:9000")
	t.Setenv("CINDEX_SEARCH_DEFAULT_FRESHNESS_DAYS", "30")
	t.Setenv("CINDEX_LLM_MODEL", "test-model")
	c, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if c.Env != "test" || c.HTTPAddr != "0.0.0.0:9000" || c.Search.DefaultFreshnessDays != 30 || c.LLM.Model != "test-model" {
		t.Fatalf("overrides not applied: %+v", c)
	}
}

func TestRedactedMasksSecrets(t *testing.T) {
	t.Setenv("DATABASE_URL", "postgres://alice:s3cret@db.example:5432/cindex")
	t.Setenv("REDIS_URL", "redis://localhost:6379/0")
	t.Setenv("CINDEX_LLM_API_KEY", "top-secret")
	c, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	r := c.Redacted()
	if got := r["database_url_redacted"]; got != "postgres://alice:****@db.example:5432/cindex" {
		t.Errorf("database_url_redacted = %q", got)
	}
	if got := r["llm_api_key_configured"]; got != "true" {
		t.Errorf("llm_api_key_configured = %q", got)
	}
	for k, v := range r {
		if v == "s3cret" || v == "top-secret" {
			t.Errorf("secret leaked in redacted value %s", k)
		}
	}
}
