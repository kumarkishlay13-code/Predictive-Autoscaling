package main

import (
	"fmt"
	"strconv"
	"time"
)

// Config is read from environment variables so it can be set from the Helm chart.
type Config struct {
	Port             string
	StartupDelay     time.Duration // simulated warm-up before the pod reports ready
	DefaultWorkUnits int           // CPU work per request when ?units= is absent
	MaxWorkUnits     int           // upper bound accepted from ?units=
	DrainDelay       time.Duration // time between SIGTERM and closing the listener
	ShutdownTimeout  time.Duration
}

// LoadConfig builds a Config from a getenv function (os.Getenv in production).
func LoadConfig(getenv func(string) string) (Config, error) {
	cfg := Config{
		Port:             "8080",
		StartupDelay:     0,
		DefaultWorkUnits: 20,
		MaxWorkUnits:     1000,
		DrainDelay:       5 * time.Second,
		ShutdownTimeout:  10 * time.Second,
	}
	if v := getenv("PORT"); v != "" {
		cfg.Port = v
	}
	var err error
	if cfg.StartupDelay, err = seconds(getenv, "STARTUP_DELAY_SECONDS", cfg.StartupDelay); err != nil {
		return cfg, err
	}
	if cfg.DrainDelay, err = seconds(getenv, "DRAIN_DELAY_SECONDS", cfg.DrainDelay); err != nil {
		return cfg, err
	}
	if cfg.DefaultWorkUnits, err = positiveInt(getenv, "DEFAULT_WORK_UNITS", cfg.DefaultWorkUnits); err != nil {
		return cfg, err
	}
	if cfg.MaxWorkUnits, err = positiveInt(getenv, "MAX_WORK_UNITS", cfg.MaxWorkUnits); err != nil {
		return cfg, err
	}
	if cfg.DefaultWorkUnits > cfg.MaxWorkUnits {
		return cfg, fmt.Errorf("DEFAULT_WORK_UNITS (%d) exceeds MAX_WORK_UNITS (%d)",
			cfg.DefaultWorkUnits, cfg.MaxWorkUnits)
	}
	return cfg, nil
}

func seconds(getenv func(string) string, key string, def time.Duration) (time.Duration, error) {
	v := getenv(key)
	if v == "" {
		return def, nil
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil || f < 0 {
		return def, fmt.Errorf("%s must be a non-negative number of seconds, got %q", key, v)
	}
	return time.Duration(f * float64(time.Second)), nil
}

func positiveInt(getenv func(string) string, key string, def int) (int, error) {
	v := getenv(key)
	if v == "" {
		return def, nil
	}
	n, err := strconv.Atoi(v)
	if err != nil || n <= 0 {
		return def, fmt.Errorf("%s must be a positive integer, got %q", key, v)
	}
	return n, nil
}
