package provider

import (
	"context"
	"fmt"
	"strings"

	"github.com/floren/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// ---- app groups ---------------------------------------------------------------------------

type appGroupResource struct{ api *client.Client }

type appGroupModel struct {
	ID          types.String `tfsdk:"id"`
	Name        types.String `tfsdk:"name"`
	Description types.String `tfsdk:"description"`
	Owners      types.Set    `tfsdk:"owners"`
}

type groupOut struct {
	ID          string   `json:"id"`
	Name        string   `json:"name"`
	Description string   `json:"description"`
	Owners      []string `json:"owners"`
	Bindings    []string `json:"bindings"`
}

func NewAppGroupResource() resource.Resource { return &appGroupResource{} }

func (r *appGroupResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_app_group"
}

func (r *appGroupResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "A Floren Data Platform app group: the unit that receives roles. Members are directory identities (managed " +
			"by SCIM/your IdP) bound with fdp_app_group_member. Import by ID.",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Computed: true, PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"name":        schema.StringAttribute{Required: true},
			"description": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"owners": schema.SetAttribute{ElementType: types.StringType, Required: true,
				Description: "Principal IDs who manage the group's members."},
		},
	}
}

func (r *appGroupResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *appGroupResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func findGroup(ctx context.Context, api *client.Client, id string) (*groupOut, error) {
	var all []groupOut
	if err := api.Do(ctx, "GET", "/v1/rbac/groups", nil, &all); err != nil {
		return nil, err
	}
	for i := range all {
		if all[i].ID == id {
			return &all[i], nil
		}
	}
	return nil, &client.APIError{Status: 404, Detail: "app group not found"}
}

func (m *appGroupModel) apply(out *groupOut) {
	m.ID, m.Name, m.Description = types.StringValue(out.ID), types.StringValue(out.Name), types.StringValue(out.Description)
	m.Owners = setValue(out.Owners)
}

func (r *appGroupResource) body(ctx context.Context, m *appGroupModel, d *diagSink) map[string]any {
	return map[string]any{"name": m.Name.ValueString(), "description": m.Description.ValueString(),
		"owners": stringSet(ctx, m.Owners, d.d)}
}

func (r *appGroupResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan appGroupModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out groupOut
	if err := r.api.Do(ctx, "POST", "/v1/rbac/groups", r.body(ctx, &plan, &diagSink{&resp.Diagnostics}), &out); err != nil {
		apiError(&resp.Diagnostics, "creating app group", err)
		return
	}
	plan.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *appGroupResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state appGroupModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	out, err := findGroup(ctx, r.api, state.ID.ValueString())
	if err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading app group", err)
		}
		return
	}
	state.apply(out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *appGroupResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state appGroupModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var out groupOut
	if err := r.api.Do(ctx, "PUT", "/v1/rbac/groups/"+state.ID.ValueString(), r.body(ctx, &plan, &diagSink{&resp.Diagnostics}), &out); err != nil {
		apiError(&resp.Diagnostics, "updating app group", err)
		return
	}
	plan.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *appGroupResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state appGroupModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.api.Do(ctx, "DELETE", "/v1/rbac/groups/"+state.ID.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "deleting app group", err)
	}
}

// ---- app group members ------------------------------------------------------------------------

type appGroupMemberResource struct{ api *client.Client }

type appGroupMemberModel struct {
	ID          types.String `tfsdk:"id"`
	AppGroupID  types.String `tfsdk:"app_group_id"`
	PrincipalID types.String `tfsdk:"principal_id"`
}

func NewAppGroupMemberResource() resource.Resource { return &appGroupMemberResource{} }

func (r *appGroupMemberResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_app_group_member"
}

func (r *appGroupMemberResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	replace := []planmodifier.String{stringplanmodifier.RequiresReplace()}
	resp.Schema = schema.Schema{
		Description: "Binds a directory identity (user, IdP group, or service) to an app group. Import as <app_group_id>/<principal_id>.",
		Attributes: map[string]schema.Attribute{
			"id":           schema.StringAttribute{Computed: true, PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"app_group_id": schema.StringAttribute{Required: true, PlanModifiers: replace},
			"principal_id": schema.StringAttribute{Required: true, PlanModifiers: replace},
		},
	}
}

func (r *appGroupMemberResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *appGroupMemberResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	group, principal, ok := strings.Cut(req.ID, "/")
	if !ok {
		resp.Diagnostics.AddError("Invalid import ID", "use <app_group_id>/<principal_id>")
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &appGroupMemberModel{ID: types.StringValue(req.ID),
		AppGroupID: types.StringValue(group), PrincipalID: types.StringValue(principal)})...)
}

func (r *appGroupMemberResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan appGroupMemberModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.api.Do(ctx, "POST", "/v1/rbac/groups/"+plan.AppGroupID.ValueString()+"/bindings",
		map[string]any{"principal_id": plan.PrincipalID.ValueString()}, nil); err != nil {
		apiError(&resp.Diagnostics, "binding identity to app group", err)
		return
	}
	plan.ID = types.StringValue(plan.AppGroupID.ValueString() + "/" + plan.PrincipalID.ValueString())
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *appGroupMemberResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state appGroupMemberModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	group, err := findGroup(ctx, r.api, state.AppGroupID.ValueString())
	if err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading app group", err)
		}
		return
	}
	for _, p := range group.Bindings {
		if p == state.PrincipalID.ValueString() {
			resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
			return
		}
	}
	resp.State.RemoveResource(ctx)
}

func (r *appGroupMemberResource) Update(context.Context, resource.UpdateRequest, *resource.UpdateResponse) {
}

