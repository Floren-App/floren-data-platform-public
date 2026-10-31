package provider

import (
	"context"
	"sort"
	"strings"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-framework/types/basetypes"
)

// ---- classifications (the installation's list) ------------------------------------------------------

const classificationsID = "classifications"

type classificationsResource struct{ api *client.Client }

type classificationLevel struct {
	Name        types.String `tfsdk:"name"`
	MaskedAs    types.String `tfsdk:"masked_as"`
	Description types.String `tfsdk:"description"`
}

type classificationsModel struct {
	ID     types.String          `tfsdk:"id"`
	Levels []classificationLevel `tfsdk:"levels"`
}

type levelOut struct {
	Name        string `json:"name"`
	MaskedAs    string `json:"masked_as"`
	Description string `json:"description"`
}

func NewClassificationsResource() resource.Resource { return &classificationsResource{} }

func (r *classificationsResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_classifications"
}

func (r *classificationsResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "The installation's classifications, from least to most sensitive; declare once. Columns and objects " +
			"are classified with these names; access policies refer to them. masked_as says what masked data of a level counts " +
			"as (itself or a lower level). A level still in use cannot be removed. Destroy restores public, internal, " +
			"confidential and restricted. Import with ID \"classifications\".",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"levels": schema.ListNestedAttribute{Required: true, NestedObject: schema.NestedAttributeObject{
				Attributes: map[string]schema.Attribute{
					"name": schema.StringAttribute{Required: true},
					"masked_as": schema.StringAttribute{Optional: true, Computed: true,
						Description: "What masked data of this level counts as; left out: the level itself."},
					"description": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
				}}},
		},
	}
}

func (r *classificationsResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *classificationsResource) ImportState(ctx context.Context, _ resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resp.Diagnostics.Append(resp.State.Set(ctx, &classificationsModel{ID: types.StringValue(classificationsID)})...)
}

func (m *classificationsModel) apply(levels []levelOut) {
	m.ID = types.StringValue(classificationsID)
	m.Levels = nil
	for _, l := range levels {
		m.Levels = append(m.Levels, classificationLevel{Name: types.StringValue(l.Name), MaskedAs: types.StringValue(l.MaskedAs),
			Description: types.StringValue(l.Description)})
	}
}

func (r *classificationsResource) put(ctx context.Context, levels []map[string]any, d *diagSink) []levelOut {
	var out struct {
		Levels []levelOut `json:"levels"`
	}
	if err := r.api.Do(ctx, "PUT", "/v1/catalog/classifications", map[string]any{"levels": levels}, &out); err != nil {
		apiError(d.d, "saving classifications", err)
	}
	return out.Levels
}

func (m *classificationsModel) body() []map[string]any {
	levels := []map[string]any{}
	for _, l := range m.Levels {
		item := map[string]any{"name": l.Name.ValueString(), "description": l.Description.ValueString()}
		if !l.MaskedAs.IsNull() && !l.MaskedAs.IsUnknown() {
			item["masked_as"] = l.MaskedAs.ValueString()
		}
		levels = append(levels, item)
	}
	return levels
}

func (r *classificationsResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan classificationsModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	out := r.put(ctx, plan.body(), &diagSink{&resp.Diagnostics})
	if resp.Diagnostics.HasError() {
		return
	}
	plan.apply(out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *classificationsResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state classificationsModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var out struct {
		Levels []levelOut `json:"levels"`
	}
	if err := r.api.Do(ctx, "GET", "/v1/catalog/classifications", nil, &out); err != nil {
		apiError(&resp.Diagnostics, "reading classifications", err)
		return
	}
	state.apply(out.Levels)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *classificationsResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan classificationsModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	out := r.put(ctx, plan.body(), &diagSink{&resp.Diagnostics})
	if resp.Diagnostics.HasError() {
		return
	}
	plan.apply(out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *classificationsResource) Delete(ctx context.Context, _ resource.DeleteRequest, resp *resource.DeleteResponse) {
	defaults := []map[string]any{{"name": "public"}, {"name": "internal"}, {"name": "confidential", "masked_as": "internal"},
		{"name": "restricted", "masked_as": "internal"}}
	r.put(ctx, defaults, &diagSink{&resp.Diagnostics})
}

// ---- column classification ---------------------------------------------------------------------------

type columnClassificationResource struct{ api *client.Client }

type columnClassModel struct {
	ID         types.String `tfsdk:"id"`
	ResourceID types.String `tfsdk:"resource_id"`
	ObjectPath types.String `tfsdk:"object_path"`
	Columns    types.Map    `tfsdk:"columns"`
}

var columnType = types.ObjectType{AttrTypes: map[string]attr.Type{"classification": types.StringType, "tags": types.SetType{ElemType: types.StringType}}}

func NewColumnClassificationResource() resource.Resource { return &columnClassificationResource{} }

func (r *columnClassificationResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_column_classification"
}

func (r *columnClassificationResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Classification and tags of a table's columns. The table's level follows the highest of its columns (and " +
			"is never below them); masked columns count as their level's masked_as for access. Only the declared columns are " +
			"managed; destroy clears them. Import with \"<schema resource id>/<table>\".",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Computed: true, PlanModifiers: keepComputed, Description: "Catalog asset ID of the table."},
			"resource_id": schema.StringAttribute{Required: true, PlanModifiers: replaceString, Description: "The schema resource."},
			"object_path": schema.StringAttribute{Required: true, PlanModifiers: replaceString, Description: "The table or view."},
			"columns": schema.MapNestedAttribute{Required: true, Description: "Per column name (lower case).",
				NestedObject: schema.NestedAttributeObject{Attributes: map[string]schema.Attribute{
					"classification": schema.StringAttribute{Required: true},
					"tags":           schema.SetAttribute{ElementType: types.StringType, Optional: true, Computed: true},
				}}},
		},
	}
}

