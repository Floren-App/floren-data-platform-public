package provider

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

func permissionPlan() permissionAssignmentModel {
	return permissionAssignmentModel{ID: types.StringNull(), TargetID: types.StringValue("set-network"), Namespace: types.StringValue("network.editor_allowlist"),
		LayerID: types.StringValue("dev-layer"), AppGroupID: types.StringValue("engineers"), PrincipalID: types.StringNull(), Role: types.StringValue("Manager"),
		Actions: types.SetNull(types.StringType), CustomRoleID: types.StringNull()}
}

func TestNamedPermissionAssignmentPreservesScopedState(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" || r.URL.Path != "/v1/permissions/assignments" {
			t.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body["layer_id"] != "dev-layer" || body["target_id"] != "set-network" || body["role"] != "Manager" {
			t.Errorf("scope lost: %#v", body)
		}
		if _, present := body["principal_id"]; present {
			t.Error("null recipient should not be serialized")
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"id": "grant-id", "target_id": "set-network", "namespace": "network.editor_allowlist",
			"layer_id": "dev-layer", "app_group_id": "engineers", "principal_id": nil, "role": "Manager", "actions": []string{}, "custom_role_id": nil})
	}))
	defer server.Close()
	api, err := client.New(client.Config{Endpoint: server.URL, Token: "test"})
	if err != nil {
		t.Fatal(err)
	}
	r := permissionAssignmentResource{api: api}
	m := permissionPlan()
	var d diag.Diagnostics
	r.save(context.Background(), &m, true, &d)
	if d.HasError() {
		t.Fatal(d)
	}
	if m.ID.ValueString() != "grant-id" || m.LayerID.ValueString() != "dev-layer" || !m.Actions.IsNull() || !m.PrincipalID.IsNull() {
		t.Fatalf("unexpected state: %#v", m)
	}
}

func TestNamedPermissionRejectsAmbiguousRecipientOrBundle(t *testing.T) {
	m := permissionPlan()
	m.PrincipalID = types.StringValue("person")
	var d diag.Diagnostics
	m.body(context.Background(), &d)
	if !d.HasError() {
		t.Fatal("accepted two recipients")
	}
	m = permissionPlan()
	m.CustomRoleID = types.StringValue("custom")
	d = nil
	m.body(context.Background(), &d)
	if !d.HasError() {
		t.Fatal("accepted two bundle selections")
	}
}

func TestEgressScopeIsPresentOnReadWriteDeleteEndpoint(t *testing.T) {
	global := egressAllowlistModel{LayerID: types.StringNull()}
	layer := egressAllowlistModel{LayerID: types.StringValue("dev-layer")}
	if global.endpoint() != "/v1/admin/settings/egress" {
		t.Fatal(global.endpoint())
	}
	if layer.endpoint() != "/v1/admin/settings/egress?layer_id=dev-layer" {
		t.Fatal(layer.endpoint())
	}
}
