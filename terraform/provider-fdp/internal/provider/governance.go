package provider

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"strings"

	"github.com/floren/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/setdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// catalogAssetID mirrors the platform: an object's catalog entry is derived from its resource ID.
func catalogAssetID(resourceID string) string {
	sum := sha256.Sum256([]byte(resourceID + "\x00"))
	return hex.EncodeToString(sum[:])[:32]
}

type assetOut struct {
	ID         string         `json:"id"`
	Governance map[string]any `json:"governance"`
	SLA        map[string]any `json:"sla"`
}

func assetTarget(m types.String) string { return catalogAssetID(m.ValueString()) }

var replaceString = []planmodifier.String{stringplanmodifier.RequiresReplace()}
var keepComputed = []planmodifier.String{stringplanmodifier.UseStateForUnknown()}

// ---- catalog governance --------------------------------------------------------------------------

type governanceResource struct{ api *client.Client }

type governanceModel struct {
	ID             types.String `tfsdk:"id"`
	ResourceID     types.String `tfsdk:"resource_id"`
	Description    types.String `tfsdk:"description"`
	Domain         types.String `tfsdk:"domain"`
	OwnerGroupID   types.String `tfsdk:"owner_group_id"`
	Steward        types.String `tfsdk:"steward"`
	Classification types.String `tfsdk:"classification"`
	Tags           types.Set    `tfsdk:"tags"`
	Status         types.String `tfsdk:"status"`
}

func NewGovernanceResource() resource.Resource { return &governanceResource{} }

func (r *governanceResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_catalog_governance"
}

func (r *governanceResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Catalog documentation and stewardship of one governed object (schema, workload, API...). " +
			"Certification requires description, domain, owner group, steward and classification. Destroy resets it. Import by resource ID.",
		Attributes: map[string]schema.Attribute{
			"id":             schema.StringAttribute{Computed: true, PlanModifiers: keepComputed, Description: "Catalog asset ID."},
			"resource_id":    schema.StringAttribute{Required: true, PlanModifiers: replaceString},
			"description":    schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"domain":         schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"owner_group_id": schema.StringAttribute{Optional: true, Description: "Accountable app group."},
			"steward":        schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"classification": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("unclassified"),
				Description: "unclassified, public, internal, confidential or restricted."},
			"tags":   schema.SetAttribute{ElementType: types.StringType, Optional: true, Computed: true, Default: setdefault.StaticValue(setValue(nil))},
			"status": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("draft"), Description: "draft, reviewed, certified or deprecated."},
		},
	}
}

func (r *governanceResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *governanceResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("resource_id"), req, resp)
}

func (m *governanceModel) apply(g map[string]any) {
	str := func(k, def string) types.String {
		if v, ok := g[k].(string); ok {
			return types.StringValue(v)
		}
		return types.StringValue(def)
	}
	m.ID = types.StringValue(assetTarget(m.ResourceID))
	m.Description, m.Domain, m.Steward = str("description", ""), str("domain", ""), str("steward", "")
	m.Classification, m.Status = str("classification", "unclassified"), str("status", "draft")
	m.OwnerGroupID = types.StringNull()
	if v, ok := g["owner_group_id"].(string); ok && v != "" {
		m.OwnerGroupID = types.StringValue(v)
	}
	tags := []string{}
	if list, ok := g["tags"].([]any); ok {
		for _, t := range list {
			if s, ok := t.(string); ok {
				tags = append(tags, s)
			}
		}
	}
	m.Tags = setValue(tags)
}

func (r *governanceResource) put(ctx context.Context, m *governanceModel, d *diagSink) {
	body := map[string]any{"description": m.Description.ValueString(), "domain": m.Domain.ValueString(),
		"steward": m.Steward.ValueString(), "classification": m.Classification.ValueString(),
		"tags": stringSet(ctx, m.Tags, d.d), "status": m.Status.ValueString(), "owner_group_id": nil}
	if !m.OwnerGroupID.IsNull() && m.OwnerGroupID.ValueString() != "" {
		body["owner_group_id"] = m.OwnerGroupID.ValueString()
	}
	var out assetOut
	if err := r.api.Do(ctx, "PUT", "/v1/catalog/assets/"+assetTarget(m.ResourceID)+"/governance", body, &out); err != nil {
		apiError(d.d, "saving catalog governance", err)
		return
	}
	m.apply(out.Governance)
}

func (r *governanceResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan governanceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &plan, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *governanceResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state governanceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var out assetOut
	if err := r.api.Do(ctx, "GET", "/v1/catalog/assets/"+assetTarget(state.ResourceID), nil, &out); err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading catalog governance", err)
		}
		return
	}
	state.apply(out.Governance)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *governanceResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan governanceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	r.put(ctx, &plan, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *governanceResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state governanceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	err := r.api.Do(ctx, "PUT", "/v1/catalog/assets/"+assetTarget(state.ResourceID)+"/governance", map[string]any{}, nil)
	if err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "resetting catalog governance", err)
	}
}

