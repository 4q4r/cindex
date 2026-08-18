package config

import (
	"encoding/json"
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
		BaseURL         string
		APIKey          string
		Model           string
		Timeout         time.Duration
		Temperature     float64
		MaxQuotes       int
		Concurrency     int
		RequestInterval time.Duration
		MaxInputChars   int
		MaxToolTurns    int
		MaxPDFPages     int
		PDFDPI          int
		MaxImages       int
		ImageDetail     string
		MaxImageDim     int
		ExtraBody       map[string]any
	}

	Search struct {
		DefaultFreshnessDays int
		FinalTopK            int
		RateLimitPerIP       int
		RateLimitWindow      time.Duration
	}

	BrowserURL     string
	CoreAPIKey     string
	ExaAPIKey      string
	CrossrefMailto string
	OpenAlexAPIKey string
	UnpaywallEmail string
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

	c.LLM.BaseURL = envOrDefault("CINDEX_LLM_BASE_URL", "")
	c.LLM.APIKey = envOrDefault("CINDEX_LLM_API_KEY", "")
	c.LLM.Model = envOrDefault("CINDEX_LLM_MODEL", "")
	c.LLM.Timeout = durationOrDefault("CINDEX_LLM_TIMEOUT", 120*time.Second)
	c.LLM.Temperature = floatOrDefault("CINDEX_LLM_TEMPERATURE", 0.2)
	c.LLM.MaxQuotes = intOrDefault("CINDEX_LLM_MAX_QUOTES", 3)
	c.LLM.Concurrency = intOrDefault("CINDEX_LLM_CONCURRENCY", 4)
	c.LLM.RequestInterval = secondsOrDefault("CINDEX_LLM_MIN_REQUEST_INTERVAL", 0)
	c.LLM.MaxInputChars = intOrDefault("CINDEX_LLM_MAX_INPUT_CHARS", 12000)
	c.LLM.MaxToolTurns = intOrDefault("CINDEX_LLM_MAX_TOOL_TURNS", 6)
	c.LLM.MaxPDFPages = intOrDefault("CINDEX_LLM_MAX_PDF_PAGES", 8)
	c.LLM.PDFDPI = intOrDefault("CINDEX_LLM_PDF_DPI", 144)
	c.LLM.MaxImages = intOrDefault("CINDEX_LLM_MAX_IMAGES", 8)
	c.LLM.ImageDetail = envOrDefault("CINDEX_LLM_IMAGE_DETAIL", "high")
	c.LLM.MaxImageDim = intOrDefault("CINDEX_LLM_MAX_IMAGE_DIM", 4096)
	c.LLM.ExtraBody = jsonObjectOrDefault("CINDEX_LLM_EXTRA_BODY")

	c.Search.DefaultFreshnessDays = intOrDefault("CINDEX_SEARCH_DEFAULT_FRESHNESS_DAYS", 14)
	c.Search.FinalTopK = intOrDefault("CINDEX_SEARCH_FINAL_TOP_K", 30)
	c.Search.RateLimitPerIP = intOrDefault("CINDEX_SEARCH_RATE_LIMIT_PER_IP", 10)
	c.Search.RateLimitWindow = durationOrDefault("CINDEX_SEARCH_RATE_LIMIT_WINDOW", 60*time.Second)

	c.BrowserURL = envOrDefault("CINDEX_BROWSER_URL", "http://browser:8081")
	c.CoreAPIKey = envOrDefault("CORE_API_KEY", "")
	c.ExaAPIKey = envOrDefault("EXA_API_KEY", "")
	c.CrossrefMailto = envOrDefault("CROSSREF_MAILTO", "")
	c.OpenAlexAPIKey = envOrDefault("OPENALEX_API_KEY", "")
	c.UnpaywallEmail = envOrDefault("UNPAYWALL_EMAIL", "")

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
		if seconds, err := strconv.ParseFloat(v, 64); err == nil && seconds >= 0 {
			return time.Duration(seconds * float64(time.Second))
		}
	}
	return def
}

func floatOrDefault(key string, def float64) float64 {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.ParseFloat(v, 64); err == nil {
			return n
		}
	}
	return def
}

func secondsOrDefault(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.ParseFloat(v, 64); err == nil && n >= 0 {
			return time.Duration(n * float64(time.Second))
		}
	}
	return def
}

func jsonObjectOrDefault(key string) map[string]any {
	value := os.Getenv(key)
	if value == "" {
		return map[string]any{}
	}
	var parsed map[string]any
	if err := json.Unmarshal([]byte(value), &parsed); err != nil || parsed == nil {
		return map[string]any{}
	}
	return parsed
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
		"env":                         c.Env,
		"http_addr":                   c.HTTPAddr,
		"articles_dir":                c.ArticlesDir,
		"llm_base_url":                c.LLM.BaseURL,
		"llm_model":                   c.LLM.Model,
		"search_freshness_days":       strconv.Itoa(c.Search.DefaultFreshnessDays),
		"search_rate_limit_per_ip":    strconv.Itoa(c.Search.RateLimitPerIP),
		"admin_api_key_configured":    strconv.FormatBool(c.AdminAPIKey != ""),
		"llm_api_key_configured":      strconv.FormatBool(c.LLM.APIKey != ""),
		"core_api_key_configured":     strconv.FormatBool(c.CoreAPIKey != ""),
		"exa_api_key_configured":      strconv.FormatBool(c.ExaAPIKey != ""),
		"crossref_mailto_configured":  strconv.FormatBool(c.CrossrefMailto != ""),
		"openalex_api_key_configured": strconv.FormatBool(c.OpenAlexAPIKey != ""),
		"unpaywall_email_configured":  strconv.FormatBool(c.UnpaywallEmail != ""),
		"browser_url":                 c.BrowserURL,
		"database_url_redacted":       redactDSN(c.DatabaseURL),
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
