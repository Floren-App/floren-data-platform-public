package provider

import (
	"context"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/setdefault"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// An alert rule: which events go to which channel, for which layers, objects and classifications.
type alertRuleResource struct{ api *client.Client }

type alertRuleModel struct {
	ID              types.String `tfsdk:"id"`
	Name            types.String `tfsdk:"name"`
	Events          types.Set    `tfsdk:"events"`
	ChannelID       types.String `tfsdk:"channel_id"`
	LayerIDs        types.Set    `tfsdk:"layer_ids"`
	ResourceIDs     types.Set    `tfsdk:"resource_ids"`
	Classifications types.Set    `tfsdk:"classifications"`
	Enabled         types.Bool   `tfsdk:"enabled"`
}

type alertRuleOut struct {
	ID              string   `json:"id"`
	Name            string   `json:"name"`
	Events          []string `json:"events"`
	ChannelID       string   `json:"channel_id"`
	LayerIDs        []string `json:"layer_ids"`
	ResourceIDs     []string `json:"resource_ids"`
	Classifications []string `json:"classifications"`
	Enabled         bool     `json:"enabled"`
}

func NewAlertRuleResource() resource.Resource { return &alertRuleResource{} }

func (r *alertRuleResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_alert_rule"
}

func (r *alertRuleResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	emptySet := func(desc string) schema.SetAttribute {
		return schema.SetAttribute{ElementType: types.StringType, Optional: true, Computed: true,
			Default: setdefault.StaticValue(setValue(nil)), Description: desc}
	}
	resp.Schema = schema.Schema{
		Description: "An alert rule: the events it sends to a channel (fdp_alert_channel), optionally only for some layers, " +
			"objects (and what is under them: a workload folder, a schema, a data product) and classifications. Events: " +
			"run.failed, deploy.failed, sla.breached, sla.recovered, access.requested, access.request_decided, review.waiting, " +
			"review.due, review.overdue, case.approved, case.declined, case.changes_requested, case.comment, membership.waiting, " +
			"access.renewal_ready, access.ending, access.ended, access.deadline, access.removed, certificate.expiring. Import by ID.",
		Attributes: map[string]schema.Attribute{
			"id":              schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"name":            schema.StringAttribute{Required: true},
			"events":          schema.SetAttribute{ElementType: types.StringType, Required: true},
			"channel_id":      schema.StringAttribute{Required: true, Description: "The channel's ID (fdp_alert_channel.id)."},
			"layer_ids":       emptySet("Layer IDs; empty: every layer."),
			"resource_ids":    emptySet("Objects, and what is under them; empty: all."),
			"classifications": emptySet("Classifications; empty: all."),
			"enabled":         schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true)},
		},
	}
}

func (r *alertRuleResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *alertRuleResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func (m *alertRuleModel) apply(out *alertRuleOut) {
	m.ID, m.Name, m.ChannelID = types.StringValue(out.ID), types.StringValue(out.Name), types.StringValue(out.ChannelID)
	m.Events, m.LayerIDs, m.ResourceIDs = setValue(out.Events), setValue(out.LayerIDs), setValue(out.ResourceIDs)
	m.Classifications, m.Enabled = setValue(out.Classifications), types.BoolValue(out.Enabled)
}

func (r *alertRuleResource) body(ctx context.Context, m *alertRuleModel, d *diagSink) map[string]any {
	return map[string]any{"name": m.Name.ValueString(), "events": stringSet(ctx, m.Events, d.d), "channel_id": m.ChannelID.ValueString(),
		"layer_ids": stringSet(ctx, m.LayerIDs, d.d), "resource_ids": stringSet(ctx, m.ResourceIDs, d.d),
		"classifications": stringSet(ctx, m.Classifications, d.d), "enabled": m.Enabled.ValueBool()}
}

func (r *alertRuleResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan alertRuleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	body := r.body(ctx, &plan, &diagSink{&resp.Diagnostics})
	if resp.Diagnostics.HasError() {
		return
	}
	var out alertRuleOut
	if err := r.api.Do(ctx, "POST", "/v1/alerts/rules", body, &out); err != nil {
		apiError(&resp.Diagnostics, "creating alert rule", err)
		return
	}
	plan.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *alertRuleResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state alertRuleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var listed []alertRuleOut
	if err := r.api.Do(ctx, "GET", "/v1/alerts/rules", nil, &listed); err != nil {
		apiError(&resp.Diagnostics, "reading alert rule", err)
		return
	}
	for i := range listed {
		if listed[i].ID == state.ID.ValueString() {
			state.apply(&listed[i])
			resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
			return
		}
	}
	resp.State.RemoveResource(ctx)
}

func (r *alertRuleResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state alertRuleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	body := r.body(ctx, &plan, &diagSink{&resp.Diagnostics})
	if resp.Diagnostics.HasError() {
		return
	}
	var out alertRuleOut
	if err := r.api.Do(ctx, "PUT", "/v1/alerts/rules/"+state.ID.ValueString(), body, &out); err != nil {
		apiError(&resp.Diagnostics, "saving alert rule", err)
		return
	}
	plan.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *alertRuleResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state alertRuleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.api.Do(ctx, "DELETE", "/v1/alerts/rules/"+state.ID.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "deleting alert rule", err)
	}
}
