package client

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

// Every request declares that Terraform manages what it changes, with the configuration's reference and revision.
func TestRequestsDeclareTerraformManagement(t *testing.T) {
	var got http.Header
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Clone()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{}`))
	}))
	defer server.Close()
	c, err := New(Config{Endpoint: server.URL, Token: "token", ConfigurationReference: "git.example.com/config//terraform", ConfigurationRevision: "abc1234"})
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Do(context.Background(), "PUT", "/v1/rbac/folders/x", map[string]string{"name": "x"}, nil); err != nil {
		t.Fatal(err)
	}
	for header, want := range map[string]string{"X-FDP-Managed-By": "terraform", "X-FDP-Configuration": "git.example.com/config//terraform",
		"X-FDP-Configuration-Revision": "abc1234"} {
		if got.Get(header) != want {
			t.Errorf("%s = %q, want %q", header, got.Get(header), want)
		}
	}
}
