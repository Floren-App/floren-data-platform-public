package provider

import (
	"context"
	"encoding/json"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types/basetypes"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
)

// JSONType holds JSON settings (layer infrastructure, data-source config, API settings, SLA policy).
// A configured value equals the current one when every key it declares has the same value: server
// defaults and keys you do not manage never show as changes, but a changed declared value does.
type JSONType struct{ basetypes.StringType }

func (t JSONType) Equal(o attr.Type) bool               { _, ok := o.(JSONType); return ok }
func (t JSONType) String() string                       { return "provider.JSONType" }
func (t JSONType) ValueType(context.Context) attr.Value { return JSONValue{} }

func (t JSONType) ValueFromString(_ context.Context, in basetypes.StringValue) (basetypes.StringValuable, diag.Diagnostics) {
	return JSONValue{StringValue: in}, nil
}

func (t JSONType) ValueFromTerraform(ctx context.Context, in tftypes.Value) (attr.Value, error) {
	value, err := t.StringType.ValueFromTerraform(ctx, in)
	if err != nil {
		return nil, err
	}
	return JSONValue{StringValue: value.(basetypes.StringValue)}, nil
}

type JSONValue struct{ basetypes.StringValue }

func (v JSONValue) Type(context.Context) attr.Type { return JSONType{} }

func (v JSONValue) Equal(o attr.Value) bool {
	other, ok := o.(JSONValue)
	return ok && v.StringValue.Equal(other.StringValue)
}

// StringSemanticEquals: the receiver is the proposed (configured) value, prior is the stored one.
func (v JSONValue) StringSemanticEquals(_ context.Context, prior basetypes.StringValuable) (bool, diag.Diagnostics) {
	old, ok := prior.(JSONValue)
	if !ok || v.IsNull() || v.IsUnknown() || old.IsNull() || old.IsUnknown() {
		return false, nil
	}
	var proposed, current any
	if json.Unmarshal([]byte(v.ValueString()), &proposed) != nil || json.Unmarshal([]byte(old.ValueString()), &current) != nil {
		return false, nil
	}
	return jsonSubset(proposed, current), nil
}

func jsonValue(s string) JSONValue { return JSONValue{StringValue: basetypes.NewStringValue(s)} }
func jsonNull() JSONValue          { return JSONValue{StringValue: basetypes.NewStringNull()} }

// jsonSubsetPlan keeps the stored JSON when the configuration declares a subset of it (for example
// right after import, when state holds the server's full settings) and otherwise plans the
// configured value. Left unset, the stored value is kept.
type jsonSubsetPlan struct{}

func (jsonSubsetPlan) Description(context.Context) string {
	return "Keeps the current JSON when every configured key already has the configured value."
}
func (m jsonSubsetPlan) MarkdownDescription(ctx context.Context) string { return m.Description(ctx) }

func (jsonSubsetPlan) PlanModifyString(_ context.Context, req planmodifier.StringRequest, resp *planmodifier.StringResponse) {
	if req.State.Raw.IsNull() || req.StateValue.IsNull() || req.StateValue.IsUnknown() {
		return // creating, or nothing stored yet
	}
	if req.ConfigValue.IsNull() {
		resp.PlanValue = req.StateValue
		return
	}
	if req.ConfigValue.IsUnknown() {
		return
	}
	var configured, stored any
	if json.Unmarshal([]byte(req.ConfigValue.ValueString()), &configured) == nil &&
		json.Unmarshal([]byte(req.StateValue.ValueString()), &stored) == nil && jsonSubset(configured, stored) {
		resp.PlanValue = req.StateValue
	}
}

var jsonPlan = []planmodifier.String{jsonSubsetPlan{}}
