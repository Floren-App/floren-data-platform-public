package provider

import (
	"context"
	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type dataObjectResource struct{ api *client.Client }
type dataObjectModel struct {
	ID       types.String `tfsdk:"id"`
	ParentID types.String `tfsdk:"parent_id"`
	Kind     types.String `tfsdk:"kind"`
	Name     types.String `tfsdk:"name"`
	SourceID types.String `tfsdk:"source_id"`
}
type dataObjectOut struct {
	ID       string `json:"id"`
	ParentID string `json:"parent_id"`
	Kind     string `json:"kind"`
	Name     string `json:"name"`
	SourceID string `json:"source_id"`
}

func NewDataObjectResource() resource.Resource { return &dataObjectResource{} }
func (r *dataObjectResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_data_object"
}
func (r *dataObjectResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	replace := []planmodifier.String{stringplanmodifier.RequiresReplace()}
	resp.Schema = schema.Schema{Description: "A stable permission target for a table, column, S3 file or directory. Metadata registration grants no data access. Destroy unregisters this target and revokes its assignments; it never deletes physical data. Children must be removed first. Import with the object ID.", Attributes: map[string]schema.Attribute{
		"id":        schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
		"parent_id": schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "Schema ID for a table or top-level file/directory; table ID for a column; directory ID for a nested file/directory."},
		"kind":      schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "table, column, file or directory."},
		"name":      schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "Exact SQL identifier or one file/directory path component. No path traversal or wildcard characters."},
		"source_id": schema.StringAttribute{Computed: true, Description: "Server-derived data source ID."},
	}}
}
func (r *dataObjectResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}
func (r *dataObjectResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}
func (m *dataObjectModel) apply(out *dataObjectOut) {
	m.ID = types.StringValue(out.ID)
	m.ParentID = types.StringValue(out.ParentID)
	m.Kind = types.StringValue(out.Kind)
	m.Name = types.StringValue(out.Name)
	m.SourceID = types.StringValue(out.SourceID)
}
func (r *dataObjectResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m dataObjectModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := map[string]any{"parent_id": m.ParentID.ValueString(), "kind": m.Kind.ValueString(), "name": m.Name.ValueString()}
	var out dataObjectOut
	if err := r.api.Do(ctx, "POST", "/v1/data-objects", body, &out); err != nil {
		apiError(&resp.Diagnostics, "registering data object", err)
		return
	}
	m.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}
func (r *dataObjectResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m dataObjectModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out dataObjectOut
	if err := r.api.Do(ctx, "GET", "/v1/data-objects/"+m.ID.ValueString(), nil, &out); err != nil {
		if client.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
		} else {
			apiError(&resp.Diagnostics, "reading data object", err)
		}
		return
	}
	m.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}
func (r *dataObjectResource) Update(_ context.Context, _ resource.UpdateRequest, resp *resource.UpdateResponse) {
	resp.Diagnostics.AddError("Replacement required", "Data object identity fields are immutable.")
}
func (r *dataObjectResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m dataObjectModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.api.Do(ctx, "DELETE", "/v1/data-objects/"+m.ID.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "unregistering data object", err)
	}
}
