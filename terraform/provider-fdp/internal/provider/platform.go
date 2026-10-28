package provider

import (
	"context"
	"strings"

	"github.com/floren/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// ---- folders -------------------------------------------------------------------------------------

type folderResource struct{ api *client.Client }

type folderModel struct {
	ID       types.String `tfsdk:"id"`
	Name     types.String `tfsdk:"name"`
	ParentID types.String `tfsdk:"parent_id"`
	LayerID  types.String `tfsdk:"layer_id"`
	Branch   types.String `tfsdk:"branch"`
}

func NewFolderResource() resource.Resource { return &folderResource{} }

func (r *folderResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_folder"
}

func (r *folderResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "A folder that organizes workloads and their permissions (inherited by everything inside). Import by ID.",
		Attributes: map[string]schema.Attribute{
			"id":        schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"name":      schema.StringAttribute{Required: true},
			"parent_id": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("workloads")},
			"branch": schema.StringAttribute{Optional: true, PlanModifiers: replaceString,
				Description: "Promotion rule: only workloads from this branch may go into this folder (an environment folder)."},
			"layer_id": schema.StringAttribute{Optional: true, Computed: true, Description: "Bind everything inside to one layer.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplaceIfConfigured(), stringplanmodifier.UseStateForUnknown()}},
		},
	}
}

func (r *folderResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *folderResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func (m *folderModel) apply(out *resourceOut) {
	m.ID, m.Name, m.ParentID = types.StringValue(out.ID), types.StringValue(out.Name), types.StringValue(out.ParentID)
	m.LayerID = optional(out.LayerID)
	if branch, ok := out.Config["branch"].(string); ok && branch != "" {
		m.Branch = types.StringValue(branch)
	}
}

func (r *folderResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan folderModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := map[string]any{"name": plan.Name.ValueString(), "parent_id": plan.ParentID.ValueString()}
	if !plan.LayerID.IsNull() && !plan.LayerID.IsUnknown() {
		body["layer_id"] = plan.LayerID.ValueString()
	}
	if !plan.Branch.IsNull() && plan.Branch.ValueString() != "" {
		body["environment"], body["branch"] = true, plan.Branch.ValueString()
	}
	var out resourceOut
	if err := r.api.Do(ctx, "POST", "/v1/rbac/folders", body, &out); err != nil {
		apiError(&resp.Diagnostics, "creating folder", err)
		return
	}
	plan.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *folderResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state folderModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var out resourceOut
	if err := r.api.Do(ctx, "GET", "/v1/rbac/resources/"+state.ID.ValueString(), nil, &out); err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading folder", err)
		}
		return
	}
	state.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *folderResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state folderModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	body := map[string]any{"name": plan.Name.ValueString(), "parent_id": plan.ParentID.ValueString()}
	var out resourceOut
	if err := r.api.Do(ctx, "PATCH", "/v1/rbac/resources/"+state.ID.ValueString(), body, &out); err != nil {
		apiError(&resp.Diagnostics, "renaming or moving folder", err)
		return
	}
	plan.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *folderResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state folderModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.api.Do(ctx, "DELETE", "/v1/rbac/folders/"+state.ID.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "deleting folder (it must be empty)", err)
	}
}

// ---- service accounts ----------------------------------------------------------------------------

type serviceAccountResource struct{ api *client.Client }

type serviceAccountModel struct {
	ID          types.String `tfsdk:"id"`
	Name        types.String `tfsdk:"name"`
	LayerID     types.String `tfsdk:"layer_id"`
	Description types.String `tfsdk:"description"`
	Groups      types.List   `tfsdk:"groups"`
	Disabled    types.Bool   `tfsdk:"disabled"`
	Principal   types.String `tfsdk:"principal"`
	ResourceID  types.String `tfsdk:"resource_id"`
}

type serviceAccountOut struct {
	ID          string   `json:"id"`
	Name        string   `json:"name"`
	LayerID     *string  `json:"layer_id"`
	Description string   `json:"description"`
	Groups      []string `json:"groups"`
	Disabled    bool     `json:"disabled"`
	Principal   string   `json:"principal"`
	ResourceID  *string  `json:"resource_id"`
}

func NewServiceAccountResource() resource.Resource { return &serviceAccountResource{} }

func (r *serviceAccountResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_service_account"
}

