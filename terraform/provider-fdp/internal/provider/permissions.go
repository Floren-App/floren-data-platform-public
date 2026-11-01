package provider

import (
	"context"
	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type permissionAssignmentResource struct{ api *client.Client }
type permissionAssignmentModel struct {
	ID           types.String `tfsdk:"id"`
	TargetID     types.String `tfsdk:"target_id"`
	Namespace    types.String `tfsdk:"namespace"`
	LayerID      types.String `tfsdk:"layer_id"`
	AppGroupID   types.String `tfsdk:"app_group_id"`
	PrincipalID  types.String `tfsdk:"principal_id"`
	Role         types.String `tfsdk:"role"`
	Actions      types.Set    `tfsdk:"actions"`
	CustomRoleID types.String `tfsdk:"custom_role_id"`
}
type permissionAssignmentOut struct {
	ID           string   `json:"id"`
	TargetID     string   `json:"target_id"`
	Namespace    string   `json:"namespace"`
	LayerID      *string  `json:"layer_id"`
	AppGroupID   *string  `json:"app_group_id"`
	PrincipalID  *string  `json:"principal_id"`
	Role         *string  `json:"role"`
	Actions      []string `json:"actions"`
	CustomRoleID *string  `json:"custom_role_id"`
}

func NewPermissionAssignmentResource() resource.Resource { return &permissionAssignmentResource{} }
func (r *permissionAssignmentResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_permission_assignment"
}
func (r *permissionAssignmentResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{Description: "An explicit named capability grant on a target, globally or in one layer. Import by assignment ID. Choose exactly one recipient and exactly one role, actions set or custom role. A page gate uses namespace pages and the stable page ID as target_id.", Attributes: map[string]schema.Attribute{
		"id":             schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
		"target_id":      schema.StringAttribute{Required: true, Description: "Stable target resource or collection ID; page IDs include set-network and set-forms."},
		"namespace":      schema.StringAttribute{Required: true, Description: "Capability domain, e.g. pages, network.editor_allowlist, compliance.forms."},
		"layer_id":       schema.StringAttribute{Optional: true, Description: "Omit for explicit global scope. Use a layer ID for that layer, or * for explicit all-layer authority (Global Admin only). Global never implicitly includes a layer."},
		"app_group_id":   schema.StringAttribute{Optional: true},
		"principal_id":   schema.StringAttribute{Optional: true},
		"role":           schema.StringAttribute{Optional: true, Description: "Viewer, Executor, Contributor, Manager, Admin or Owner."},
		"actions":        schema.SetAttribute{Optional: true, ElementType: types.StringType, Description: "Individually named permissions in this namespace, instead of a built-in role."},
		"custom_role_id": schema.StringAttribute{Optional: true, Description: "fdp_custom_role ID, instead of a built-in role or explicit actions."},
	}}
}
func (r *permissionAssignmentResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}
func (r *permissionAssignmentResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}
func (m *permissionAssignmentModel) body(ctx context.Context, d *diag.Diagnostics) map[string]any {
	selected := 0
	if !m.Role.IsNull() {
		selected++
	}
	if !m.Actions.IsNull() {
		selected++
	}
	if !m.CustomRoleID.IsNull() {
		selected++
	}
	if selected != 1 || m.AppGroupID.IsNull() == m.PrincipalID.IsNull() {
		d.AddError("Invalid permission assignment", "Choose exactly one recipient and one of role, actions or custom_role_id")
		return nil
	}
	body := map[string]any{"target_id": m.TargetID.ValueString(), "namespace": m.Namespace.ValueString()}
	for key, value := range map[string]types.String{"layer_id": m.LayerID, "app_group_id": m.AppGroupID, "principal_id": m.PrincipalID, "role": m.Role, "custom_role_id": m.CustomRoleID} {
		if !value.IsNull() {
			body[key] = value.ValueString()
		}
	}
	if !m.Actions.IsNull() {
		var actions []string
		d.Append(m.Actions.ElementsAs(ctx, &actions, false)...)
		body["actions"] = actions
	}
	return body
}
func permissionString(value *string) types.String {
	if value == nil {
		return types.StringNull()
	}
	return types.StringValue(*value)
}
func (m *permissionAssignmentModel) apply(ctx context.Context, out permissionAssignmentOut, d *diag.Diagnostics) {
	m.ID, m.TargetID, m.Namespace = types.StringValue(out.ID), types.StringValue(out.TargetID), types.StringValue(out.Namespace)
	m.LayerID, m.AppGroupID, m.PrincipalID, m.Role, m.CustomRoleID = permissionString(out.LayerID), permissionString(out.AppGroupID), permissionString(out.PrincipalID), permissionString(out.Role), permissionString(out.CustomRoleID)
	if len(out.Actions) == 0 {
		m.Actions = types.SetNull(types.StringType)
	} else {
		value, diags := types.SetValueFrom(ctx, types.StringType, out.Actions)
		d.Append(diags...)
		m.Actions = value
	}
}
func (r *permissionAssignmentResource) save(ctx context.Context, m *permissionAssignmentModel, create bool, d *diag.Diagnostics) {
	body := m.body(ctx, d)
	if d.HasError() {
		return
	}
	method, url := "PUT", "/v1/permissions/assignments/"+m.ID.ValueString()
	if create {
		method, url = "POST", "/v1/permissions/assignments"
	}
	var out permissionAssignmentOut
	if err := r.api.Do(ctx, method, url, body, &out); err != nil {
		apiError(d, "saving permission assignment", err)
		return
	}
	m.apply(ctx, out, d)
}
func (r *permissionAssignmentResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m permissionAssignmentModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.save(ctx, &m, true, &resp.Diagnostics)
	if !resp.Diagnostics.HasError() {
		resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
	}
}
func (r *permissionAssignmentResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m permissionAssignmentModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out permissionAssignmentOut
	if err := r.api.Do(ctx, "GET", "/v1/permissions/assignments/"+m.ID.ValueString(), nil, &out); err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading permission assignment", err)
		}
		return
	}
	m.apply(ctx, out, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}
