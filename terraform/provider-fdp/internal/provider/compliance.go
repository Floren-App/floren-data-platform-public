package provider

import (
	"context"
	"net/url"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64default"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/setplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// ---- compliance forms -------------------------------------------------------------------------------

type accessFormResource struct{ api *client.Client }

type accessFormModel struct {
	ID          types.String `tfsdk:"id"`
	Name        types.String `tfsdk:"name"`
	Description types.String `tfsdk:"description"`
	Definition  JSONValue    `tfsdk:"definition"`
	Version     types.Int64  `tfsdk:"version"`
}

type formOut struct {
	ID               string `json:"id"`
	Name             string `json:"name"`
	Description      string `json:"description"`
	PublishedVersion *int64 `json:"published_version"`
	Versions         []struct {
		Version    int64          `json:"version"`
		Published  bool           `json:"published"`
		Definition map[string]any `json:"definition"`
	} `json:"versions"`
}

func NewAccessFormResource() resource.Resource { return &accessFormResource{} }

func (r *accessFormResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_access_form"
}

func (r *accessFormResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "A compliance form people fill in to justify access under an access policy. Applying a changed definition " +
			"publishes it as a new version; cases keep the version they were filled in with. Field types: short_text, long_text, " +
			"choice, multi_choice, date, number, statement, person, file. Import by form ID.",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"name":        schema.StringAttribute{Required: true},
			"description": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"definition": schema.StringAttribute{Required: true, CustomType: JSONType{}, PlanModifiers: jsonPlan,
				Description: "jsonencode({ pages = [{ id, title, intro, show_if = { field, equals | contains }, fields = [{ id, label, " +
					"help, type, required, options, show_if }] }] }). Only declared keys are compared."},
			"version": schema.Int64Attribute{Computed: true, Description: "The published version."},
		},
	}
}

func (r *accessFormResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *accessFormResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func (m *accessFormModel) apply(out *formOut) {
	m.ID, m.Name, m.Description = types.StringValue(out.ID), types.StringValue(out.Name), types.StringValue(out.Description)
	m.Version = types.Int64Null()
	if out.PublishedVersion != nil {
		m.Version = types.Int64Value(*out.PublishedVersion)
		for _, v := range out.Versions {
			if v.Version == *out.PublishedVersion {
				m.Definition = reconcileJSON(m.Definition, v.Definition)
			}
		}
	}
}

func (r *accessFormResource) save(ctx context.Context, current string, m *accessFormModel, d *diagSink) {
	body := map[string]any{"name": m.Name.ValueString(), "description": m.Description.ValueString(),
		"definition": decodeJSON(m.Definition, d.d, "definition")}
	var draft formOut
	if err := r.api.Do(ctx, "PUT", "/v1/compliance/forms/"+url.PathEscape(current), body, &draft); err != nil {
		apiError(d.d, "saving the form", err)
		return
	}
	var published formOut
	if err := r.api.Do(ctx, "POST", "/v1/compliance/forms/"+draft.ID+"/publish", nil, &published); err != nil {
		apiError(d.d, "publishing the form", err)
		return
	}
	m.apply(&published)
}

func (r *accessFormResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan accessFormModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.save(ctx, plan.Name.ValueString(), &plan, &diagSink{&resp.Diagnostics})
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *accessFormResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state accessFormModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var out formOut
	if err := r.api.Do(ctx, "GET", "/v1/compliance/forms/"+state.ID.ValueString(), nil, &out); err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading the form", err)
		}
		return
	}
	state.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *accessFormResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state accessFormModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.save(ctx, state.Name.ValueString(), &plan, &diagSink{&resp.Diagnostics})
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *accessFormResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state accessFormModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.api.Do(ctx, "DELETE", "/v1/compliance/forms/"+state.ID.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "removing the form", err)
	}
}

// ---- access policies (cells of the grid) ---------------------------------------------------------------

type accessPolicyResource struct{ api *client.Client }

type policyStep struct {
	Role      types.String `tfsdk:"role"`
	Approvals types.Int64  `tfsdk:"approvals"`
}

type accessPolicyModel struct {
	ID             types.String `tfsdk:"id"`
	LayerID        types.String `tfsdk:"layer_id"`
	Classification types.String `tfsdk:"classification"`
	FormID         types.String `tfsdk:"form_id"`
	Steps          []policyStep `tfsdk:"steps"`
	MaxDays        types.Int64  `tfsdk:"max_days"`
	ReviewDays     types.Int64  `tfsdk:"review_days"`
	RenewDays      types.Int64  `tfsdk:"renew_days"`
	GraceDays      types.Int64  `tfsdk:"grace_days"`
}

type policyOut struct {
	ID       string `json:"id"`
	LayerID  string `json:"layer_id"`
	Level    string `json:"classification"`
	FormID   string `json:"form_id"`
	MaxDays  int64  `json:"max_days"`
	Review   int64  `json:"review_days"`
	Renew    int64  `json:"renew_days"`
	Grace    int64  `json:"grace_days"`
	StepList []struct {
		Role      string `json:"role"`
		Approvals int64  `json:"approvals"`
	} `json:"steps"`
}

