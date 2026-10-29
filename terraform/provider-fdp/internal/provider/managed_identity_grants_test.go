package provider

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
)

func TestIdentityGrantsReconcileAndDrift(t *testing.T) {
	assignments := map[string]string{"old": "Reader", "unrelated": "Owner"}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		switch req.Method + " " + req.URL.Path {
		case "PUT /v1/rbac/resources/new/assignments":
			var body struct {
				Principal string `json:"principal_id"`
				Role      string `json:"role"`
			}
			json.NewDecoder(req.Body).Decode(&body)
			if body.Principal != "managed" || body.Role != "Contributor" {
				t.Errorf("unexpected grant: %+v", body)
			}
			assignments["new"] = body.Role
			w.WriteHeader(204)
		case "DELETE /v1/rbac/resources/old/assignments/services/managed":
			if assignments["new"] != "Contributor" {
				t.Error("old access removed before replacement was granted")
			}
			delete(assignments, "old")
			w.WriteHeader(204)
		case "GET /v1/rbac/resources/new/assignments":
			json.NewEncoder(w).Encode([]any{map[string]any{"principal_id": "managed", "role": "Reader", "inherited": false}, map[string]any{"principal_id": "other", "role": "Owner"}, map[string]any{"principal_id": "managed", "role": "Owner", "inherited": true}})
		case "GET /v1/rbac/resources/missing/assignments":
			w.WriteHeader(404)
		default:
			t.Errorf("unexpected request %s %s", req.Method, req.URL.Path)
			w.WriteHeader(500)
		}
	}))
	defer srv.Close()
	api, _ := client.New(client.Config{Endpoint: srv.URL, Token: "test"})
	r := &workloadResource{api: api}
	if err := r.reconcileIdentityGrants(context.Background(), "managed", map[string]string{"old": "Reader"}, map[string]string{"new": "Contributor"}); err != nil {
		t.Fatal(err)
	}
	if _, ok := assignments["old"]; ok {
		t.Fatal("old grant retained")
	}
	if assignments["unrelated"] != "Owner" {
		t.Fatal("unowned assignment changed")
	}
	actual, err := r.readIdentityGrants(context.Background(), "managed", map[string]string{"new": "Contributor", "missing": "Reader"})
	if err != nil {
		t.Fatal(err)
	}
	if len(actual) != 1 || actual["new"] != "Reader" {
		t.Fatalf("drift hidden: %v", actual)
	}
	if err := r.reconcileIdentityGrants(context.Background(), "", nil, map[string]string{"new": "Reader"}); err == nil {
		t.Fatal("missing identity accepted")
	}
}

func TestIdentityGrantBootstrapRetry(t *testing.T) {
	synced := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.Method == "POST" && req.URL.Path == "/v1/workloads/job/sync" {
			synced = true
			w.WriteHeader(200)
			return
		}
		if req.Method != "GET" || req.URL.Path != "/v1/workloads/job" {
			t.Errorf("unexpected request")
			w.WriteHeader(500)
			return
		}
		if synced {
			json.NewEncoder(w).Encode(map[string]any{"id": "job", "status": "live"})
		} else {
			json.NewEncoder(w).Encode(map[string]any{"id": "job", "status": "failed", "access_issue": map[string]any{"message": "missing schema permission"}})
		}
	}))
	defer srv.Close()
	api, _ := client.New(client.Config{Endpoint: srv.URL, Token: "test"})
	r := &workloadResource{api: api}
	out, err := r.waitWithIdentityGrants(context.Background(), "job", time.Second, true)
	if err != nil || out.Status != "live" || !synced {
		t.Fatalf("bootstrap retry failed: %v %+v", err, out)
	}
	synced = false
	if _, err := r.waitWithIdentityGrants(context.Background(), "job", time.Second, false); err == nil || synced {
		t.Fatal("unmanaged failure retried")
	}
}
