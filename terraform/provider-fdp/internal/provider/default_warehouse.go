package provider

import (
	"context"
	"net/url"
	"strings"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type defaultWarehouseResource struct{ api *client.Client }

type defaultWarehouseModel struct {
	ID           types.String `tfsdk:"id"`
	Name         types.String `tfsdk:"name"`
	LayerID      types.String `tfsdk:"layer_id"`
	WorkloadKind types.String `tfsdk:"workload_kind"`
}

type defaultWarehouseOut struct {
	Name *string `json:"name"`
}

func NewDefaultWarehouseResource() resource.Resource { return &defaultWarehouseResource{} }

func (r *defaultWarehouseResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_default_warehouse"
}

func (r *defaultWarehouseResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	replace := []planmodifier.String{stringplanmodifier.RequiresReplace()}
	resp.Schema = schema.Schema{
		Description: "The warehouse that work without one runs on, and that warehouse choices start with. Without layer_id and " +
			"workload_kind it is the installation default (declare it once); with either or both it is the default for that " +
			"layer and/or kind of work. The most specific match wins: layer and kind, the layer, the kind, then the installation " +
			"default. It cannot be a dedicated warehouse or one that runs Trino workers, a scoped default must be meant for its " +
			"layer and kind, and a warehouse cannot be deleted while it is a default. " +
			"Import with ID \"default\", or \"<layer id>/<kind>\" using \"*\" for either part (e.g. \"1a0000000001/dbt\", \"*/dbt\").",
		Attributes: map[string]schema.Attribute{
			"id":   schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"name": schema.StringAttribute{Required: true, Description: "The warehouse's name."},
			"layer_id": schema.StringAttribute{Optional: true, PlanModifiers: replace,
				Description: "Only for work in this layer (fdp_layer id)."},
			"workload_kind": schema.StringAttribute{Optional: true, PlanModifiers: replace,
				Description: "Only for this kind of work: script, notebook, app, dbt or airflow."},
		},
	}
}

func (r *defaultWarehouseResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (m *defaultWarehouseModel) scoped() bool {
	return m.LayerID.ValueString() != "" || m.WorkloadKind.ValueString() != ""
}

// scopeID is "default" for the installation default, otherwise "<layer|*>/<kind|*>".
func (m *defaultWarehouseModel) scopeID() string {
	if !m.scoped() {
		return "default"
	}
	part := func(v types.String) string {
		if v.ValueString() == "" {
			return "*"
		}
		return v.ValueString()
	}
	return part(m.LayerID) + "/" + part(m.WorkloadKind)
}

func (m *defaultWarehouseModel) query() string {
	q := url.Values{}
	if v := m.LayerID.ValueString(); v != "" {
		q.Set("layer_id", v)
	}
	if v := m.WorkloadKind.ValueString(); v != "" {
		q.Set("workload_kind", v)
	}
	if len(q) == 0 {
		return ""
	}
	return "?" + q.Encode()
}

func (r *defaultWarehouseResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	m := defaultWarehouseModel{ID: types.StringValue("default"), Name: types.StringNull(), LayerID: types.StringNull(), WorkloadKind: types.StringNull()}
	if req.ID != "default" && req.ID != "" {
		layer, kind, ok := strings.Cut(req.ID, "/")
		if !ok {
			resp.Diagnostics.AddError("Invalid import ID", "use \"default\" or \"<layer id>/<kind>\", with \"*\" for either part")
			return
		}
		if layer != "*" {
			m.LayerID = types.StringValue(layer)
		}
		if kind != "*" {
			m.WorkloadKind = types.StringValue(kind)
		}
		m.ID = types.StringValue(m.scopeID())
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *defaultWarehouseResource) put(ctx context.Context, m *defaultWarehouseModel, d *diagSink) {
	body := map[string]any{"name": m.Name.ValueString()}
	if v := m.LayerID.ValueString(); v != "" {
		body["layer_id"] = v
	}
	if v := m.WorkloadKind.ValueString(); v != "" {
		body["workload_kind"] = v
	}
	if err := r.api.Do(ctx, "PUT", "/v1/admin/settings/default-warehouse", body, nil); err != nil {
		apiError(d.d, "setting the default warehouse", err)
		return
	}
	m.ID = types.StringValue(m.scopeID())
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
	if err := r.api.Do(ctx, "GET", "/v1/admin/settings/default-warehouse"+state.query(), nil, &out); err != nil {
		apiError(&resp.Diagnostics, "reading the default warehouse", err)
		return
	}
	if out.Name == nil {
		resp.State.RemoveResource(ctx) // cleared outside configuration: plan sets it again
		return
	}
	state.ID, state.Name = types.StringValue(state.scopeID()), types.StringValue(*out.Name)
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

func (r *defaultWarehouseResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state defaultWarehouseModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.api.Do(ctx, "DELETE", "/v1/admin/settings/default-warehouse"+state.query(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "clearing the default warehouse", err)
	}
}
