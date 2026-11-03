package provider

import (
	"context"
	"encoding/json"
	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestIndependentBackupPlanPreservesScheduleSourceAndPrefix(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" || r.URL.Path != "/v1/backups/plans" {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		for key, want := range map[string]any{"source_kind": "storage", "source_id": "orders", "destination_id": "offsite", "prefix": "finance/nightly", "schedule": "30 2 * * *", "timezone": "Europe/Amsterdam"} {
			if body[key] != want {
				t.Errorf("lost %s: %v", key, body[key])
			}
		}
		body["id"] = "plan-one"
		body["next_run_at"] = "2026-11-04T01:30:00Z"
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(body)
	}))
	defer server.Close()
	api, err := client.New(client.Config{Endpoint: server.URL, Token: "test"})
	if err != nil {
		t.Fatal(err)
	}
	r := backupPlanResource{api: api}
	m := backupPlanModel{Name: types.StringValue("Nightly"), SourceKind: types.StringValue("storage"), SourceID: types.StringValue("orders"), DestinationID: types.StringValue("offsite"), Prefix: types.StringValue("finance/nightly"), Schedule: types.StringValue("30 2 * * *"), Timezone: types.StringValue("Europe/Amsterdam"), Schemas: types.ListValueMust(types.StringType, []attr.Value{types.StringValue("public")})}
	var d diag.Diagnostics
	r.put(context.Background(), &m, true, &diagSink{&d})
	if d.HasError() || m.ID.ValueString() != "plan-one" || m.NextRunAt.ValueString() == "" {
		t.Fatalf("bad state: %v", d)
	}
}

func TestBackupDestinationOnlySendsVaultReference(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		if body["recovery_key_ref"] != "backups/production/key" {
			t.Error("vault reference missing")
		}
		for _, key := range []string{"private_key", "recovery_public_key", "secret_access_key", "password"} {
			if _, ok := body[key]; ok {
				t.Errorf("secret field sent: %s", key)
			}
		}
		body["id"] = "offsite"
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(body)
	}))
	defer server.Close()
	api, _ := client.New(client.Config{Endpoint: server.URL, Token: "test"})
	r := backupDestinationResource{api: api}
	m := backupDestinationModel{Name: types.StringValue("Offsite"), Bucket: types.StringValue("backup-bucket"), Prefix: types.StringValue("production"), RecoveryKeyRef: types.StringValue("backups/production/key")}
	var d diag.Diagnostics
	r.put(context.Background(), &m, true, &diagSink{&d})
	if d.HasError() {
		t.Fatal(d)
	}
}
