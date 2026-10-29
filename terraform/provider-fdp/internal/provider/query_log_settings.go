package provider

import (
	"context"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64default"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// queryLogSettingsID is the only instance: the settings belong to the installation.
const queryLogSettingsID = "query_log"

type queryLogSettingsResource struct{ api *client.Client }

type queryLogSettingsModel struct {
	ID            types.String `tfsdk:"id"`
	RetentionDays types.Int64  `tfsdk:"retention_days"`
	RecordSQL     types.Bool   `tfsdk:"record_sql"`
}

type queryLogSettingsOut struct {
	RetentionDays int64 `json:"retention_days"`
	RecordSQL     bool  `json:"record_sql"`
}

func NewQueryLogSettingsResource() resource.Resource { return &queryLogSettingsResource{} }

func (r *queryLogSettingsResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_query_log_settings"
}

func (r *queryLogSettingsResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "The query log of every query on the layers' Trino (who or what ran it, tables, duration, result); " +
			"declare once. Destroy restores the defaults (90 days, no SQL text). Import with ID \"query_log\".",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"retention_days": schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(90),
				Description: "Rows older than this are removed."},
			"record_sql": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false),
				Description: "Keep each query's SQL text. Off by default: queries can contain literal values such as personal data."},
		},
	}
}

func (r *queryLogSettingsResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *queryLogSettingsResource) ImportState(ctx context.Context, _ resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resp.Diagnostics.Append(resp.State.Set(ctx, &queryLogSettingsModel{ID: types.StringValue(queryLogSettingsID),
		RetentionDays: types.Int64Null(), RecordSQL: types.BoolNull()})...)
}

func (m *queryLogSettingsModel) apply(out *queryLogSettingsOut) {
	m.ID, m.RetentionDays, m.RecordSQL = types.StringValue(queryLogSettingsID), types.Int64Value(out.RetentionDays), types.BoolValue(out.RecordSQL)
}

func (r *queryLogSettingsResource) put(ctx context.Context, m *queryLogSettingsModel, d *diagSink) {
	var out queryLogSettingsOut
	body := map[string]any{"retention_days": m.RetentionDays.ValueInt64(), "record_sql": m.RecordSQL.ValueBool()}
	if err := r.api.Do(ctx, "PUT", "/v1/admin/settings/query-log", body, &out); err != nil {
		apiError(d.d, "saving query log settings", err)
		return
	}
	m.apply(&out)
}

func (r *queryLogSettingsResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan queryLogSettingsModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &plan, &diagSink{&resp.Diagnostics})
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *queryLogSettingsResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state queryLogSettingsModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var out queryLogSettingsOut
	if err := r.api.Do(ctx, "GET", "/v1/admin/settings/query-log", nil, &out); err != nil {
		apiError(&resp.Diagnostics, "reading query log settings", err)
		return
	}
	state.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *queryLogSettingsResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan queryLogSettingsModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &plan, &diagSink{&resp.Diagnostics})
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *queryLogSettingsResource) Delete(ctx context.Context, _ resource.DeleteRequest, resp *resource.DeleteResponse) {
	if err := r.api.Do(ctx, "DELETE", "/v1/admin/settings/query-log", nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "resetting query log settings", err)
	}
}