func NewAccessPolicyResource() resource.Resource { return &accessPolicyResource{} }

func (r *accessPolicyResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_access_policy"
}

func (r *accessPolicyResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "One cell of the access policy grid: access to data of this classification in this layer needs the form and " +
			"its review. Without a policy for a cell, no form is needed. Import with \"<layer id>/<classification>\".",
		Attributes: map[string]schema.Attribute{
			"id":             schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"layer_id":       schema.StringAttribute{Required: true, PlanModifiers: replaceString},
			"classification": schema.StringAttribute{Required: true, PlanModifiers: replaceString},
			"form_id":        schema.StringAttribute{Required: true},
			"steps": schema.ListNestedAttribute{Required: true, Description: "Review steps in order: data_owner or compliance_reviewer.",
				NestedObject: schema.NestedAttributeObject{Attributes: map[string]schema.Attribute{
					"role":      schema.StringAttribute{Required: true},
					"approvals": schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(1)},
				}}},
			"max_days":    schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(365), Description: "Longest approved access."},
			"review_days": schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(10), Description: "Reviews are due within this many days."},
			"renew_days": schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(30),
				Description: "A renewal, prefilled with the answers, opens this many days before approved access ends."},
			"grace_days": schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(30),
				Description: "Access that existed before the policy, or whose data was classified higher since, is kept this many days for a form to be approved; then it is removed."},
		},
	}
}

func (r *accessPolicyResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *accessPolicyResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	layer, level, ok := splitOnce(req.ID)
	if !ok {
		resp.Diagnostics.AddError("Invalid import ID", "use <layer id>/<classification>")
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("layer_id"), layer)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("classification"), level)...)
}

func (m *accessPolicyModel) apply(out *policyOut) {
	m.ID, m.LayerID, m.Classification, m.FormID = types.StringValue(out.ID), types.StringValue(out.LayerID), types.StringValue(out.Level), types.StringValue(out.FormID)
	m.MaxDays, m.ReviewDays = types.Int64Value(out.MaxDays), types.Int64Value(out.Review)
	m.RenewDays, m.GraceDays = types.Int64Value(out.Renew), types.Int64Value(out.Grace)
	m.Steps = nil
	for _, s := range out.StepList {
		m.Steps = append(m.Steps, policyStep{Role: types.StringValue(s.Role), Approvals: types.Int64Value(s.Approvals)})
	}
}

func (r *accessPolicyResource) put(ctx context.Context, m *accessPolicyModel, d *diagSink) {
	steps := []map[string]any{}
	for _, s := range m.Steps {
		steps = append(steps, map[string]any{"role": s.Role.ValueString(), "approvals": s.Approvals.ValueInt64()})
	}
	body := map[string]any{"form_id": m.FormID.ValueString(), "steps": steps, "max_days": m.MaxDays.ValueInt64(), "review_days": m.ReviewDays.ValueInt64(),
		"renew_days": m.RenewDays.ValueInt64(), "grace_days": m.GraceDays.ValueInt64()}
	var out policyOut
	target := "/v1/compliance/policies/" + m.LayerID.ValueString() + "/" + url.PathEscape(m.Classification.ValueString())
	if err := r.api.Do(ctx, "PUT", target, body, &out); err != nil {
		apiError(d.d, "saving the access policy", err)
		return
	}
	m.apply(&out)
}

