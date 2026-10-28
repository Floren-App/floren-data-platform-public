package provider

import (
	"context"

	"github.com/floren/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type workspaceTemplateResource struct{ api *client.Client }

type workspaceTemplateModel struct {
	ID           types.String `tfsdk:"id"`
	Name         types.String `tfsdk:"name"`
	GroupID      types.String `tfsdk:"group_id"`
	Editor       types.String `tfsdk:"editor"`
	LayerID      types.String `tfsdk:"layer_id"`
	Description  types.String `tfsdk:"description"`
	Repositories types.List   `tfsdk:"repository_ids"`
}

type workspaceTemplateOut struct {
	ID           string   `json:"id"`
	Name         string   `json:"name"`
	GroupID      string   `json:"group_id"`
	Editor       string   `json:"editor"`
	LayerID      string   `json:"layer_id"`
	Description  string   `json:"description"`
	Repositories []string `json:"repositories"`
}

func NewWorkspaceTemplateResource() resource.Resource { return &workspaceTemplateResource{} }

func (r *workspaceTemplateResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_workspace_template"
}

func (r *workspaceTemplateResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "A group's default workspace. Every member of the app group gets their own private workspace from it, " +
			"created stopped, including people who join later; leaving the group removes theirs (files stay in its volume). " +
			"Destroy removes the template's workspaces. Import by ID.",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"name":        schema.StringAttribute{Required: true, Description: "The workspace name each member sees."},
			"group_id":    schema.StringAttribute{Required: true, Description: "The app group whose members get it."},
			"editor":      schema.StringAttribute{Required: true, Description: "vscode, rstudio or sql."},
			"layer_id":    schema.StringAttribute{Required: true, Description: "A layer that allows workspaces."},
			"description": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"repository_ids": schema.ListAttribute{ElementType: types.StringType, Optional: true, Computed: true,
				Default: listdefault.StaticValue(listValue(nil)), Description: "Repositories cloned at first start."},
		},
	}
}

func (r *workspaceTemplateResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *workspaceTemplateResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func (m *workspaceTemplateModel) apply(out *workspaceTemplateOut) {
	m.ID, m.Name, m.GroupID = types.StringValue(out.ID), types.StringValue(out.Name), types.StringValue(out.GroupID)
	m.Editor, m.LayerID, m.Description = types.StringValue(out.Editor), types.StringValue(out.LayerID), types.StringValue(out.Description)
	m.Repositories = listValue(out.Repositories)
}

func (r *workspaceTemplateResource) body(ctx context.Context, m *workspaceTemplateModel, d *diagSink) map[string]any {
	return map[string]any{"name": m.Name.ValueString(), "group_id": m.GroupID.ValueString(), "editor": m.Editor.ValueString(),
		"layer_id": m.LayerID.ValueString(), "description": m.Description.ValueString(),
		"repositories": stringList(ctx, m.Repositories, d.d)}
}

func (r *workspaceTemplateResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan workspaceTemplateModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out workspaceTemplateOut
	if err := r.api.Do(ctx, "POST", "/v1/admin/workspace-templates", r.body(ctx, &plan, &diagSink{&resp.Diagnostics}), &out); err != nil {
		apiError(&resp.Diagnostics, "creating workspace template", err)
		return
	}
	plan.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *workspaceTemplateResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state workspaceTemplateModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var out workspaceTemplateOut
	if err := r.api.Do(ctx, "GET", "/v1/admin/workspace-templates/"+state.ID.ValueString(), nil, &out); err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading workspace template", err)
		}
		return
	}
	state.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *workspaceTemplateResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state workspaceTemplateModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out workspaceTemplateOut
	if err := r.api.Do(ctx, "PUT", "/v1/admin/workspace-templates/"+state.ID.ValueString(),
		r.body(ctx, &plan, &diagSink{&resp.Diagnostics}), &out); err != nil {
		apiError(&resp.Diagnostics, "updating workspace template", err)
		return
	}
	plan.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *workspaceTemplateResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state workspaceTemplateModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.api.Do(ctx, "DELETE", "/v1/admin/workspace-templates/"+state.ID.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "deleting workspace template", err)
	}
}