func (r *serviceAccountResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "A layer-bound service identity that deployed programs can run as (run_as). Grant it roles with " +
			"fdp_role_assignment (principal_id from the directory). Destroy disables it. Import by name. Tokens stay runtime state.",
		Attributes: map[string]schema.Attribute{
			"id":   schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"name": schema.StringAttribute{Required: true, PlanModifiers: replaceString},
			"layer_id": schema.StringAttribute{Optional: true, Computed: true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplaceIfConfigured(), stringplanmodifier.UseStateForUnknown()}},
			"description": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"groups": schema.ListAttribute{ElementType: types.StringType, Optional: true, Computed: true,
				Default: listdefault.StaticValue(listValue(nil)), Description: "Legacy data access groups."},
			"disabled":    schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false)},
			"principal":   schema.StringAttribute{Computed: true, PlanModifiers: keepComputed, Description: "sa:<name>, as used by run_as."},
			"resource_id": schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
		},
	}
}

func (r *serviceAccountResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *serviceAccountResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("name"), req, resp)
}

func (m *serviceAccountModel) apply(out *serviceAccountOut) {
	m.ID, m.Name, m.Description = types.StringValue(out.ID), types.StringValue(out.Name), types.StringValue(out.Description)
	m.LayerID, m.Groups, m.Disabled = optional(out.LayerID), listValue(out.Groups), types.BoolValue(out.Disabled)
	m.Principal, m.ResourceID = types.StringValue(out.Principal), optional(out.ResourceID)
}

func (r *serviceAccountResource) put(ctx context.Context, m *serviceAccountModel, disabled bool, d *diagSink) {
	body := map[string]any{"description": m.Description.ValueString(), "groups": stringList(ctx, m.Groups, d.d), "disabled": disabled}
	if !m.LayerID.IsNull() && !m.LayerID.IsUnknown() {
		body["layer_id"] = m.LayerID.ValueString()
	}
	var out serviceAccountOut
	if err := r.api.Do(ctx, "PUT", "/v1/admin/service-accounts/"+m.Name.ValueString(), body, &out); err != nil {
		apiError(d.d, "saving service account", err)
		return
	}
	m.apply(&out)
}

func (r *serviceAccountResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan serviceAccountModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &plan, plan.Disabled.ValueBool(), &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *serviceAccountResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state serviceAccountModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var all []serviceAccountOut
	if err := r.api.Do(ctx, "GET", "/v1/service-accounts", nil, &all); err != nil {
		apiError(&resp.Diagnostics, "reading service accounts", err)
		return
	}
	for i := range all {
		if all[i].Name == state.Name.ValueString() {
			state.apply(&all[i])
			resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
			return
		}
	}
	resp.State.RemoveResource(ctx)
}

func (r *serviceAccountResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan serviceAccountModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	r.put(ctx, &plan, plan.Disabled.ValueBool(), &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *serviceAccountResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state serviceAccountModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	err := r.api.Do(ctx, "DELETE", "/v1/admin/service-accounts/"+state.Name.ValueString(), nil, nil)
	if err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "disabling service account", err)
	}
}

// ---- secrets ------------------------------------------------------------------------------------

type secretResource struct{ api *client.Client }

type secretModel struct {
	ID           types.String `tfsdk:"id"`
	Scope        types.String `tfsdk:"scope"`
	Owner        types.String `tfsdk:"owner"`
	Name         types.String `tfsdk:"name"`
	Kind         types.String `tfsdk:"kind"`
	Description  types.String `tfsdk:"description"`
	ValueWO      types.String `tfsdk:"value_wo"`
	Certificate  types.String `tfsdk:"certificate"`
	CAChain      types.String `tfsdk:"ca_chain"`
	PrivateKeyWO types.String `tfsdk:"private_key_wo"`
	ValueVersion types.Int64  `tfsdk:"value_wo_version"`
	Reference    types.String `tfsdk:"reference"`
}

type secretOut struct {
	Scope       string `json:"scope"`
	Owner       string `json:"owner"`
	Name        string `json:"name"`
	Kind        string `json:"kind"`
	Ref         string `json:"ref"`
	Description string `json:"description"`
}

func NewSecretResource() resource.Resource { return &secretResource{} }

func (r *secretResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_secret"
}

