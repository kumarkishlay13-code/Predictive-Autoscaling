package main

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

func newTestServer(t *testing.T, cfg Config) *Server {
	t.Helper()
	return NewServer(cfg, prometheus.NewRegistry(), slog.New(slog.NewTextHandler(io.Discard, nil)))
}

func defaultConfig(t *testing.T) Config {
	t.Helper()
	cfg, err := LoadConfig(func(string) string { return "" })
	if err != nil {
		t.Fatal(err)
	}
	return cfg
}

func get(t *testing.T, h http.Handler, path string) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
	return rec
}

func TestWorkEndpoint(t *testing.T) {
	s := newTestServer(t, defaultConfig(t))
	h := s.Routes()

	if rec := get(t, h, "/work?units=1"); rec.Code != http.StatusOK {
		t.Fatalf("units=1: got %d", rec.Code)
	}
	for _, bad := range []string{"-1", "abc", "1001"} {
		if rec := get(t, h, "/work?units="+bad); rec.Code != http.StatusBadRequest {
			t.Errorf("units=%s: got %d, want 400", bad, rec.Code)
		}
	}
}

func TestBurnIsDeterministic(t *testing.T) {
	if burn(1) != burn(1) || burn(1) == burn(2) {
		t.Fatal("burn must be deterministic and depend on units")
	}
}

func TestReadinessLifecycle(t *testing.T) {
	cfg := defaultConfig(t)
	cfg.StartupDelay = 50 * time.Millisecond
	s := newTestServer(t, cfg)
	h := s.Routes()

	if rec := get(t, h, "/readyz"); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("before warm-up: got %d, want 503", rec.Code)
	}
	s.WarmUp(context.Background())
	if rec := get(t, h, "/readyz"); rec.Code != http.StatusOK {
		t.Fatalf("after warm-up: got %d, want 200", rec.Code)
	}
	s.SetDraining()
	rec := get(t, h, "/readyz")
	if rec.Code != http.StatusServiceUnavailable || !strings.Contains(rec.Body.String(), "draining") {
		t.Fatalf("draining: got %d %s", rec.Code, rec.Body.String())
	}
	if rec := get(t, h, "/healthz"); rec.Code != http.StatusOK {
		t.Fatalf("liveness must stay up while draining, got %d", rec.Code)
	}
}

func TestMetricsCountArrivalsAndCodes(t *testing.T) {
	s := newTestServer(t, defaultConfig(t))
	h := s.Routes()
	get(t, h, "/work?units=0")
	get(t, h, "/work?units=x")

	body := get(t, h, "/metrics").Body.String()
	for _, want := range []string{
		`demo_requests_received_total{handler="work"} 2`,
		`demo_http_requests_total{code="200",handler="work"} 1`,
		`demo_http_requests_total{code="400",handler="work"} 1`,
		`demo_http_request_duration_seconds_bucket`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("metrics missing %q", want)
		}
	}
}

func TestLoadConfig(t *testing.T) {
	env := map[string]string{"STARTUP_DELAY_SECONDS": "1.5", "DEFAULT_WORK_UNITS": "7", "PORT": "9000"}
	cfg, err := LoadConfig(func(k string) string { return env[k] })
	if err != nil {
		t.Fatal(err)
	}
	if cfg.StartupDelay != 1500*time.Millisecond || cfg.DefaultWorkUnits != 7 || cfg.Port != "9000" {
		t.Fatalf("unexpected config: %+v", cfg)
	}
	for k, v := range map[string]string{"STARTUP_DELAY_SECONDS": "-1", "DEFAULT_WORK_UNITS": "0",
		"MAX_WORK_UNITS": "abc"} {
		if _, err := LoadConfig(func(key string) string {
			if key == k {
				return v
			}
			return ""
		}); err == nil {
			t.Errorf("%s=%s: expected an error", k, v)
		}
	}
}

// BenchmarkBurnUnit reports the CPU time of one work unit on this machine.
func BenchmarkBurnUnit(b *testing.B) {
	for b.Loop() {
		burn(1)
	}
}