func (r *accessPolicyResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan accessPolicyModel
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

func (r *accessPolicyResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state accessPolicyModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var grid struct {
		Policies []policyOut `json:"policies"`
	}
	if err := r.api.Do(ctx, "GET", "/v1/compliance/policies", nil, &grid); err != nil {
		apiError(&resp.Diagnostics, "reading access policies", err)
		return
	}
	for i := range grid.Policies {
		if grid.Policies[i].LayerID == state.LayerID.ValueString() && grid.Policies[i].Level == state.Classification.ValueString() {
			state.apply(&grid.Policies[i])
			resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
			return
		}
	}
	resp.State.RemoveResource(ctx)
}

func (r *accessPolicyResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan accessPolicyModel
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

func (r *accessPolicyResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state accessPolicyModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	target := "/v1/compliance/policies/" + state.LayerID.ValueString() + "/" + url.PathEscape(state.Classification.ValueString())
	if err := r.api.Do(ctx, "DELETE", target, nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "removing the access policy", err)
	}
}

// ---- compliance roles ---------------------------------------------------------------------------------

type complianceRoleResource struct{ api *client.Client }

type complianceRoleModel struct {
	ID              types.String `tfsdk:"id"`
	Role            types.String `tfsdk:"role"`
	AppGroupID      types.String `tfsdk:"app_group_id"`
	ResourceIDs     types.Set    `tfsdk:"resource_ids"`
	Layers          types.Set    `tfsdk:"layers"`
	Classifications types.Set    `tfsdk:"classifications"`
	GroupIDs        types.Set    `tfsdk:"group_ids"`
}

func NewComplianceRoleResource() resource.Resource { return &complianceRoleResource{} }

func (r *complianceRoleResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_compliance_role"
}

func (r *complianceRoleResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	emptySet := func(desc string) schema.SetAttribute {
		return schema.SetAttribute{ElementType: types.StringType, Optional: true, Computed: true, Description: desc, PlanModifiers: []planmodifier.Set{setplanmodifier.RequiresReplace()}}
	}
	resp.Schema = schema.Schema{
		Description: "A compliance role an app group holds within a scope: compliance_reviewer (reviews compliance forms), " +
			"membership_approver (approves people joining groups with justified access) or compliance_admin (forms, policies, " +
			"classifications, roles). An empty scope list means all. Any change replaces the role. Import by ID.",
		Attributes: map[string]schema.Attribute{
			"id":              schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"role":            schema.StringAttribute{Required: true, PlanModifiers: replaceString},
			"app_group_id":    schema.StringAttribute{Required: true, PlanModifiers: replaceString},
			"resource_ids":    emptySet("Objects (and what is under them)."),
			"layers":          emptySet("Layer IDs."),
			"classifications": emptySet("Classifications."),
			"group_ids":       emptySet("For membership approvers: app group IDs."),
		},
	}
}

func (r *complianceRoleResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *complianceRoleResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

type roleScope struct {
	ResourceIDs     []string `json:"resource_ids"`
	Layers          []string `json:"layers"`
	Classifications []string `json:"classifications"`
	GroupIDs        []string `json:"group_ids"`
}

type complianceRoleOut struct {
	ID         string    `json:"id"`
	Role       string    `json:"role"`
	AppGroupID string    `json:"app_group_id"`
	Scope      roleScope `json:"scope"`
}

func (m *complianceRoleModel) apply(out *complianceRoleOut) {
	m.ID, m.Role, m.AppGroupID = types.StringValue(out.ID), types.StringValue(out.Role), types.StringValue(out.AppGroupID)
	m.ResourceIDs, m.Layers = setValue(out.Scope.ResourceIDs), setValue(out.Scope.Layers)
	m.Classifications, m.GroupIDs = setValue(out.Scope.Classifications), setValue(out.Scope.GroupIDs)
}

func optionalSet(ctx context.Context, value types.Set, d *diagSink) []string {
	if value.IsNull() || value.IsUnknown() {
		return []string{}
	}
	return stringSet(ctx, value, d.d)
}

func (r *complianceRoleResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan complianceRoleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	d := &diagSink{&resp.Diagnostics}
	body := map[string]any{"role": plan.Role.ValueString(), "app_group_id": plan.AppGroupID.ValueString(), "scope": map[string]any{
		"resource_ids": optionalSet(ctx, plan.ResourceIDs, d), "layers": optionalSet(ctx, plan.Layers, d),
		"classifications": optionalSet(ctx, plan.Classifications, d), "group_ids": optionalSet(ctx, plan.GroupIDs, d)}}
	var out complianceRoleOut
	if err := r.api.Do(ctx, "POST", "/v1/compliance/roles", body, &out); err != nil {
		apiError(&resp.Diagnostics, "giving the compliance role", err)
		return
	}
	plan.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *complianceRoleResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state complianceRoleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var listed struct {
		Roles []complianceRoleOut `json:"roles"`
	}
	if err := r.api.Do(ctx, "GET", "/v1/compliance/roles", nil, &listed); err != nil {
		apiError(&resp.Diagnostics, "reading compliance roles", err)
		return
	}
	for i := range listed.Roles {
		if listed.Roles[i].ID == state.ID.ValueString() {
			state.apply(&listed.Roles[i])
			resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
			return
		}
	}
	resp.State.RemoveResource(ctx)
}

// Update: every attribute that can change replaces the role (scopes are set at creation).
func (r *complianceRoleResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state complianceRoleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.api.Do(ctx, "DELETE", "/v1/compliance/roles/"+state.ID.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "changing the compliance role", err)
		return
	}
	d := &diagSink{&resp.Diagnostics}
	body := map[string]any{"role": plan.Role.ValueString(), "app_group_id": plan.AppGroupID.ValueString(), "scope": map[string]any{
		"resource_ids": optionalSet(ctx, plan.ResourceIDs, d), "layers": optionalSet(ctx, plan.Layers, d),
		"classifications": optionalSet(ctx, plan.Classifications, d), "group_ids": optionalSet(ctx, plan.GroupIDs, d)}}
	var out complianceRoleOut
	if err := r.api.Do(ctx, "POST", "/v1/compliance/roles", body, &out); err != nil {
		apiError(&resp.Diagnostics, "changing the compliance role", err)
		return
	}
	plan.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *complianceRoleResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state complianceRoleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.api.Do(ctx, "DELETE", "/v1/compliance/roles/"+state.ID.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "removing the compliance role", err)
	}
}

func splitOnce(id string) (string, string, bool) {
	for i := 0; i < len(id); i++ {
		if id[i] == '/' {
			return id[:i], id[i+1:], i > 0 && i < len(id)-1
		}
	}
	return "", "", false
}
