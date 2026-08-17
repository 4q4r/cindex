package config

import (
	"os"
	"strconv"
	"strings"
	"time"
)

// Config holds the 12-factor environment configuration for all binaries.
type Config struct {
	Env string

	HTTPAddr    string
	ShutdownCtx time.Duration

	DatabaseURL string
	RedisURL    string

	ArticlesDir string

	AdminAPIKey string

	LLM struct {
		BaseURL string
		APIKey  string
		Model   string
		Timeout time.Duration
	}

	Search struct {
		DefaultFreshnessDays int
		RateLimitPerIP       int
		RateLimitWindow      time.Duration
	}

	PDFSidecarURL string
}

// Load reads the environment into a Config, failing on missing required keys.
func Load() (*Config, error) {
	c := &Config{}

	c.Env = envOrDefault("CINDEX_ENV", "production")
	c.HTTPAddr = envOrDefault("CINDEX_HTTP_ADDR", "127.0.0.1:8001")
	c.ShutdownCtx = durationOrDefault("CINDEX_SHUTDOWN_TIMEOUT", 10*time.Second)

	c.DatabaseURL = envOrDefault("DATABASE_URL", "")
	if c.DatabaseURL == "" {
		return nil, errMissing("DATABASE_URL")
	}
	c.RedisURL = envOrDefault("REDIS_URL", "")
	if c.RedisURL == "" {
		return nil, errMissing("REDIS_URL")
	}

	c.ArticlesDir = envOrDefault("CINDEX_ARTICLES_DIR", "./var/articles")
	c.AdminAPIKey = envOrDefault("CINDEX_ADMIN_API_KEY", "")

	c.LLM.BaseURL = envOrDefault("CINDEX_LLM_BASE_URL", "https://api.z.ai/api/paas/v4")
	c.LLM.APIKey = envOrDefault("CINDEX_LLM_API_KEY", "")
	c.LLM.Model = envOrDefault("CINDEX_LLM_MODEL", "glm-4.5-air")
	c.LLM.Timeout = durationOrDefault("CINDEX_LLM_TIMEOUT", 180*time.Second)

	c.Search.DefaultFreshnessDays = intOrDefault("CINDEX_SEARCH_DEFAULT_FRESHNESS_DAYS", 14)
	c.Search.RateLimitPerIP = intOrDefault("CINDEX_SEARCH_RATE_LIMIT_PER_IP", 10)
	c.Search.RateLimitWindow = durationOrDefault("CINDEX_SEARCH_RATE_LIMIT_WINDOW", 60*time.Second)

	c.PDFSidecarURL = envOrDefault("CINDEX_PDF_SIDECAR_URL", "")

	return c, nil
}

func envOrDefault(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func intOrDefault(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func durationOrDefault(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}

// MissingEnvError reports a required environment variable that is not set.
type MissingEnvError struct{ Key string }

func (e *MissingEnvError) Error() string {
	return "required environment variable " + e.Key + " is not set"
}

func errMissing(key string) error {
	return &MissingEnvError{Key: key}
}

// Redacted returns a copy safe for logging: secrets are masked.
func (c *Config) Redacted() map[string]string {
	m := map[string]string{
		"env":                      c.Env,
		"http_addr":                c.HTTPAddr,
		"articles_dir":             c.ArticlesDir,
		"llm_base_url":             c.LLM.BaseURL,
		"llm_model":                c.LLM.Model,
		"search_freshness_days":    strconv.Itoa(c.Search.DefaultFreshnessDays),
		"search_rate_limit_per_ip": strconv.Itoa(c.Search.RateLimitPerIP),
		"pdf_sidecar_url":          c.PDFSidecarURL,
		"admin_api_key_configured": strconv.FormatBool(c.AdminAPIKey != ""),
		"llm_api_key_configured":   strconv.FormatBool(c.LLM.APIKey != ""),
		"database_url_redacted":    redactDSN(c.DatabaseURL),
	}
	return m
}

func redactDSN(dsn string) string {
	// postgres://user:pass@host:port/db?params=1 -> postgres://user:****@host:port/db?params=1
	const marker = "://"
	i := strings.Index(dsn, marker)
	if i < 0 {
		return "<redacted>"
	}
	rest := dsn[i+len(marker):]
	at := strings.Index(rest, "@")
	if at < 0 {
		return dsn[:i+len(marker)] + "<redacted>"
	}
	user := rest[:at]
	colon := strings.Index(user, ":")
	if colon < 0 {
		return dsn[:i+len(marker)] + user + "@<redacted>"
	}
	return dsn[:i+len(marker)] + user[:colon] + ":****@" + rest[at+1:]
}
