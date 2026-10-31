package provider

import (
	"context"
	"encoding/json"
	"strings"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// ---- data products ----------------------------------------------------------------------------------

type dataProductResource struct{ api *client.Client }

type productMember struct {
	ResourceID types.String `tfsdk:"resource_id"`
	ObjectPath types.String `tfsdk:"object_path"`
}

type dataProductModel struct {
	ID               types.String    `tfsdk:"id"`
	AccessResourceID types.String    `tfsdk:"access_resource_id"`
	Title            types.String    `tfsdk:"title"`
	Summary          types.String    `tfsdk:"summary"`
	Members          []productMember `tfsdk:"members"`
	Options          JSONValue       `tfsdk:"options"`
}

type productOut struct {
	ID      string `json:"id"`
	Title   string `json:"title"`
	Summary string `json:"summary"`
	Members []struct {
		ResourceID string  `json:"resource_id"`
		ObjectPath *string `json:"object_path"`
	} `json:"members"`
	Options        []map[string]any `json:"options"`
	AccessResource *struct {
		ID string `json:"id"`
	} `json:"access_resource"`
}

func NewDataProductResource() resource.Resource { return &dataProductResource{} }

func (r *dataProductResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_data_product"
}

func (r *dataProductResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "A data product: tables and views, dashboards, APIs or Kafka topics you own, published together. Everyone " +
			"in its layer sees its card; groups get access as Consumers of the product (fdp_data_product_consumer) with one " +
			"of its access options. The first member leads (the card's icon). Domain and classification come from the " +
			"members' governance (fdp_catalog_governance, fdp_column_classification). Import by product ID.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"access_resource_id": schema.StringAttribute{Computed: true, PlanModifiers: keepComputed,
				Description: "The product's own RBAC resource (Consumer and Owner roles)."},
			"title":   schema.StringAttribute{Required: true},
			"summary": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"members": schema.ListNestedAttribute{Required: true, Description: "Objects in the product, lead first.",
				NestedObject: schema.NestedAttributeObject{Attributes: map[string]schema.Attribute{
					"resource_id": schema.StringAttribute{Required: true, Description: "A schema (with object_path), workload, API or topic resource."},
					"object_path": schema.StringAttribute{Optional: true, Description: "A table or view in the schema."},
				}}},
			"options": schema.StringAttribute{Optional: true, Computed: true, CustomType: JSONType{}, PlanModifiers: jsonPlan,
				Description: "jsonencode([{ name, description, default, masks = [{ resource_id, table, column, method, expression }], " +
					"filters = [{ resource_id, table, expression }] }]). method: null, redact, hash, partial or expression. Left out: " +
					"one Standard option without masks. Only declared keys are compared."},
		},
	}
}

