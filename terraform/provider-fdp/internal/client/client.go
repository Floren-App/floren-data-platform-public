// Package client talks to the Floren Data Platform control-plane API with a bearer token: either a fixed token or one
// obtained (and renewed) with OAuth client credentials from the identity provider.
package client

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

type Config struct {
	Endpoint     string // e.g. https://fdp.example.com/api
	Token        string
	TokenURL     string
	ClientID     string
	ClientSecret string
	CAFile       string
	// Explicit owner group for created objects; automation never depends on a person's sidebar selection.
	ActiveGroupID string
}

type Client struct {
	base        string
	http        *http.Client
	cfg         Config
	mu          sync.Mutex
	token       string
	expires     time.Time
	activeGroup string
}

// APIError carries the HTTP status and the API's own explanation, which is safe to show.
type APIError struct {
	Status int
	Detail string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("Floren Data Platform API returned %d: %s", e.Status, e.Detail)
}

func IsNotFound(err error) bool {
	var apiErr *APIError
	return errors.As(err, &apiErr) && apiErr.Status == http.StatusNotFound
}

func New(cfg Config) (*Client, error) {
	if cfg.Endpoint == "" {
		return nil, errors.New("endpoint is required")
	}
	if cfg.Token == "" && (cfg.TokenURL == "" || cfg.ClientID == "" || cfg.ClientSecret == "") {
		return nil, errors.New("set token, or token_url with client_id and client_secret")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if cfg.CAFile != "" {
		pem, err := os.ReadFile(cfg.CAFile)
		if err != nil {
			return nil, fmt.Errorf("reading ca_file: %w", err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pem) {
			return nil, errors.New("ca_file contains no PEM certificates")
		}
		transport.TLSClientConfig = &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}
	}
	return &Client{base: strings.TrimRight(cfg.Endpoint, "/"), cfg: cfg, activeGroup: cfg.ActiveGroupID,
		http: &http.Client{Timeout: 60 * time.Second, Transport: transport}}, nil
}

func (c *Client) bearer(ctx context.Context) (string, error) {
	if c.cfg.Token != "" {
		return c.cfg.Token, nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.token != "" && time.Now().Before(c.expires) {
		return c.token, nil
	}
	form := url.Values{"grant_type": {"client_credentials"}, "client_id": {c.cfg.ClientID}, "client_secret": {c.cfg.ClientSecret}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.cfg.TokenURL, strings.NewReader(form.Encode()))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := c.http.Do(req)
	if err != nil {
		return "", fmt.Errorf("requesting an access token: %w", err)
	}
	defer resp.Body.Close()
	var out struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int    `json:"expires_in"`
	}
	if resp.StatusCode != http.StatusOK || json.NewDecoder(resp.Body).Decode(&out) != nil || out.AccessToken == "" {
		// Never include the response body: it may echo client details.
		return "", fmt.Errorf("the identity provider refused the client credentials (HTTP %d)", resp.StatusCode)
	}
	lifetime := time.Duration(out.ExpiresIn) * time.Second
	if lifetime <= 0 {
		lifetime = 5 * time.Minute
	}
	c.token, c.expires = out.AccessToken, time.Now().Add(lifetime*3/4)
	return c.token, nil
}

// Do sends a JSON request. out may be nil. A 404 is returned as *APIError so callers can treat it as "gone".
func (c *Client) Do(ctx context.Context, method, path string, in, out any) error {
	var body io.Reader
	if in != nil {
		data, err := json.Marshal(in)
		if err != nil {
			return err
		}
		body = bytes.NewReader(data)
	}
	for attempt := 0; ; attempt++ {
		token, err := c.bearer(ctx)
		if err != nil {
			return err
		}
		req, err := http.NewRequestWithContext(ctx, method, c.base+path, body)
		if err != nil {
			return err
		}
		req.Header.Set("Authorization", "Bearer "+token)
		req.Header.Set("Accept", "application/json")
		if in != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		if c.activeGroup != "" {
			req.Header.Set("X-FDP-Active-Group", c.activeGroup)
		}
		resp, err := c.http.Do(req)
		if err != nil {
			return err
		}
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
		resp.Body.Close()
		// The platform may restart during an upgrade; retry briefly on gateway/unavailable answers.
		if (resp.StatusCode == 502 || resp.StatusCode == 503 || resp.StatusCode == 504) && attempt < 5 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt+1) * 2 * time.Second):
			}
			if seeker, ok := body.(io.Seeker); ok {
				_, _ = seeker.Seek(0, io.SeekStart)
			}
			continue
		}
		if resp.StatusCode >= 300 {
			return &APIError{Status: resp.StatusCode, Detail: detail(data)}
		}
		if out != nil && len(data) > 0 {
			return json.Unmarshal(data, out)
		}
		return nil
	}
}

func detail(data []byte) string {
	var body struct {
		Detail any `json:"detail"`
	}
	if json.Unmarshal(data, &body) == nil && body.Detail != nil {
		switch d := body.Detail.(type) {
		case string:
			return d
		case map[string]any:
			if msg, ok := d["message"].(string); ok {
				if errs, ok := d["errors"]; ok {
					encoded, _ := json.Marshal(errs)
					return msg + " " + string(encoded)
				}
				return msg
			}
		}
		encoded, _ := json.Marshal(body.Detail)
		return string(encoded)
	}
	if len(data) > 300 {
		data = data[:300]
	}
	return strings.TrimSpace(string(data))
}
