package httpapi

import (
	"fmt"
	"net"
	"net/http"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"
)

const rateLimitScript = `
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
if current > tonumber(ARGV[1]) then
  return {0, redis.call('TTL', KEYS[1])}
end
return {1, 0}
`

// rateLimiter throttles per-IP requests with a fixed-window Redis counter.
type rateLimiter struct {
	redis  *redis.Client
	prefix string
	limit  int
	window time.Duration
	next   http.Handler
}

// newRateLimiter wraps h with a per-IP fixed-window limit; when allow is
// false the wrapped handler never runs (used to exempt the poll endpoint).
func newRateLimiter(redisClient *redis.Client, prefix string, limit int, window time.Duration) func(http.Handler) http.Handler {
	if redisClient == nil || limit <= 0 {
		return func(h http.Handler) http.Handler { return h }
	}
	return func(next http.Handler) http.Handler {
		return &rateLimiter{redis: redisClient, prefix: prefix, limit: limit, window: window, next: next}
	}
}

func (l *rateLimiter) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	ip := clientIP(r)
	key := fmt.Sprintf("%s:%s", l.prefix, ip)
	res, err := l.redis.Eval(r.Context(), rateLimitScript, []string{key}, l.limit, int(l.window.Seconds())).Int64Slice()
	if err != nil {
		// Fail open on infrastructure errors, matching Django's throttle
		// behaviour of not blocking legitimate traffic on cache failures.
		l.next.ServeHTTP(w, r)
		return
	}
	if len(res) < 2 || res[0] != 1 {
		ttl := int64(0)
		if len(res) >= 2 {
			ttl = res[1]
		}
		if ttl <= 0 {
			ttl = int64(l.window.Seconds())
		}
		w.Header().Set("Retry-After", strconv.FormatInt(ttl, 10))
		writeDetailError(w, http.StatusTooManyRequests, "Request was throttled.")
		return
	}
	l.next.ServeHTTP(w, r)
}

func clientIP(r *http.Request) string {
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
		if ip := firstIP(xff); ip != "" {
			return ip
		}
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err == nil {
		return host
	}
	return r.RemoteAddr
}

func firstIP(xff string) string {
	for _, part := range splitComma(xff) {
		if part != "" {
			return part
		}
	}
	return ""
}

func splitComma(s string) []string {
	var out []string
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == ',' {
			out = append(out, trimSpace(s[start:i]))
			start = i + 1
		}
	}
	out = append(out, trimSpace(s[start:]))
	return out
}

func trimSpace(s string) string {
	start, end := 0, len(s)
	for start < end && (s[start] == ' ' || s[start] == '\t') {
		start++
	}
	for end > start && (s[end-1] == ' ' || s[end-1] == '\t') {
		end--
	}
	return s[start:end]
}
