package provider

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

func TestBackupConfigurationPreservesDestinationAndDestroyOnlyDisables(t *testing.T) {
	calls := []string{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.Method+" "+r.URL.Path)
		if r.URL.Path != "/v1/backups/configuration" {
			t.Errorf("wrong endpoint: %s", r.URL.Path)
		}
		if r.Method == "DELETE" {
			w.WriteHeader(204)
			return
		}
		var body backupConfigurationOut
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body.Bucket != "recovery" || body.Prefix != "prod/fdp" || body.RecoveryPublicKey != "public-key" || body.SourceID != "integration" || body.ServerSideEncryption != "aws:kms" || body.KMSKeyID != "kms-key" || body.ObjectLockDays != 30 {
			t.Errorf("destination lost: %#v", body)
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(body)
	}))
	defer server.Close()
	api, err := client.New(client.Config{Endpoint: server.URL, Token: "test"})
	if err != nil {
		t.Fatal(err)
	}
	r := backupConfigurationResource{api: api}
	m := backupConfigurationModel{Enabled: types.BoolValue(true), Bucket: types.StringValue("recovery"), Prefix: types.StringValue("prod/fdp"),
		EndpointURL: types.StringValue("https://s3.example.com"), Region: types.StringValue("eu-west-1"), SourceID: types.StringValue("integration"),
		IntervalHours: types.Int64Value(12), RecoveryPublicKey: types.StringValue("public-key"), ServerSideEncryption: types.StringValue("aws:kms"),
		KMSKeyID: types.StringValue("kms-key"), ObjectLockDays: types.Int64Value(30)}
	var diagnostics diag.Diagnostics
	r.put(context.Background(), &m, &diagSink{&diagnostics})
	if diagnostics.HasError() || m.ID.ValueString() != "platform_backups" || !m.Enabled.ValueBool() {
		t.Fatalf("state error: %#v %#v", m, diagnostics)
	}
	var deleted resource.DeleteResponse
	r.Delete(context.Background(), resource.DeleteRequest{}, &deleted)
	if deleted.Diagnostics.HasError() || len(calls) != 2 || calls[1] != "DELETE /v1/backups/configuration" {
		t.Fatalf("unexpected delete: %#v %#v", calls, deleted.Diagnostics)
	}
}
