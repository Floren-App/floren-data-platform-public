package provider

import (
	"context"
	"net/url"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// An app profile: which parts of the app (pages, navigation and the actions behind them) a group gets.
type appProfileResource struct{ api *client.Client }

type appProfileModel struct {
	ID          types.String `tfsdk:"id"`
	Name        types.String `tfsdk:"name"`
	Description types.String `tfsdk:"description"`
	Features    types.Set    `tfsdk:"features"`
	Default     types.Bool   `tfsdk:"default"`
}

type appProfileOut struct {
	ID          string   `json:"id"`
	Name        string   `json:"name"`
	Description string   `json:"description"`
	Features    []string `json:"features"`
	Default     bool     `json:"default"`
}

func NewAppProfileResource() resource.Resource { return &appProfileResource{} }

func (r *appProfileResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_app_profile"
}

func (r *appProfileResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "An app profile: the features (pages, navigation and the API actions behind them) a group gets. Give it to " +
			"groups with fdp_app_group.app_profile. People get the profile of their active group; Global Admin has everything; " +
			"object permissions still decide what can be opened. Features: products.browse, products.request, products.publish, " +
			"catalog.objects, workspaces, workloads.view, workloads.manage, storage, packages, kafka, apis, usage, security. " +
			"A feature brings what it needs (products.request brings products.browse). Import by name.",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"name":        schema.StringAttribute{Required: true},
			"description": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"features": schema.SetAttribute{ElementType: types.StringType, Required: true,
				Description: "Feature keys; the server adds what they need, so declare those too to avoid drift."},
			"default": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false),
				Description: "The profile of groups without one. At most one profile is the default; making this one the default " +
					"takes it from the other. Without a default, groups without a profile get the whole app."},
		},
	}
}

func (r *appProfileResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *appProfileResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("name"), req, resp)
}

func (m *appProfileModel) apply(out *appProfileOut) {
	m.ID, m.Name, m.Description = types.StringValue(out.ID), types.StringValue(out.Name), types.StringValue(out.Description)
	m.Features, m.Default = setValue(out.Features), types.BoolValue(out.Default)
}

func (r *appProfileResource) put(ctx context.Context, current string, m *appProfileModel, d *diagSink) {
	body := map[string]any{"name": m.Name.ValueString(), "description": m.Description.ValueString(),
		"features": stringSet(ctx, m.Features, d.d), "default": m.Default.ValueBool()}
	var out appProfileOut
	if err := r.api.Do(ctx, "PUT", "/v1/admin/app-profiles/"+url.PathEscape(current), body, &out); err != nil {
		apiError(d.d, "saving app profile", err)
		return
	}
	m.apply(&out)
}

func (r *appProfileResource) find(ctx context.Context, id, name string) (*appProfileOut, error) {
	var listed struct {
		Profiles []appProfileOut `json:"profiles"`
	}
	if err := r.api.Do(ctx, "GET", "/v1/app-profiles", nil, &listed); err != nil {
		return nil, err
	}
	for i := range listed.Profiles {
		if (id != "" && listed.Profiles[i].ID == id) || (id == "" && listed.Profiles[i].Name == name) {
			return &listed.Profiles[i], nil
		}
	}
	return nil, &client.APIError{Status: 404, Detail: "app profile not found"}
}

func (r *appProfileResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan appProfileModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, plan.Name.ValueString(), &plan, &diagSink{&resp.Diagnostics})
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *appProfileResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state appProfileModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	id := ""
	if !state.ID.IsNull() && !state.ID.IsUnknown() {
		id = state.ID.ValueString()
	}
	out, err := r.find(ctx, id, state.Name.ValueString())
	if err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading app profile", err)
		}
		return
	}
	state.apply(out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *appProfileResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state appProfileModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, state.Name.ValueString(), &plan, &diagSink{&resp.Diagnostics}) // a new name renames it
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *appProfileResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state appProfileModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.api.Do(ctx, "DELETE", "/v1/admin/app-profiles/"+url.PathEscape(state.Name.ValueString()), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "deleting app profile", err)
	}
}
