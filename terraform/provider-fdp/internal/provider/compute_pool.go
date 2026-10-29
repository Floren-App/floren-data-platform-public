package provider

import (
	"context"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64default"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type computePoolResource struct{ api *client.Client }

type computePoolModel struct {
	ID                 types.String  `tfsdk:"id"`
	Name               types.String  `tfsdk:"name"`
	CPU                types.Float64 `tfsdk:"cpu"`
	MemoryGB           types.Float64 `tfsdk:"memory_gb"`
	GPU                types.Int64   `tfsdk:"gpu"`
	MaxConcurrent      types.Int64   `tfsdk:"max_concurrent"`
	AutoSuspendSeconds types.Int64   `tfsdk:"auto_suspend_seconds"`
	AllowedGroups      types.List    `tfsdk:"allowed_groups"`
	Dedicated          types.Bool    `tfsdk:"dedicated"`
	LayerIDs           types.List    `tfsdk:"layer_ids"`
	WorkloadKinds      types.List    `tfsdk:"workload_kinds"`
}

type poolOut struct {
	ID                 string   `json:"id"`
	Name               string   `json:"name"`
	CPU                float64  `json:"cpu"`
	MemoryGB           float64  `json:"memory_gb"`
	GPU                int64    `json:"gpu"`
	MaxConcurrent      int64    `json:"max_concurrent"`
	AutoSuspendSeconds int64    `json:"auto_suspend_seconds"`
	AllowedGroups      []string `json:"allowed_groups"`
	Dedicated          bool     `json:"dedicated"`
	LayerIDs           []string `json:"layer_ids"`
	WorkloadKinds      []string `json:"workload_kinds"`
}

func NewComputePoolResource() resource.Resource { return &computePoolResource{} }

func (r *computePoolResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_compute_pool"
}

func (r *computePoolResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "A warehouse (compute pool): the size each job, app or Airflow component gets, and how many may run at once. Import by name.",
		Attributes: map[string]schema.Attribute{
			"id":        schema.StringAttribute{Computed: true, PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"name":      schema.StringAttribute{Required: true, PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"cpu":       schema.Float64Attribute{Required: true},
			"memory_gb": schema.Float64Attribute{Required: true},
			"gpu":       schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(0)},
			"max_concurrent": schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(10),
				Description: "Containers running on this warehouse at once: each job, app and Airflow component counts one. More wait in line and start in order. Workspaces do not count."},
			"auto_suspend_seconds": schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(600)},
			"allowed_groups": schema.ListAttribute{ElementType: types.StringType, Optional: true, Computed: true,
				Default: listdefault.StaticValue(listValue(nil)), Description: "Empty: everyone."},
			"dedicated": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false),
				Description: "Dedicated to allowed_groups (required): not a shared on-demand warehouse, so max_concurrent does not apply " +
					"and its work never queues or counts elsewhere. Make sure the cluster has nodes for it."},
			"layer_ids": schema.ListAttribute{ElementType: types.StringType, Optional: true, Computed: true,
				Default:     listdefault.StaticValue(listValue(nil)),
				Description: "Layers whose work may use this warehouse (fdp_layer ids). Empty: every layer."},
			"workload_kinds": schema.ListAttribute{ElementType: types.StringType, Optional: true, Computed: true,
				Default: listdefault.StaticValue(listValue(nil)),
				Description: "Kinds of work that may use it: script, notebook, app, dbt, airflow. Empty: every kind. " +
					"With allowed_groups: \"dbt projects of the data engineers in Prod\". A declared warehouse outside the scope " +
					"falls back to the default for the layer and kind (fdp_default_warehouse)."},
		},
	}
}

func (r *computePoolResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *computePoolResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("name"), req, resp)
}

func (r *computePoolResource) put(ctx context.Context, m *computePoolModel, diags *diagSink) {
	body := map[string]any{"name": m.Name.ValueString(), "cpu": m.CPU.ValueFloat64(), "memory_gb": m.MemoryGB.ValueFloat64(),
		"gpu": m.GPU.ValueInt64(), "max_concurrent": m.MaxConcurrent.ValueInt64(),
		"auto_suspend_seconds": m.AutoSuspendSeconds.ValueInt64(), "allowed_groups": stringList(ctx, m.AllowedGroups, diags.d),
		"dedicated": m.Dedicated.ValueBool(), "layer_ids": stringList(ctx, m.LayerIDs, diags.d),
		"workload_kinds": stringList(ctx, m.WorkloadKinds, diags.d)}
	var out poolOut
	if err := r.api.Do(ctx, "PUT", "/v1/admin/pools/"+m.Name.ValueString(), body, &out); err != nil {
		apiError(diags.d, "saving compute pool", err)
		return
	}
	m.apply(&out)
}

func (m *computePoolModel) apply(out *poolOut) {
	m.ID, m.Name = types.StringValue(out.ID), types.StringValue(out.Name)
	m.CPU, m.MemoryGB = types.Float64Value(out.CPU), types.Float64Value(out.MemoryGB)
	m.GPU, m.MaxConcurrent = types.Int64Value(out.GPU), types.Int64Value(out.MaxConcurrent)
	m.AutoSuspendSeconds, m.AllowedGroups = types.Int64Value(out.AutoSuspendSeconds), listValue(out.AllowedGroups)
	m.Dedicated = types.BoolValue(out.Dedicated)
	m.LayerIDs, m.WorkloadKinds = listValue(out.LayerIDs), listValue(out.WorkloadKinds)
}

func (r *computePoolResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan computePoolModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var existing []poolOut
	if err := r.api.Do(ctx, "GET", "/v1/admin/pools", nil, &existing); err == nil {
		for _, p := range existing {
			if p.Name == plan.Name.ValueString() {
				resp.Diagnostics.AddError("Compute pool already exists", "Import it instead: tofu import <address> "+p.Name)
				return
			}
		}
	}
	r.put(ctx, &plan, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *computePoolResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state computePoolModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var all []poolOut
	if err := r.api.Do(ctx, "GET", "/v1/admin/pools", nil, &all); err != nil {
		apiError(&resp.Diagnostics, "reading compute pools", err)
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

func (r *computePoolResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan computePoolModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &plan, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *computePoolResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state computePoolModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.api.Do(ctx, "DELETE", "/v1/admin/pools/"+state.Name.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "deleting compute pool", err)
	}
}
