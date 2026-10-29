package provider

import (
	"context"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// workspaceSettingsID is the only instance: the settings belong to the installation.
const workspaceSettingsID = "workspaces"

type workspaceSettingsResource struct{ api *client.Client }

type workspaceSettingsModel struct {
	ID                types.String `tfsdk:"id"`
	Warehouse         types.String `tfsdk:"warehouse"`
	MaxRunning        types.Int64  `tfsdk:"max_running"`
	MaxRunningPerUser types.Int64  `tfsdk:"max_running_per_user"`
}

type workspaceSettingsOut struct {
	Warehouse         *string `json:"warehouse"`
	MaxRunning        *int64  `json:"max_running"`
	MaxRunningPerUser int64   `json:"max_running_per_user"`
}

func NewWorkspaceSettingsResource() resource.Resource { return &workspaceSettingsResource{} }

func (r *workspaceSettingsResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_workspace_settings"
}

func (r *workspaceSettingsResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Workspace limits and size for the whole installation (declare it once). Workspaces never count " +
			"toward a warehouse's max_concurrent. Destroy restores the installation defaults. Import with ID \"workspaces\".",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"warehouse": schema.StringAttribute{Optional: true,
				Description: "Warehouse whose CPU and memory every editor gets. Unset: the installation default size."},
			"max_running": schema.Int64Attribute{Optional: true,
				Description: "Workspaces running at once across everyone; more wait in line and start in order. Unset: no total limit."},
			"max_running_per_user": schema.Int64Attribute{Required: true,
				Description: "Workspaces one person may have running or waiting."},
		},
	}
}

func (r *workspaceSettingsResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *workspaceSettingsResource) ImportState(ctx context.Context, _ resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resp.Diagnostics.Append(resp.State.Set(ctx, &workspaceSettingsModel{ID: types.StringValue(workspaceSettingsID),
		Warehouse: types.StringNull(), MaxRunning: types.Int64Null(), MaxRunningPerUser: types.Int64Null()})...)
}

func (m *workspaceSettingsModel) apply(out *workspaceSettingsOut) {
	m.ID = types.StringValue(workspaceSettingsID)
	m.Warehouse, m.MaxRunning = types.StringNull(), types.Int64Null()
	if out.Warehouse != nil {
		m.Warehouse = types.StringValue(*out.Warehouse)
	}
	if out.MaxRunning != nil {
		m.MaxRunning = types.Int64Value(*out.MaxRunning)
	}
	m.MaxRunningPerUser = types.Int64Value(out.MaxRunningPerUser)
}

func (r *workspaceSettingsResource) put(ctx context.Context, m *workspaceSettingsModel, d *diagSink) {
	body := map[string]any{"warehouse": nil, "max_running": nil, "max_running_per_user": m.MaxRunningPerUser.ValueInt64()}
	if !m.Warehouse.IsNull() {
		body["warehouse"] = m.Warehouse.ValueString()
	}
	if !m.MaxRunning.IsNull() {
		body["max_running"] = m.MaxRunning.ValueInt64()
	}
	var out workspaceSettingsOut
	if err := r.api.Do(ctx, "PUT", "/v1/admin/settings/workspaces", body, &out); err != nil {
		apiError(d.d, "saving workspace settings", err)
		return
	}
	m.apply(&out)
}

func (r *workspaceSettingsResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan workspaceSettingsModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &plan, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *workspaceSettingsResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state workspaceSettingsModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var out workspaceSettingsOut
	if err := r.api.Do(ctx, "GET", "/v1/admin/settings/workspaces", nil, &out); err != nil {
		apiError(&resp.Diagnostics, "reading workspace settings", err)
		return
	}
	state.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *workspaceSettingsResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan workspaceSettingsModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &plan, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *workspaceSettingsResource) Delete(ctx context.Context, _ resource.DeleteRequest, resp *resource.DeleteResponse) {
	if err := r.api.Do(ctx, "DELETE", "/v1/admin/settings/workspaces", nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "resetting workspace settings", err)
	}
}