func (r *secretResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	wo := "Write-only: never stored in state or returned; sent on create and when value_wo_version changes."
	resp.Schema = schema.Schema{
		Description: "A shared secret (group or service-account scope) that programs reference by name, or a certificate " +
			"(public certificate in state, private key write-only). Import as <scope>/<owner>/<name>.",
		Attributes: map[string]schema.Attribute{
			"id":               schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"scope":            schema.StringAttribute{Required: true, PlanModifiers: replaceString, Description: "group or sa."},
			"owner":            schema.StringAttribute{Required: true, PlanModifiers: replaceString, Description: "Group name or service account name."},
			"name":             schema.StringAttribute{Required: true, PlanModifiers: replaceString},
			"kind":             schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("string"), PlanModifiers: replaceString, Description: "string or certificate."},
			"description":      schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"value_wo":         schema.StringAttribute{Optional: true, Sensitive: true, WriteOnly: true, Description: "The secret value. " + wo},
			"certificate":      schema.StringAttribute{Optional: true, Description: "PEM certificate (public)."},
			"ca_chain":         schema.StringAttribute{Optional: true, Description: "PEM issuing chain (public)."},
			"private_key_wo":   schema.StringAttribute{Optional: true, Sensitive: true, WriteOnly: true, Description: "PEM private key. " + wo},
			"value_wo_version": schema.Int64Attribute{Optional: true},
			"reference":        schema.StringAttribute{Computed: true, PlanModifiers: keepComputed, Description: "How programs reference it (fdp.json secrets)."},
		},
	}
}

func (r *secretResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *secretResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	parts := strings.SplitN(req.ID, "/", 3)
	if len(parts) != 3 {
		resp.Diagnostics.AddError("Invalid import ID", "use <scope>/<owner>/<name>")
		return
	}
	for i, key := range []string{"scope", "owner", "name"} {
		resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root(key), parts[i])...)
	}
}

func (m *secretModel) target() string {
	return "/v1/secrets/" + m.Scope.ValueString() + "/" + m.Owner.ValueString() + "/" + m.Name.ValueString()
}

func (r *secretResource) put(ctx context.Context, plan, config *secretModel, d *diagSink) {
	body := map[string]any{"kind": plan.Kind.ValueString(), "description": plan.Description.ValueString()}
	if plan.Kind.ValueString() == "certificate" {
		body["certificate"] = plan.Certificate.ValueString() + "\n" + plan.CAChain.ValueString()
		if !config.PrivateKeyWO.IsNull() {
			body["private_key"] = config.PrivateKeyWO.ValueString()
		}
	} else {
		body["value"] = config.ValueWO.ValueString()
	}
	var out secretOut
	if err := r.api.Do(ctx, "PUT", plan.target(), body, &out); err != nil {
		apiError(d.d, "saving secret", err)
		return
	}
	plan.ID, plan.Reference = types.StringValue(out.Scope+"/"+out.Owner+"/"+out.Name), types.StringValue(out.Ref)
	plan.ValueWO, plan.PrivateKeyWO = types.StringNull(), types.StringNull()
}

func (r *secretResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan, config secretModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &plan, &config, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *secretResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state secretModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var all []secretOut
	if err := r.api.Do(ctx, "GET", "/v1/secrets", nil, &all); err != nil {
		apiError(&resp.Diagnostics, "listing secrets", err)
		return
	}
	for _, s := range all {
		if s.Scope == state.Scope.ValueString() && s.Owner == state.Owner.ValueString() && s.Name == state.Name.ValueString() {
			state.ID, state.Reference = types.StringValue(s.Scope+"/"+s.Owner+"/"+s.Name), types.StringValue(s.Ref)
			state.Kind, state.Description = types.StringValue(s.Kind), types.StringValue(s.Description)
			resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
			return
		}
	}
	resp.State.RemoveResource(ctx)
}

func (r *secretResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state, config secretModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	valueChanged := !plan.ValueVersion.Equal(state.ValueVersion) || !plan.Certificate.Equal(state.Certificate) || !plan.CAChain.Equal(state.CAChain)
	if !valueChanged && plan.Kind.ValueString() != "certificate" {
		// Only the description changed: the API stores values and descriptions together, so the
		// value must be resent. Require a version bump instead of silently clearing it.
		if config.ValueWO.IsNull() {
			resp.Diagnostics.AddError("Secret value needed", "changing a secret's description resends its value; set value_wo and bump value_wo_version")
			return
		}
	}
	r.put(ctx, &plan, &config, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *secretResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state secretModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.api.Do(ctx, "DELETE", state.target(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "deleting secret", err)
	}
}
