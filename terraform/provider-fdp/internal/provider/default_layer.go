package provider

import (
	"context"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type defaultLayerResource struct{ api *client.Client }

type defaultLayerModel struct {
	ID      types.String `tfsdk:"id"`
	LayerID types.String `tfsdk:"layer_id"`
}

type defaultLayerOut struct {
	LayerID    *string `json:"layer_id"`
	Configured bool    `json:"configured"`
}

func NewDefaultLayerResource() resource.Resource { return &defaultLayerResource{} }

func (r *defaultLayerResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_default_layer"
}

func (r *defaultLayerResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "The layer that work without a layer goes to (declare it once). Without it, an installation with one " +
			"layer uses that layer and one with several asks for a layer. The default layer cannot be deleted. " +
			"Import with ID \"default\".",
		Attributes: map[string]schema.Attribute{
			"id":       schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"layer_id": schema.StringAttribute{Required: true},
		},
	}
}

func (r *defaultLayerResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *defaultLayerResource) ImportState(ctx context.Context, _ resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resp.Diagnostics.Append(resp.State.Set(ctx, &defaultLayerModel{ID: types.StringValue("default"), LayerID: types.StringNull()})...)
}

func (r *defaultLayerResource) put(ctx context.Context, m *defaultLayerModel, d *diagSink) {
	if err := r.api.Do(ctx, "PUT", "/v1/admin/settings/default-layer", map[string]any{"layer_id": m.LayerID.ValueString()}, nil); err != nil {
		apiError(d.d, "setting the default layer", err)
		return
	}
	m.ID = types.StringValue("default")
}

func (r *defaultLayerResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan defaultLayerModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &plan, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *defaultLayerResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state defaultLayerModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var out defaultLayerOut
	if err := r.api.Do(ctx, "GET", "/v1/admin/settings/default-layer", nil, &out); err != nil {
		apiError(&resp.Diagnostics, "reading the default layer", err)
		return
	}
	if !out.Configured || out.LayerID == nil {
		resp.State.RemoveResource(ctx) // cleared outside configuration: plan sets it again
		return
	}
	state.ID, state.LayerID = types.StringValue("default"), types.StringValue(*out.LayerID)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *defaultLayerResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan defaultLayerModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &plan, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *defaultLayerResource) Delete(ctx context.Context, _ resource.DeleteRequest, resp *resource.DeleteResponse) {
	if err := r.api.Do(ctx, "DELETE", "/v1/admin/settings/default-layer", nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "clearing the default layer", err)
	}
}
