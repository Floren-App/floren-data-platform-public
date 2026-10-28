package provider

import (
	"context"
	"encoding/json"
	"fmt"
	"reflect"

	"github.com/floren/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// configured extracts the API client handed over by the provider.
func configured(data any, diags *diag.Diagnostics) *client.Client {
	if data == nil {
		return nil
	}
	c, ok := data.(*client.Client)
	if !ok {
		diags.AddError("Unexpected provider data", fmt.Sprintf("%T", data))
	}
	return c
}

func apiError(diags *diag.Diagnostics, action string, err error) {
	diags.AddError("Floren Data Platform: "+action, err.Error())
}

// jsonSubset reports whether every value the user declared equals the server's. The server adds defaults
// (layer infrastructure, data-source configuration); those must not show up as perpetual drift.
func jsonSubset(declared, actual any) bool {
	switch d := declared.(type) {
	case map[string]any:
		a, ok := actual.(map[string]any)
		if !ok {
			return false
		}
		for key, value := range d {
			if !jsonSubset(value, a[key]) {
				return false
			}
		}
		return true
	case []any:
		a, ok := actual.([]any)
		if !ok || len(a) != len(d) {
			return false
		}
		for i := range d {
			if !jsonSubset(d[i], a[i]) {
				return false
			}
		}
		return true
	default:
		return reflect.DeepEqual(declared, actual)
	}
}

// reconcileJSON keeps the user's JSON when the server still matches it; otherwise reports the server's value.
func reconcileJSON(declared JSONValue, actual any) JSONValue {
	if !declared.IsNull() && !declared.IsUnknown() && declared.ValueString() != "" {
		var want any
		if json.Unmarshal([]byte(declared.ValueString()), &want) == nil && jsonSubset(want, actual) {
			return declared
		}
	}
	if actual == nil {
		return jsonNull()
	}
	encoded, _ := json.Marshal(actual)
	return jsonValue(string(encoded))
}

func decodeJSON(value JSONValue, diags *diag.Diagnostics, name string) map[string]any {
	out := map[string]any{}
	if value.IsNull() || value.IsUnknown() || value.ValueString() == "" {
		return out
	}
	if err := json.Unmarshal([]byte(value.ValueString()), &out); err != nil {
		diags.AddAttributeError(pathRoot(name), "Invalid JSON", "use jsonencode({...}): "+err.Error())
	}
	return out
}

func stringList(ctx context.Context, value types.List, diags *diag.Diagnostics) []string {
	out := []string{}
	if !value.IsNull() && !value.IsUnknown() {
		diags.Append(value.ElementsAs(ctx, &out, false)...)
	}
	return out
}

func stringSet(ctx context.Context, value types.Set, diags *diag.Diagnostics) []string {
	out := []string{}
	if !value.IsNull() && !value.IsUnknown() {
		diags.Append(value.ElementsAs(ctx, &out, false)...)
	}
	return out
}

func stringMap(ctx context.Context, value types.Map, diags *diag.Diagnostics) map[string]string {
	out := map[string]string{}
	if !value.IsNull() && !value.IsUnknown() {
		diags.Append(value.ElementsAs(ctx, &out, false)...)
	}
	return out
}

func listValue(values []string) types.List {
	elems := make([]attr.Value, 0, len(values))
	for _, v := range values {
		elems = append(elems, types.StringValue(v))
	}
	return types.ListValueMust(types.StringType, elems)
}

func setValue(values []string) types.Set {
	elems := make([]attr.Value, 0, len(values))
	for _, v := range values {
		elems = append(elems, types.StringValue(v))
	}
	return types.SetValueMust(types.StringType, elems)
}

func mapValue(values map[string]string) types.Map {
	elems := make(map[string]attr.Value, len(values))
	for k, v := range values {
		elems[k] = types.StringValue(v)
	}
	return types.MapValueMust(types.StringType, elems)
}

// gone removes a resource deleted outside configuration from state, so the next plan recreates it.
func gone(ctx context.Context, err error, resp *resource.ReadResponse) bool {
	if client.IsNotFound(err) {
		resp.State.RemoveResource(ctx)
		return true
	}
	return false
}

func pathRoot(name string) path.Path { return path.Root(name) }

// diagSink lets small helpers append to whichever response's diagnostics they run under.
type diagSink struct{ d *diag.Diagnostics }
