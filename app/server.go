package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"net/http"
	"strconv"
	"sync/atomic"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// hashesPerUnit sets the CPU cost of one work unit. One unit is a fixed amount of work, not a
// fixed duration, so a request costs the same CPU whether or not the pod is throttled; under
// overload, latency grows instead. Measure the real cost with `go test -bench .` and the
// capacity test.
const hashesPerUnit = 2000

const (
	stateWarming int32 = iota
	stateReady
	stateDraining
)

// Server holds the HTTP handlers, readiness state and metrics.
type Server struct {
	cfg    Config
	logger *slog.Logger
	state  atomic.Int32

	received *prometheus.CounterVec
	requests *prometheus.CounterVec
	duration *prometheus.HistogramVec
	inFlight prometheus.Gauge
	ready    prometheus.Gauge
	registry *prometheus.Registry
}

// NewServer registers metrics on reg and returns a server in the warming state.
func NewServer(cfg Config, reg *prometheus.Registry, logger *slog.Logger) *Server {
	s := &Server{
		cfg:      cfg,
		logger:   logger,
		registry: reg,
		// Counted on arrival, before any work, so it measures offered load even when the pod
		// is saturated. The forecaster reads this metric.
		received: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "demo_requests_received_total",
			Help: "Requests received by the work endpoint, counted on arrival.",
		}, []string{"handler"}),
		requests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "demo_http_requests_total",
			Help: "Completed HTTP requests by handler and status code.",
		}, []string{"handler", "code"}),
		duration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "demo_http_request_duration_seconds",
			Help:    "Server-side request latency.",
			Buckets: []float64{.005, .01, .025, .05, .075, .1, .15, .2, .3, .5, .75, 1, 2, 5, 10},
		}, []string{"handler"}),
		inFlight: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "demo_http_in_flight_requests",
			Help: "Requests currently being served.",
		}),
		ready: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "demo_ready",
			Help: "1 when the pod reports ready, else 0.",
		}),
	}
	reg.MustRegister(s.received, s.requests, s.duration, s.inFlight, s.ready,
		collectors.NewGoCollector(), collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
	return s
}

// WarmUp waits StartupDelay (the simulated warm-up of a real service) and then marks ready.
func (s *Server) WarmUp(ctx context.Context) {
	if s.cfg.StartupDelay > 0 {
		select {
		case <-time.After(s.cfg.StartupDelay):
		case <-ctx.Done():
			return
		}
	}
	if s.state.CompareAndSwap(stateWarming, stateReady) {
		s.ready.Set(1)
		s.logger.Info("ready")
	}
}

// SetDraining makes readiness fail so the pod stops receiving new traffic.
func (s *Server) SetDraining() {
	s.state.Store(stateDraining)
	s.ready.Set(0)
}

// Routes returns the HTTP handler tree.
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.Handle("GET /work", s.instrument("work", http.HandlerFunc(s.handleWork)))
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /readyz", s.handleReady)
	mux.Handle("GET /metrics", promhttp.HandlerFor(s.registry, promhttp.HandlerOpts{}))
	return mux
}

func (s *Server) handleReady(w http.ResponseWriter, _ *http.Request) {
	switch s.state.Load() {
	case stateReady:
		writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
	case stateDraining:
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "draining"})
	default:
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "warming"})
	}
}

func (s *Server) handleWork(w http.ResponseWriter, r *http.Request) {
	units := s.cfg.DefaultWorkUnits
	if v := r.URL.Query().Get("units"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 || n > s.cfg.MaxWorkUnits {
			writeJSON(w, http.StatusBadRequest, map[string]string{
				"error": "units must be an integer between 0 and " + strconv.Itoa(s.cfg.MaxWorkUnits)})
			return
		}
		units = n
	}
	writeJSON(w, http.StatusOK, map[string]any{"units": units, "digest": burn(units)})
}

// burn performs `units` of deterministic CPU work and returns a digest so it cannot be
// optimised away.
func burn(units int) string {
	sum := sha256.Sum256([]byte("pas"))
	for i := 0; i < units*hashesPerUnit; i++ {
		sum = sha256.Sum256(sum[:])
	}
	return hex.EncodeToString(sum[:8])
}

// instrument records arrival, completion, status code and latency for a handler.
func (s *Server) instrument(name string, next http.Handler) http.Handler {
	received := s.received.WithLabelValues(name)
	duration := s.duration.WithLabelValues(name)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		received.Inc()
		s.inFlight.Inc()
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, code: http.StatusOK}
		next.ServeHTTP(rec, r)
		duration.Observe(time.Since(start).Seconds())
		s.requests.WithLabelValues(name, strconv.Itoa(rec.code)).Inc()
		s.inFlight.Dec()
	})
}

type statusRecorder struct {
	http.ResponseWriter
	code int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.code = code
	r.ResponseWriter.WriteHeader(code)
}

func writeJSON(w http.ResponseWriter, code int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(body)
}