func (r *permissionAssignmentResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m permissionAssignmentModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.save(ctx, &m, false, &resp.Diagnostics)
	if !resp.Diagnostics.HasError() {
		resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
	}
}
func (r *permissionAssignmentResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m permissionAssignmentModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.api.Do(ctx, "DELETE", "/v1/permissions/assignments/"+m.ID.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "revoking permission assignment", err)
	}
}

type customRoleResource struct{ api *client.Client }
type customRoleModel struct {
	ID      types.String `tfsdk:"id"`
	Name    types.String `tfsdk:"name"`
	Actions types.Set    `tfsdk:"actions"`
}
type customRoleOut struct {
	ID      string   `json:"id"`
	Name    string   `json:"name"`
	Actions []string `json:"actions"`
}

func NewCustomRoleResource() resource.Resource { return &customRoleResource{} }
func (r *customRoleResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_custom_role"
}
func (r *customRoleResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{Description: "A named set of registered action permissions. Global Admin must be active to edit a definition because edits affect all recipients. Import by ID.", Attributes: map[string]schema.Attribute{
		"id": schema.StringAttribute{Computed: true, PlanModifiers: keepComputed}, "name": schema.StringAttribute{Required: true}, "actions": schema.SetAttribute{Required: true, ElementType: types.StringType},
	}}
}
func (r *customRoleResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}
func (r *customRoleResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}
func (m *customRoleModel) apply(ctx context.Context, out customRoleOut, d *diag.Diagnostics) {
	m.ID, m.Name = types.StringValue(out.ID), types.StringValue(out.Name)
	v, diags := types.SetValueFrom(ctx, types.StringType, out.Actions)
	d.Append(diags...)
	m.Actions = v
}
func (r *customRoleResource) save(ctx context.Context, m *customRoleModel, create bool, d *diag.Diagnostics) {
	var actions []string
	d.Append(m.Actions.ElementsAs(ctx, &actions, false)...)
	if d.HasError() {
		return
	}
	method, url := "PUT", "/v1/permissions/roles/"+m.ID.ValueString()
	if create {
		method, url = "POST", "/v1/permissions/roles"
	}
	var out customRoleOut
	if err := r.api.Do(ctx, method, url, map[string]any{"name": m.Name.ValueString(), "actions": actions}, &out); err != nil {
		apiError(d, "saving custom role", err)
		return
	}
	m.apply(ctx, out, d)
}
func (r *customRoleResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m customRoleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.save(ctx, &m, true, &resp.Diagnostics)
	if !resp.Diagnostics.HasError() {
		resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
	}
}
func (r *customRoleResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m customRoleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out customRoleOut
	if err := r.api.Do(ctx, "GET", "/v1/permissions/roles/"+m.ID.ValueString(), nil, &out); err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading custom role", err)
		}
		return
	}
	m.apply(ctx, out, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}
func (r *customRoleResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m customRoleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.save(ctx, &m, false, &resp.Diagnostics)
	if !resp.Diagnostics.HasError() {
		resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
	}
}
func (r *customRoleResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m customRoleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.api.Do(ctx, "DELETE", "/v1/permissions/roles/"+m.ID.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "deleting custom role", err)
	}
}
