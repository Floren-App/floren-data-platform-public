package provider

import (
	"context"
	"fmt"
	"strings"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type runtimeResource struct{ api *client.Client }

type runtimeModel struct {
	ID          types.String `tfsdk:"id"`
	Kind        types.String `tfsdk:"kind"`
	Version     types.String `tfsdk:"version"`
	Image       types.String `tfsdk:"image"`
	Description types.String `tfsdk:"description"`
	Default     types.Bool   `tfsdk:"default"`
	Enabled     types.Bool   `tfsdk:"enabled"`
}

type runtimeOut struct {
	Kind         string `json:"kind"`
	Version      string `json:"version"`
	Image        string `json:"image"`
	Description  string `json:"description"`
	IsDefault    bool   `json:"is_default"`
	Enabled      bool   `json:"enabled"`
	Installation bool   `json:"installation"`
}

func NewRuntimeResource() resource.Resource { return &runtimeResource{} }

func (r *runtimeResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_runtime"
}

func (r *runtimeResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	replace := []planmodifier.String{stringplanmodifier.RequiresReplace()}
	resp.Schema = schema.Schema{
		Description: "An additional runtime version people can choose in fdp.json (e.g. Python 3.11 next to the installation's " +
			"3.12), with its image. The versions the installation ships come from the Helm values (runtime.images, " +
			"runtime.versions) and follow them on every upgrade; do not declare those here. Import as kind/version.",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"kind":        schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "python, r, dbt or airflow."},
			"version":     schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "e.g. 3.11."},
			"image":       schema.StringAttribute{Required: true, Description: "The compute image providing this version."},
			"description": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"default": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false),
				Description: "Make it the version used when fdp.json names none."},
			"enabled": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true)},
		},
	}
}

func (r *runtimeResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *runtimeResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	kind, version, ok := strings.Cut(req.ID, "/")
	if !ok {
		resp.Diagnostics.AddError("Invalid import ID", "use kind/version, e.g. python/3.11")
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), req.ID)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("kind"), kind)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("version"), version)...)
}

func (m *runtimeModel) apply(out *runtimeOut) {
	m.ID = types.StringValue(out.Kind + "/" + out.Version)
	m.Kind, m.Version, m.Image = types.StringValue(out.Kind), types.StringValue(out.Version), types.StringValue(out.Image)
	m.Description, m.Default, m.Enabled = types.StringValue(out.Description), types.BoolValue(out.IsDefault), types.BoolValue(out.Enabled)
}

func (r *runtimeResource) put(ctx context.Context, m *runtimeModel, d *diagSink) {
	body := map[string]any{"image": m.Image.ValueString(), "description": m.Description.ValueString(),
		"is_default": m.Default.ValueBool(), "enabled": m.Enabled.ValueBool()}
	var out runtimeOut
	if err := r.api.Do(ctx, "PUT", "/v1/admin/runtimes/"+m.Kind.ValueString()+"/"+m.Version.ValueString(), body, &out); err != nil {
		apiError(d.d, "saving runtime", err)
		return
	}
	m.apply(&out)
}

func (r *runtimeResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan runtimeModel
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

func (r *runtimeResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state runtimeModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var rows []runtimeOut
	if err := r.api.Do(ctx, "GET", "/v1/admin/runtimes", nil, &rows); err != nil {
		apiError(&resp.Diagnostics, "reading runtimes", err)
		return
	}
	for i := range rows {
		if rows[i].Kind == state.Kind.ValueString() && rows[i].Version == state.Version.ValueString() {
			if rows[i].Installation {
				resp.Diagnostics.AddWarning("Runtime comes with the installation",
					fmt.Sprintf("%s %s is shipped by the Helm values; remove it from the configuration (it is not managed here).",
						rows[i].Kind, rows[i].Version))
			}
			state.apply(&rows[i])
			resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
			return
		}
	}
	resp.State.RemoveResource(ctx)
}

func (r *runtimeResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan runtimeModel
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

func (r *runtimeResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state runtimeModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	err := r.api.Do(ctx, "DELETE", "/v1/admin/runtimes/"+state.Kind.ValueString()+"/"+state.Version.ValueString(), nil, nil)
	if err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "removing runtime (make another version the default first)", err)
	}
}
