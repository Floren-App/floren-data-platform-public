package provider

import (
	"context"

	"github.com/floren/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type defaultWarehouseResource struct{ api *client.Client }

type defaultWarehouseModel struct {
	ID   types.String `tfsdk:"id"`
	Name types.String `tfsdk:"name"`
}

type defaultWarehouseOut struct {
	Name *string `json:"name"`
}

func NewDefaultWarehouseResource() resource.Resource { return &defaultWarehouseResource{} }

func (r *defaultWarehouseResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_default_warehouse"
}

func (r *defaultWarehouseResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "The warehouse that work without one runs on, and that warehouse choices start with (declare it once). " +
			"It cannot be a dedicated warehouse or one that runs Trino workers, and it cannot be deleted while it is the default. " +
			"Import with ID \"default\".",
		Attributes: map[string]schema.Attribute{
			"id":   schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"name": schema.StringAttribute{Required: true, Description: "The warehouse's name."},
		},
	}
}

func (r *defaultWarehouseResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *defaultWarehouseResource) ImportState(ctx context.Context, _ resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resp.Diagnostics.Append(resp.State.Set(ctx, &defaultWarehouseModel{ID: types.StringValue("default"), Name: types.StringNull()})...)
}

func (r *defaultWarehouseResource) put(ctx context.Context, m *defaultWarehouseModel, d *diagSink) {
	if err := r.api.Do(ctx, "PUT", "/v1/admin/settings/default-warehouse", map[string]any{"name": m.Name.ValueString()}, nil); err != nil {
		apiError(d.d, "setting the default warehouse", err)
		return
	}
	m.ID = types.StringValue("default")
}

func (r *defaultWarehouseResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan defaultWarehouseModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &plan, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *defaultWarehouseResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state defaultWarehouseModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var out defaultWarehouseOut
	if err := r.api.Do(ctx, "GET", "/v1/admin/settings/default-warehouse", nil, &out); err != nil {
		apiError(&resp.Diagnostics, "reading the default warehouse", err)
		return
	}
	if out.Name == nil {
		resp.State.RemoveResource(ctx) // cleared outside configuration: plan sets it again
		return
	}
	state.ID, state.Name = types.StringValue("default"), types.StringValue(*out.Name)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *defaultWarehouseResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan defaultWarehouseModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &plan, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *defaultWarehouseResource) Delete(ctx context.Context, _ resource.DeleteRequest, resp *resource.DeleteResponse) {
	if err := r.api.Do(ctx, "DELETE", "/v1/admin/settings/default-warehouse", nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "clearing the default warehouse", err)
	}
}
