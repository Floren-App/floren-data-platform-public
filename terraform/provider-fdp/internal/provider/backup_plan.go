package provider

import (
	"context"
	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64default"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type backupPlanResource struct{ api *client.Client }
type backupPlanModel struct {
	ID            types.String `tfsdk:"id"`
	Name          types.String `tfsdk:"name"`
	Enabled       types.Bool   `tfsdk:"enabled"`
	SourceKind    types.String `tfsdk:"source_kind"`
	SourceID      types.String `tfsdk:"source_id"`
	DestinationID types.String `tfsdk:"destination_id"`
	Prefix        types.String `tfsdk:"prefix"`
	Schedule      types.String `tfsdk:"schedule"`
	Timezone      types.String `tfsdk:"timezone"`
	Schemas       types.List   `tfsdk:"schemas"`
	SourcePrefix  types.String `tfsdk:"source_prefix"`
	RetentionDays types.Int64  `tfsdk:"retention_days"`
	MaxBytes      types.Int64  `tfsdk:"max_bytes"`
	MaxObjects    types.Int64  `tfsdk:"max_objects"`
	NextRunAt     types.String `tfsdk:"next_run_at"`
}
type backupPlanOut struct {
	ID            string   `json:"id"`
	Name          string   `json:"name"`
	Enabled       bool     `json:"enabled"`
	SourceKind    string   `json:"source_kind"`
	SourceID      string   `json:"source_id"`
	DestinationID string   `json:"destination_id"`
	Prefix        string   `json:"prefix"`
	Schedule      string   `json:"schedule"`
	Timezone      string   `json:"timezone"`
	Schemas       []string `json:"schemas"`
	SourcePrefix  string   `json:"source_prefix"`
	RetentionDays int64    `json:"retention_days"`
	MaxBytes      int64    `json:"max_bytes"`
	MaxObjects    int64    `json:"max_objects"`
	NextRunAt     string   `json:"next_run_at"`
}

func NewBackupPlanResource() resource.Resource { return &backupPlanResource{} }
func (r *backupPlanResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_backup_plan"
}
func (r *backupPlanResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}
func (r *backupPlanResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{Description: `Independent control-plane, PostgreSQL, S3 or snapshot-aware Iceberg v1/v2 backup plan. Source export, destination use and scheduling are separately authorized. Destroy stops scheduling and retains archives and history.`, Attributes: map[string]schema.Attribute{
		"id":             schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
		"name":           schema.StringAttribute{Required: true},
		"enabled":        schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false)},
		"source_kind":    schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("control_plane")},
		"source_id":      schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
		"destination_id": schema.StringAttribute{Required: true},
		"prefix":         schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("daily")},
		"schedule":       schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("0 2 * * *")},
		"timezone":       schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("UTC")},
		"schemas":        schema.ListAttribute{Description: "PostgreSQL schemas or exact Iceberg namespaces; empty selects all. Iceberg includes every retained snapshot in each captured table metadata version.", ElementType: types.StringType, Optional: true, Computed: true, Default: listdefault.StaticValue(types.ListValueMust(types.StringType, []attr.Value{}))},
		"source_prefix":  schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
		"retention_days": schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(30)},
		"max_bytes":      schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(10737418240)},
		"max_objects":    schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(100000)},
		"next_run_at":    schema.StringAttribute{Computed: true},
	}}
}
func (r *backupPlanResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}
func (m *backupPlanModel) apply(ctx context.Context, out *backupPlanOut, d *diagSink) {
	m.ID = types.StringValue(out.ID)
	m.Name = types.StringValue(out.Name)
	m.Enabled = types.BoolValue(out.Enabled)
	m.SourceKind = types.StringValue(out.SourceKind)
	m.SourceID = types.StringValue(out.SourceID)
	m.DestinationID = types.StringValue(out.DestinationID)
	m.Prefix = types.StringValue(out.Prefix)
	m.Schedule = types.StringValue(out.Schedule)
	m.Timezone = types.StringValue(out.Timezone)
	schemas, diagnostics := types.ListValueFrom(ctx, types.StringType, out.Schemas)
	d.d.Append(diagnostics...)
	m.Schemas = schemas
	m.SourcePrefix = types.StringValue(out.SourcePrefix)
	m.RetentionDays = types.Int64Value(out.RetentionDays)
	m.MaxBytes = types.Int64Value(out.MaxBytes)
	m.MaxObjects = types.Int64Value(out.MaxObjects)
	m.NextRunAt = types.StringValue(out.NextRunAt)
}
func (r *backupPlanResource) put(ctx context.Context, m *backupPlanModel, create bool, d *diagSink) {
	body := map[string]any{
		"name":           m.Name.ValueString(),
		"enabled":        m.Enabled.ValueBool(),
		"source_kind":    m.SourceKind.ValueString(),
		"source_id":      m.SourceID.ValueString(),
		"destination_id": m.DestinationID.ValueString(),
		"prefix":         m.Prefix.ValueString(),
		"schedule":       m.Schedule.ValueString(),
		"timezone":       m.Timezone.ValueString(),
		"source_prefix":  m.SourcePrefix.ValueString(),
		"retention_days": m.RetentionDays.ValueInt64(),
		"max_bytes":      m.MaxBytes.ValueInt64(),
		"max_objects":    m.MaxObjects.ValueInt64(),
	}
	var schemas []string
	d.d.Append(m.Schemas.ElementsAs(ctx, &schemas, false)...)
	if schemas == nil {
		schemas = []string{}
	}
	body["schemas"] = schemas
	method, endpoint := "PUT", "/v1/backups/plans/"+m.ID.ValueString()
	if create {
		method, endpoint = "POST", "/v1/backups/plans"
	}
	var out backupPlanOut
	if err := r.api.Do(ctx, method, endpoint, body, &out); err != nil {
		apiError(d.d, "saving backup_plan", err)
		return
	}
	m.apply(ctx, &out, d)
}
func (r *backupPlanResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m backupPlanModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &m, true, &diagSink{&resp.Diagnostics})
	if !resp.Diagnostics.HasError() {
		resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
	}
}
func (r *backupPlanResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m backupPlanModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &m, false, &diagSink{&resp.Diagnostics})
	if !resp.Diagnostics.HasError() {
		resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
	}
}
func (r *backupPlanResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m backupPlanModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out backupPlanOut
	if err := r.api.Do(ctx, "GET", "/v1/backups/plans/"+m.ID.ValueString(), nil, &out); err != nil {
		if client.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		apiError(&resp.Diagnostics, "reading backup_plan", err)
		return
	}
	m.apply(ctx, &out, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}
func (r *backupPlanResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m backupPlanModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.api.Do(ctx, "DELETE", "/v1/backups/plans/"+m.ID.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "removing backup_plan", err)
	}
}