func (r *columnClassificationResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *columnClassificationResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	parts := strings.SplitN(req.ID, "/", 2)
	if len(parts) != 2 {
		resp.Diagnostics.AddError("Invalid import ID", "use <schema resource id>/<table>")
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("resource_id"), parts[0])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("object_path"), parts[1])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("columns"), types.MapNull(columnType))...)
}

type columnGov struct {
	Classification string   `json:"classification"`
	Tags           []string `json:"tags"`
}

func (r *columnClassificationResource) declared(ctx context.Context, m *columnClassModel, d *diagSink) map[string]columnGov {
	out := map[string]columnGov{}
	if m.Columns.IsNull() || m.Columns.IsUnknown() {
		return out
	}
	elements := map[string]types.Object{}
	d.d.Append(m.Columns.ElementsAs(ctx, &elements, false)...)
	for name, obj := range elements {
		var item struct {
			Classification types.String `tfsdk:"classification"`
			Tags           types.Set    `tfsdk:"tags"`
		}
		d.d.Append(obj.As(ctx, &item, basetypes.ObjectAsOptions{})...)
		tags := []string{}
		if !item.Tags.IsNull() && !item.Tags.IsUnknown() {
			tags = stringSet(ctx, item.Tags, d.d)
		}
		out[strings.ToLower(name)] = columnGov{Classification: item.Classification.ValueString(), Tags: tags}
	}
	return out
}

func (m *columnClassModel) apply(columns map[string]columnGov, keep map[string]columnGov) {
	m.ID = types.StringValue(catalogObjectID(m.ResourceID.ValueString(), m.ObjectPath.ValueString()))
	values := map[string]attr.Value{}
	for name := range keep {
		got, ok := columns[name]
		if !ok {
			got = columnGov{Classification: "unclassified", Tags: []string{}}
		}
		tags := append([]string{}, got.Tags...)
		sort.Strings(tags)
		values[name] = types.ObjectValueMust(columnType.AttrTypes, map[string]attr.Value{
			"classification": types.StringValue(got.Classification), "tags": setValue(tags)})
	}
	m.Columns = types.MapValueMust(columnType, values)
}

func (r *columnClassificationResource) send(ctx context.Context, m *columnClassModel, columns map[string]columnGov, d *diagSink) map[string]columnGov {
	if err := ensureAsset(ctx, r.api, m.ResourceID.ValueString(), m.ObjectPath.ValueString()); err != nil {
		apiError(d.d, "declaring the table in the catalog", err)
		return nil
	}
	body := map[string]any{}
	for name, c := range columns {
		body[name] = map[string]any{"classification": c.Classification, "tags": c.Tags}
	}
	var out struct {
		Classification struct {
			Columns map[string]columnGov `json:"columns"`
		} `json:"classification"`
	}
	target := catalogObjectID(m.ResourceID.ValueString(), m.ObjectPath.ValueString())
	if err := r.api.Do(ctx, "PUT", "/v1/catalog/assets/"+target+"/columns", map[string]any{"columns": body}, &out); err != nil {
		apiError(d.d, "classifying columns", err)
		return nil
	}
	return out.Classification.Columns
}

func (r *columnClassificationResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan columnClassModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	d := &diagSink{&resp.Diagnostics}
	declared := r.declared(ctx, &plan, d)
	got := r.send(ctx, &plan, declared, d)
	if resp.Diagnostics.HasError() {
		return
	}
	plan.apply(got, declared)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *columnClassificationResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state columnClassModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	d := &diagSink{&resp.Diagnostics}
	var out struct {
		Classification struct {
			Columns map[string]columnGov `json:"columns"`
		} `json:"classification"`
	}
	target := catalogObjectID(state.ResourceID.ValueString(), state.ObjectPath.ValueString())
	if err := r.api.Do(ctx, "GET", "/v1/catalog/assets/"+target, nil, &out); err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading column classification", err)
		}
		return
	}
	keep := r.declared(ctx, &state, d)
	if state.Columns.IsNull() { // imported: take every classified column
		keep = out.Classification.Columns
	}
	state.apply(out.Classification.Columns, keep)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *columnClassificationResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state columnClassModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	d := &diagSink{&resp.Diagnostics}
	declared := r.declared(ctx, &plan, d)
	send := map[string]columnGov{}
	for name := range r.declared(ctx, &state, d) { // columns no longer declared are cleared
		send[name] = columnGov{Classification: "unclassified", Tags: []string{}}
	}
	for name, c := range declared {
		send[name] = c
	}
	got := r.send(ctx, &plan, send, d)
	if resp.Diagnostics.HasError() {
		return
	}
	plan.apply(got, declared)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *columnClassificationResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state columnClassModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	d := &diagSink{&resp.Diagnostics}
	clear := map[string]columnGov{}
	for name := range r.declared(ctx, &state, d) {
		clear[name] = columnGov{Classification: "unclassified", Tags: []string{}}
	}
	if len(clear) > 0 {
		r.send(ctx, &state, clear, d)
	}
}
