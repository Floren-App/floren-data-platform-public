package provider

import (
	"context"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/boolplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type schemaResource struct{ api *client.Client }

type schemaModel struct {
	ID         types.String `tfsdk:"id"`
	Source     types.String `tfsdk:"data_source"`
	Name       types.String `tfsdk:"name"`
	Prefix     types.String `tfsdk:"prefix"`
	Provision  types.Bool   `tfsdk:"provision"`
	AppendOnly types.Bool   `tfsdk:"append_only"`
	State      types.String `tfsdk:"state"`
	LayerID    types.String `tfsdk:"layer_id"`
}

type resourceOut struct {
	ID       string         `json:"id"`
	Name     string         `json:"name"`
	Kind     string         `json:"kind"`
	ParentID string         `json:"parent_id"`
	ObjectID string         `json:"object_id"`
	Config   map[string]any `json:"config"`
	LayerID  *string        `json:"layer_id"`
}

func NewSchemaResource() resource.Resource { return &schemaResource{} }

func (r *schemaResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_schema"
}

func (r *schemaResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	replace := []planmodifier.String{stringplanmodifier.RequiresReplace()}
	resp.Schema = schema.Schema{
		Description: "A governed schema (SQL) or namespace (S3 prefix) of a data source. Destroying it stops governing it; " +
			"the data itself is never touched. Import by ID.",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Computed: true, PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"data_source": schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "Data source name."},
			"name":        schema.StringAttribute{Required: true, PlanModifiers: replace},
			"prefix":      schema.StringAttribute{Optional: true, PlanModifiers: replace, Description: "S3 files only."},
			"provision": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true),
				Description: "Set up access in the source after registering (roles/policies)."},
			"append_only": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false),
				PlanModifiers: []planmodifier.Bool{boolplanmodifier.RequiresReplace()},
				Description:   "Trino SQL schemas: permit inserts and initial table creation, refuse destructive writes/DDL. Changing this contract requires re-registration."},
			"state":    schema.StringAttribute{Computed: true},
			"layer_id": schema.StringAttribute{Computed: true},
		},
	}
}

func (r *schemaResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *schemaResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("provision"), types.BoolValue(true))...)
}

func (m *schemaModel) apply(out *resourceOut, source string) {
	m.AppendOnly = types.BoolValue(out.Config["append_only"] == true)
	m.ID, m.Name = types.StringValue(out.ID), types.StringValue(out.Name)
	if source != "" {
		m.Source = types.StringValue(source)
	}
	state, _ := out.Config["state"].(string)
	m.State = types.StringValue(state)
	if prefix, ok := out.Config["prefix"].(string); ok {
		m.Prefix = types.StringValue(prefix)
	}
	m.LayerID = types.StringNull()
	if out.LayerID != nil {
		m.LayerID = types.StringValue(*out.LayerID)
	}
}

func (r *schemaResource) sourceName(ctx context.Context, out *resourceOut) string {
	var parent resourceOut
	if err := r.api.Do(ctx, "GET", "/v1/rbac/resources/"+out.ParentID, nil, &parent); err == nil {
		return parent.Name
	}
	return ""
}

func (r *schemaResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan schemaModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := map[string]any{"name": plan.Name.ValueString(), "append_only": plan.AppendOnly.ValueBool()}
	if !plan.Prefix.IsNull() {
		body["prefix"] = plan.Prefix.ValueString()
	}
	var out resourceOut
	if err := r.api.Do(ctx, "POST", "/v1/rbac/sources/"+plan.Source.ValueString()+"/schemas", body, &out); err != nil {
		apiError(&resp.Diagnostics, "registering schema", err)
		return
	}
	plan.apply(&out, "")
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
	if plan.Provision.ValueBool() {
		if err := r.api.Do(ctx, "POST", "/v1/rbac/resources/"+out.ID+"/provision", nil, &out); err != nil {
			apiError(&resp.Diagnostics, "setting up schema access", err)
		}
		if err := r.api.Do(ctx, "GET", "/v1/rbac/resources/"+out.ID, nil, &out); err == nil {
			plan.apply(&out, "")
		}
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *schemaResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state schemaModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var out resourceOut
	if err := r.api.Do(ctx, "GET", "/v1/rbac/resources/"+state.ID.ValueString(), nil, &out); err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading schema", err)
		}
		return
	}
	source := ""
	if state.Source.IsNull() || state.Source.ValueString() == "" {
		source = r.sourceName(ctx, &out)
	}
	state.apply(&out, source)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *schemaResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state schemaModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	plan.ID, plan.State, plan.LayerID = state.ID, state.State, state.LayerID
	if plan.Provision.ValueBool() && !state.Provision.ValueBool() {
		var out resourceOut
		if err := r.api.Do(ctx, "POST", "/v1/rbac/resources/"+plan.ID.ValueString()+"/provision", nil, &out); err != nil {
			apiError(&resp.Diagnostics, "setting up schema access", err)
		}
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *schemaResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state schemaModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.api.Do(ctx, "DELETE", "/v1/rbac/resources/"+state.ID.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "deregistering schema", err)
	}
}