func (r *appGroupMemberResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state appGroupMemberModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	err := r.api.Do(ctx, "DELETE", "/v1/rbac/groups/"+state.AppGroupID.ValueString()+"/bindings/"+state.PrincipalID.ValueString(), nil, nil)
	if err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "removing app group member", err)
	}
}

// ---- role assignments -------------------------------------------------------------------------

type roleAssignmentResource struct{ api *client.Client }

type roleAssignmentModel struct {
	ID          types.String `tfsdk:"id"`
	ResourceID  types.String `tfsdk:"resource_id"`
	Role        types.String `tfsdk:"role"`
	AppGroupID  types.String `tfsdk:"app_group_id"`
	PrincipalID types.String `tfsdk:"principal_id"`
}

type assignmentOut struct {
	ResourceID  string  `json:"resource_id"`
	AppGroupID  *string `json:"app_group_id"`
	PrincipalID *string `json:"principal_id"`
	Role        string  `json:"role"`
	Inherited   bool    `json:"inherited"`
}

func NewRoleAssignmentResource() resource.Resource { return &roleAssignmentResource{} }

func (r *roleAssignmentResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_role_assignment"
}

func (r *roleAssignmentResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	replace := []planmodifier.String{stringplanmodifier.RequiresReplace()}
	resp.Schema = schema.Schema{
		Description: "A built-in role on one object (schema, source, folder, workload, API...) for an app group or " +
			"directly for a user/service identity. Import as <resource_id>/group/<id> or <resource_id>/principal/<id>.",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Computed: true, PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"resource_id": schema.StringAttribute{Required: true, PlanModifiers: replace},
			"role": schema.StringAttribute{Required: true, Description: "e.g. Reader, Contributor, Admin, Owner, Executor, Consumer; " +
				"on the package repository (resource_id \"packages\"): Publisher, Maintainer or Owner."},
			"app_group_id": schema.StringAttribute{Optional: true, PlanModifiers: replace},
			"principal_id": schema.StringAttribute{Optional: true, PlanModifiers: replace},
		},
	}
}

func (r *roleAssignmentResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *roleAssignmentResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	parts := strings.Split(req.ID, "/")
	if len(parts) != 3 || (parts[1] != "group" && parts[1] != "principal") {
		resp.Diagnostics.AddError("Invalid import ID", "use <resource_id>/group/<app_group_id> or <resource_id>/principal/<principal_id>")
		return
	}
	m := roleAssignmentModel{ID: types.StringValue(req.ID), ResourceID: types.StringValue(parts[0]),
		AppGroupID: types.StringNull(), PrincipalID: types.StringNull(), Role: types.StringNull()}
	if parts[1] == "group" {
		m.AppGroupID = types.StringValue(parts[2])
	} else {
		m.PrincipalID = types.StringValue(parts[2])
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (m *roleAssignmentModel) key() string {
	if !m.AppGroupID.IsNull() {
		return m.ResourceID.ValueString() + "/group/" + m.AppGroupID.ValueString()
	}
	return m.ResourceID.ValueString() + "/principal/" + m.PrincipalID.ValueString()
}

func (r *roleAssignmentResource) put(ctx context.Context, m *roleAssignmentModel, d *diagSink) {
	if m.AppGroupID.IsNull() == m.PrincipalID.IsNull() {
		d.d.AddError("Choose one recipient", "set exactly one of app_group_id or principal_id")
		return
	}
	body := map[string]any{"role": m.Role.ValueString()}
	if !m.AppGroupID.IsNull() {
		body["app_group_id"] = m.AppGroupID.ValueString()
	} else {
		body["principal_id"] = m.PrincipalID.ValueString()
	}
	if err := r.api.Do(ctx, "PUT", "/v1/rbac/resources/"+m.ResourceID.ValueString()+"/assignments", body, nil); err != nil {
		apiError(d.d, "assigning role", err)
		return
	}
	m.ID = types.StringValue(m.key())
}

func (r *roleAssignmentResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan roleAssignmentModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &plan, &diagSink{&resp.Diagnostics})
	if !resp.Diagnostics.HasError() {
		resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
	}
}

func (r *roleAssignmentResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state roleAssignmentModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var all []assignmentOut
	if err := r.api.Do(ctx, "GET", "/v1/rbac/resources/"+state.ResourceID.ValueString()+"/assignments", nil, &all); err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading role assignments", err)
		}
		return
	}
	for _, a := range all {
		if a.Inherited || a.ResourceID != state.ResourceID.ValueString() {
			continue
		}
		if (!state.AppGroupID.IsNull() && a.AppGroupID != nil && *a.AppGroupID == state.AppGroupID.ValueString()) ||
			(!state.PrincipalID.IsNull() && a.PrincipalID != nil && *a.PrincipalID == state.PrincipalID.ValueString()) {
			state.Role = types.StringValue(a.Role)
			state.ID = types.StringValue(state.key())
			resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
			return
		}
	}
	resp.State.RemoveResource(ctx)
}

func (r *roleAssignmentResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan roleAssignmentModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	r.put(ctx, &plan, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *roleAssignmentResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state roleAssignmentModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	target := "/v1/rbac/resources/" + state.ResourceID.ValueString() + "/assignments/"
	if !state.AppGroupID.IsNull() {
		target += state.AppGroupID.ValueString()
	} else {
		target += "services/" + state.PrincipalID.ValueString()
	}
	if err := r.api.Do(ctx, "DELETE", target, nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, fmt.Sprintf("revoking role %s", state.Role.ValueString()), err)
	}
}