// ---- SLA policy ---------------------------------------------------------------------------------

type slaResource struct{ api *client.Client }

type slaModel struct {
	ID         types.String `tfsdk:"id"`
	ResourceID types.String `tfsdk:"resource_id"`
	Policy     JSONValue    `tfsdk:"policy"`
}

func NewSLAResource() resource.Resource { return &slaResource{} }

func (r *slaResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_sla_policy"
}

func (r *slaResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Service level of one object: freshness for datasets; success rate, deadlines, input gates and " +
			"enforcement for deployed workloads. Destroy removes the policy. Import by resource ID.",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Computed: true, PlanModifiers: keepComputed, Description: "Catalog asset ID."},
			"resource_id": schema.StringAttribute{Required: true, PlanModifiers: replaceString},
			"policy": schema.StringAttribute{Optional: true, Computed: true, CustomType: JSONType{}, PlanModifiers: jsonPlan, Description: "jsonencode({ freshness_minutes, success_rate_percent, " +
				"window_hours, deadline_time, timezone, weekdays, required_assets, enforcement, ... }). Only declared keys are compared."},
		},
	}
}

func (r *slaResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *slaResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("resource_id"), req, resp)
}

func (r *slaResource) put(ctx context.Context, m *slaModel, d *diagSink) {
	body := decodeJSON(m.Policy, d.d, "policy")
	if d.d.HasError() {
		return
	}
	var out assetOut
	if err := r.api.Do(ctx, "PUT", "/v1/catalog/assets/"+assetTarget(m.ResourceID)+"/sla", body, &out); err != nil {
		apiError(d.d, "saving SLA policy", err)
		return
	}
	m.ID = types.StringValue(assetTarget(m.ResourceID))
	m.Policy = reconcileJSON(m.Policy, withoutKeys(out.SLA, "starts_at"))
}

func withoutKeys(in map[string]any, keys ...string) map[string]any {
	out := map[string]any{}
	for k, v := range in {
		out[k] = v
	}
	for _, k := range keys {
		delete(out, k)
	}
	return out
}

func (r *slaResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan slaModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if plan.Policy.IsNull() || plan.Policy.IsUnknown() {
		resp.Diagnostics.AddAttributeError(pathRoot("policy"), "Missing policy", "policy is required: jsonencode({...}).")
	}
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &plan, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *slaResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state slaModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var out assetOut
	if err := r.api.Do(ctx, "GET", "/v1/catalog/assets/"+assetTarget(state.ResourceID), nil, &out); err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading SLA policy", err)
		}
		return
	}
	if len(out.SLA) == 0 {
		resp.State.RemoveResource(ctx)
		return
	}
	state.ID = types.StringValue(out.ID)
	state.Policy = reconcileJSON(state.Policy, withoutKeys(out.SLA, "starts_at"))
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *slaResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan slaModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	r.put(ctx, &plan, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *slaResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state slaModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	err := r.api.Do(ctx, "PUT", "/v1/catalog/assets/"+assetTarget(state.ResourceID)+"/sla", map[string]any{}, nil)
	if err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "removing SLA policy", err)
	}
}

// ---- KPI / quality metric -----------------------------------------------------------------------

type metricResource struct{ api *client.Client }

type metricModel struct {
	ID           types.String  `tfsdk:"id"`
	ResourceID   types.String  `tfsdk:"resource_id"`
	Key          types.String  `tfsdk:"key"`
	Name         types.String  `tfsdk:"name"`
	Kind         types.String  `tfsdk:"kind"`
	Description  types.String  `tfsdk:"description"`
	Unit         types.String  `tfsdk:"unit"`
	Source       types.String  `tfsdk:"source"`
	SQL          types.String  `tfsdk:"sql"`
	Dependencies types.List    `tfsdk:"dependencies"`
	Minimum      types.Float64 `tfsdk:"minimum"`
	Maximum      types.Float64 `tfsdk:"maximum"`
	WarnMinimum  types.Float64 `tfsdk:"warn_minimum"`
	WarnMaximum  types.Float64 `tfsdk:"warn_maximum"`
	Enabled      types.Bool    `tfsdk:"enabled"`
	Version      types.Int64   `tfsdk:"version"`
}

type metricOut struct {
	ID          string         `json:"id"`
	Key         string         `json:"key"`
	Name        string         `json:"name"`
	Kind        string         `json:"kind"`
	Description string         `json:"description"`
	Unit        string         `json:"unit"`
	Definition  map[string]any `json:"definition"`
	Version     int64          `json:"version"`
	Enabled     bool           `json:"enabled"`
}

func NewMetricResource() resource.Resource { return &metricResource{} }

func (r *metricResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_metric"
}