func (r *dataProductResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *dataProductResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func (r *dataProductResource) body(m *dataProductModel, d *diagSink) map[string]any {
	members := []map[string]any{}
	for _, item := range m.Members {
		ref := map[string]any{"resource_id": item.ResourceID.ValueString()}
		if !item.ObjectPath.IsNull() && item.ObjectPath.ValueString() != "" {
			ref["object_path"] = item.ObjectPath.ValueString()
		}
		members = append(members, ref)
	}
	body := map[string]any{"title": m.Title.ValueString(), "summary": m.Summary.ValueString(), "members": members}
	if !m.Options.IsNull() && !m.Options.IsUnknown() && m.Options.ValueString() != "" {
		var options []any
		if err := json.Unmarshal([]byte(m.Options.ValueString()), &options); err != nil {
			d.d.AddError("Invalid options", "options must be a JSON list: "+err.Error())
		}
		body["options"] = options
	}
	return body
}

func (m *dataProductModel) apply(out *productOut) {
	m.ID, m.Title, m.Summary = types.StringValue(out.ID), types.StringValue(out.Title), types.StringValue(out.Summary)
	if out.AccessResource != nil {
		m.AccessResourceID = types.StringValue(out.AccessResource.ID)
	}
	m.Members = nil
	for _, item := range out.Members {
		member := productMember{ResourceID: types.StringValue(item.ResourceID), ObjectPath: types.StringNull()}
		if item.ObjectPath != nil && *item.ObjectPath != "" {
			member.ObjectPath = types.StringValue(*item.ObjectPath)
		}
		m.Members = append(m.Members, member)
	}
	options := make([]any, 0, len(out.Options))
	for _, o := range out.Options {
		options = append(options, o)
	}
	m.Options = reconcileJSON(m.Options, options)
}

func (r *dataProductResource) read(ctx context.Context, id string, m *dataProductModel) error {
	var out productOut
	if err := r.api.Do(ctx, "GET", "/v1/catalog/products/"+id, nil, &out); err != nil {
		return err
	}
	m.apply(&out)
	return nil
}

func (r *dataProductResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan dataProductModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := r.body(&plan, &diagSink{&resp.Diagnostics})
	if resp.Diagnostics.HasError() {
		return
	}
	var created productOut
	if err := r.api.Do(ctx, "POST", "/v1/catalog/products", body, &created); err != nil {
		apiError(&resp.Diagnostics, "publishing data product", err)
		return
	}
	if err := r.read(ctx, created.ID, &plan); err != nil {
		apiError(&resp.Diagnostics, "reading data product", err)
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *dataProductResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state dataProductModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.read(ctx, state.ID.ValueString(), &state); err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading data product", err)
		}
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *dataProductResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state dataProductModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := r.body(&plan, &diagSink{&resp.Diagnostics})
	if _, ok := body["options"]; !ok {
		body["options"] = []any{} // left out: one Standard option
	}
	if err := r.api.Do(ctx, "PATCH", "/v1/catalog/products/"+state.ID.ValueString(), body, nil); err != nil {
		apiError(&resp.Diagnostics, "changing data product", err)
		return
	}
	if err := r.read(ctx, state.ID.ValueString(), &plan); err != nil {
		apiError(&resp.Diagnostics, "reading data product", err)
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *dataProductResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state dataProductModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.api.Do(ctx, "DELETE", "/v1/catalog/products/"+state.ID.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "unpublishing data product", err)
	}
}

// ---- consumers of a data product ----------------------------------------------------------------------

type productConsumerResource struct{ api *client.Client }

type productConsumerModel struct {
	ID         types.String `tfsdk:"id"`
	ProductID  types.String `tfsdk:"product_id"`
	AppGroupID types.String `tfsdk:"app_group_id"`
	Option     types.String `tfsdk:"option"`
}

type productDetail struct {
	Options []struct {
		ID      string `json:"id"`
		Name    string `json:"name"`
		Default bool   `json:"default"`
	} `json:"options"`
	Consumers []struct {
		GroupID string `json:"group_id"`
		Option  string `json:"option"`
	} `json:"consumers"`
}

func NewProductConsumerResource() resource.Resource { return &productConsumerResource{} }

func (r *productConsumerResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_data_product_consumer"
}

func (r *productConsumerResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "An app group's access to a data product: Consumer on the product with one of its access options (its " +
			"masks and row filters). The group may then use everything in the product. Destroy takes the access away. " +
			"Import with \"<product id>/<app group id>\".",
		Attributes: map[string]schema.Attribute{
			"id":           schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"product_id":   schema.StringAttribute{Required: true, PlanModifiers: replaceString},
			"app_group_id": schema.StringAttribute{Required: true, PlanModifiers: replaceString},
			"option": schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: keepComputed,
				Description: "Name of the access option; left out: the product's default option."},
		},
	}
}

func (r *productConsumerResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *productConsumerResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	parts := strings.SplitN(req.ID, "/", 2)
	if len(parts) != 2 {
		resp.Diagnostics.AddError("Invalid import ID", "use <product id>/<app group id>")
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("product_id"), parts[0])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("app_group_id"), parts[1])...)
}

func (r *productConsumerResource) detail(ctx context.Context, productID string) (*productDetail, error) {
	var out productDetail
	err := r.api.Do(ctx, "GET", "/v1/catalog/products/"+productID, nil, &out)
	return &out, err
}

func (r *productConsumerResource) put(ctx context.Context, m *productConsumerModel, d *diagSink) {
	detail, err := r.detail(ctx, m.ProductID.ValueString())
	if err != nil {
		apiError(d.d, "reading data product", err)
		return
	}
	body := map[string]any{}
	if !m.Option.IsNull() && !m.Option.IsUnknown() && m.Option.ValueString() != "" {
		for _, o := range detail.Options {
			if o.Name == m.Option.ValueString() {
				body["option_id"] = o.ID
			}
		}
		if body["option_id"] == nil {
			d.d.AddError("Unknown option", "the product has no access option named "+m.Option.ValueString())
			return
		}
	}
	var out struct {
		Option string `json:"option"`
	}
	target := "/v1/catalog/products/" + m.ProductID.ValueString() + "/consumers/" + m.AppGroupID.ValueString()
	if err := r.api.Do(ctx, "PUT", target, body, &out); err != nil {
		apiError(d.d, "giving access to the data product", err)
		return
	}
	m.ID = types.StringValue(m.ProductID.ValueString() + "/" + m.AppGroupID.ValueString())
	m.Option = types.StringValue(out.Option)
}

func (r *productConsumerResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan productConsumerModel
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

func (r *productConsumerResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state productConsumerModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	detail, err := r.detail(ctx, state.ProductID.ValueString())
	if err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading data product", err)
		}
		return
	}
	for _, c := range detail.Consumers {
		if c.GroupID == state.AppGroupID.ValueString() {
			state.ID = types.StringValue(state.ProductID.ValueString() + "/" + c.GroupID)
			state.Option = types.StringValue(c.Option)
			resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
			return
		}
	}
	resp.State.RemoveResource(ctx)
}

func (r *productConsumerResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan productConsumerModel
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

func (r *productConsumerResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state productConsumerModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	target := "/v1/catalog/products/" + state.ProductID.ValueString() + "/consumers/" + state.AppGroupID.ValueString()
	if err := r.api.Do(ctx, "DELETE", target, nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "taking access to the data product away", err)
	}
}