func (r *metricResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	opt := func(desc string) schema.Float64Attribute {
		return schema.Float64Attribute{Optional: true, Description: desc}
	}
	resp.Schema = schema.Schema{
		Description: "A KPI or quality metric on a data product, optionally measured by a read-only SQL query. " +
			"Every change creates a new version. Destroy disables it (history is kept). Import as <resource_id>/<key>.",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"resource_id": schema.StringAttribute{Required: true, PlanModifiers: replaceString},
			"key":         schema.StringAttribute{Required: true, PlanModifiers: replaceString},
			"name":        schema.StringAttribute{Required: true},
			"kind":        schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("kpi"), Description: "kpi or quality."},
			"description": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"unit":        schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"source":      schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString(""), Description: "SQL data source (same layer)."},
			"sql":         schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString(""), Description: "One read-only SELECT."},
			"dependencies": schema.ListAttribute{ElementType: types.StringType, Optional: true, Computed: true,
				Default: listdefault.StaticValue(listValue(nil)), Description: "Catalog asset IDs the measure reads."},
			"minimum":      opt("Failure below this value."),
			"maximum":      opt("Failure above this value."),
			"warn_minimum": opt("Warning below this value."),
			"warn_maximum": opt("Warning above this value."),
			"enabled":      schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true)},
			"version":      schema.Int64Attribute{Computed: true},
		},
	}
}

func (r *metricResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *metricResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resourceID, key, ok := strings.Cut(req.ID, "/")
	if !ok {
		resp.Diagnostics.AddError("Invalid import ID", "use <resource_id>/<key>")
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("resource_id"), resourceID)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("key"), key)...)
}

func floatPtr(v types.Float64) any {
	if v.IsNull() || v.IsUnknown() {
		return nil
	}
	return v.ValueFloat64()
}

func floatValue(v any) types.Float64 {
	if f, ok := v.(float64); ok {
		return types.Float64Value(f)
	}
	return types.Float64Null()
}

func (m *metricModel) apply(out *metricOut) {
	m.ID, m.Key, m.Name, m.Kind = types.StringValue(out.ID), types.StringValue(out.Key), types.StringValue(out.Name), types.StringValue(out.Kind)
	m.Description, m.Unit, m.Enabled, m.Version = types.StringValue(out.Description), types.StringValue(out.Unit), types.BoolValue(out.Enabled), types.Int64Value(out.Version)
	src, _ := out.Definition["source"].(string)
	sql, _ := out.Definition["sql"].(string)
	m.Source, m.SQL = types.StringValue(src), types.StringValue(sql)
	deps := []string{}
	if list, ok := out.Definition["dependencies"].([]any); ok {
		for _, d := range list {
			if s, ok := d.(string); ok && s != assetTarget(m.ResourceID) { // the server always adds the product itself
				deps = append(deps, s)
			}
		}
	}
	m.Dependencies = listValue(deps)
	m.Minimum, m.Maximum = floatValue(out.Definition["minimum"]), floatValue(out.Definition["maximum"])
	m.WarnMinimum, m.WarnMaximum = floatValue(out.Definition["warn_minimum"]), floatValue(out.Definition["warn_maximum"])
}

func (r *metricResource) put(ctx context.Context, m *metricModel, enabled bool, d *diagSink) {
	body := map[string]any{"key": m.Key.ValueString(), "name": m.Name.ValueString(), "kind": m.Kind.ValueString(),
		"description": m.Description.ValueString(), "unit": m.Unit.ValueString(), "source": m.Source.ValueString(),
		"sql": m.SQL.ValueString(), "dependencies": stringList(ctx, m.Dependencies, d.d), "enabled": enabled,
		"minimum": floatPtr(m.Minimum), "maximum": floatPtr(m.Maximum),
		"warn_minimum": floatPtr(m.WarnMinimum), "warn_maximum": floatPtr(m.WarnMaximum)}
	var out metricOut
	if err := r.api.Do(ctx, "POST", "/v1/catalog/assets/"+assetTarget(m.ResourceID)+"/metrics", body, &out); err != nil {
		apiError(d.d, "saving metric", err)
		return
	}
	m.apply(&out)
}

func (r *metricResource) find(ctx context.Context, m *metricModel) (*metricOut, error) {
	var all []metricOut
	if err := r.api.Do(ctx, "GET", "/v1/catalog/assets/"+assetTarget(m.ResourceID)+"/metrics", nil, &all); err != nil {
		return nil, err
	}
	for i := range all {
		if all[i].Key == m.Key.ValueString() {
			return &all[i], nil
		}
	}
	return nil, &client.APIError{Status: 404, Detail: "metric not found"}
}

func (r *metricResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan metricModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &plan, plan.Enabled.ValueBool(), &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *metricResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state metricModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	out, err := r.find(ctx, &state)
	if err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading metric", err)
		}
		return
	}
	state.apply(out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *metricResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan metricModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	r.put(ctx, &plan, plan.Enabled.ValueBool(), &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *metricResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state metricModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	// Definitions keep their measurement history; disabling retires them.
	r.put(ctx, &state, false, &diagSink{&resp.Diagnostics})
}
