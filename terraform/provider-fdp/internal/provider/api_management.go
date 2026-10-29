package provider

import (
	"context"
	"strings"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type apiOut struct {
	ID          string         `json:"id"`
	Slug        string         `json:"slug"`
	Name        string         `json:"name"`
	Direction   string         `json:"direction"`
	Protocol    string         `json:"protocol"`
	Description string         `json:"description"`
	Config      map[string]any `json:"config"`
	ResourceID  string         `json:"resource_id"`
	Endpoint    string         `json:"endpoint"`
	Identity    *struct {
		PrincipalID string `json:"principal_id"`
	} `json:"identity"`
	Operations []apiOperationOut `json:"operations"`
}

type apiOperationOut struct {
	ID          string         `json:"id"`
	Name        string         `json:"name"`
	Group       string         `json:"group"`
	Method      string         `json:"method"`
	Path        string         `json:"path"`
	SoapAction  string         `json:"soap_action"`
	Description string         `json:"description"`
	Config      map[string]any `json:"config"`
	ResourceID  string         `json:"resource_id"`
}

// ---- API definitions -----------------------------------------------------------------------------

type apiResource struct{ api *client.Client }

type apiModel struct {
	ID                  types.String `tfsdk:"id"`
	Name                types.String `tfsdk:"name"`
	Slug                types.String `tfsdk:"slug"`
	Direction           types.String `tfsdk:"direction"`
	Protocol            types.String `tfsdk:"protocol"`
	Description         types.String `tfsdk:"description"`
	LayerID             types.String `tfsdk:"layer_id"`
	Inbound             JSONValue    `tfsdk:"inbound"`
	Outbound            JSONValue    `tfsdk:"outbound"`
	Endpoint            types.String `tfsdk:"endpoint"`
	ResourceID          types.String `tfsdk:"resource_id"`
	IdentityPrincipalID types.String `tfsdk:"identity_principal_id"`
}

func NewAPIResource() resource.Resource { return &apiResource{} }

func (r *apiResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_api"
}

func (r *apiResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "An internal API (deployed programs behind JWT/mTLS) or an external API (outbound calls with " +
			"server-side credentials). Credentials and certificates are secret references, never values. Import by ID.",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"name":        schema.StringAttribute{Required: true},
			"slug":        schema.StringAttribute{Required: true, PlanModifiers: replaceString},
			"direction":   schema.StringAttribute{Required: true, PlanModifiers: replaceString, Description: "internal or external."},
			"protocol":    schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("rest"), PlanModifiers: replaceString},
			"description": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"layer_id": schema.StringAttribute{Optional: true, Computed: true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplaceIfConfigured(), stringplanmodifier.UseStateForUnknown()}},
			"inbound":     schema.StringAttribute{Optional: true, Computed: true, CustomType: JSONType{}, PlanModifiers: jsonPlan, Description: "Internal APIs: jsonencode({ jwt = {...}, mtls = {...} }). Only declared keys are compared."},
			"outbound":    schema.StringAttribute{Optional: true, Computed: true, CustomType: JSONType{}, PlanModifiers: jsonPlan, Description: "External APIs: jsonencode({ base_url, headers, auth = {...}, ... })."},
			"endpoint":    schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"resource_id": schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"identity_principal_id": schema.StringAttribute{Computed: true, PlanModifiers: keepComputed,
				Description: "Internal APIs: the managed identity their programs run as; grant it Executor."},
		},
	}
}

func (r *apiResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *apiResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func (m *apiModel) apply(out *apiOut) {
	m.ID, m.Name, m.Slug = types.StringValue(out.ID), types.StringValue(out.Name), types.StringValue(out.Slug)
	m.Direction, m.Protocol, m.Description = types.StringValue(out.Direction), types.StringValue(out.Protocol), types.StringValue(out.Description)
	if layer, ok := out.Config["layer_id"].(string); ok {
		m.LayerID = types.StringValue(layer)
	}
	m.Inbound, m.Outbound = reconcileJSON(m.Inbound, out.Config["inbound"]), reconcileJSON(m.Outbound, out.Config["outbound"])
	m.Endpoint, m.ResourceID = types.StringValue(out.Endpoint), types.StringValue(out.ResourceID)
	m.IdentityPrincipalID = types.StringNull()
	if out.Identity != nil {
		m.IdentityPrincipalID = types.StringValue(out.Identity.PrincipalID)
	}
}

func (r *apiResource) body(m *apiModel, d *diagSink) map[string]any {
	body := map[string]any{"name": m.Name.ValueString(), "slug": m.Slug.ValueString(), "direction": m.Direction.ValueString(),
		"protocol": m.Protocol.ValueString(), "description": m.Description.ValueString(),
		"inbound": decodeJSON(m.Inbound, d.d, "inbound"), "outbound": decodeJSON(m.Outbound, d.d, "outbound")}
	if !m.LayerID.IsNull() && !m.LayerID.IsUnknown() {
		body["layer_id"] = m.LayerID.ValueString()
	}
	return body
}

func (r *apiResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan apiModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out apiOut
	if err := r.api.Do(ctx, "POST", "/v1/apis", r.body(&plan, &diagSink{&resp.Diagnostics}), &out); err != nil {
		apiError(&resp.Diagnostics, "creating API", err)
		return
	}
	plan.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *apiResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state apiModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var out apiOut
	if err := r.api.Do(ctx, "GET", "/v1/apis/"+state.ID.ValueString(), nil, &out); err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading API", err)
		}
		return
	}
	state.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *apiResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state apiModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var out apiOut
	if err := r.api.Do(ctx, "PUT", "/v1/apis/"+state.ID.ValueString(), r.body(&plan, &diagSink{&resp.Diagnostics}), &out); err != nil {
		apiError(&resp.Diagnostics, "updating API", err)
		return
	}
	plan.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *apiResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state apiModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.api.Do(ctx, "DELETE", "/v1/apis/"+state.ID.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "deleting API", err)
	}
}

// ---- API operations ------------------------------------------------------------------------------

type apiOperationResource struct{ api *client.Client }

type apiOperationModel struct {
	ID          types.String `tfsdk:"id"`
	APIID       types.String `tfsdk:"api_id"`
	Name        types.String `tfsdk:"name"`
	Group       types.String `tfsdk:"group"`
	Method      types.String `tfsdk:"method"`
	Path        types.String `tfsdk:"path"`
	SoapAction  types.String `tfsdk:"soap_action"`
	Description types.String `tfsdk:"description"`
	Config      JSONValue    `tfsdk:"config"`
	ResourceID  types.String `tfsdk:"resource_id"`
}

func NewAPIOperationResource() resource.Resource { return &apiOperationResource{} }

func (r *apiOperationResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_api_operation"
}

func (r *apiOperationResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "One operation of an API. Internal: the deployed program it runs (same layer) and how request " +
			"fields map to arguments. External: extra headers and timeout. Import as <api_id>/<operation_id>.",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"api_id":      schema.StringAttribute{Required: true, PlanModifiers: replaceString},
			"name":        schema.StringAttribute{Required: true},
			"group":       schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString(""), Description: "Permission group within the API."},
			"method":      schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("GET")},
			"path":        schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("/")},
			"soap_action": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"description": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"config": schema.StringAttribute{Optional: true, Computed: true, CustomType: JSONType{}, PlanModifiers: jsonPlan, Description: "Internal: jsonencode({ workload_id, mode, args, ... }); " +
				"external: jsonencode({ headers, timeout_seconds }). Only declared keys are compared."},
			"resource_id": schema.StringAttribute{Computed: true, PlanModifiers: keepComputed, Description: "Grant Consumer/Caller roles on this."},
		},
	}
}

func (r *apiOperationResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *apiOperationResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	apiID, opID, ok := strings.Cut(req.ID, "/")
	if !ok {
		resp.Diagnostics.AddError("Invalid import ID", "use <api_id>/<operation_id>")
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("api_id"), apiID)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), opID)...)
}

func (m *apiOperationModel) apply(out *apiOperationOut) {
	m.ID, m.Name, m.Group, m.Method = types.StringValue(out.ID), types.StringValue(out.Name), types.StringValue(out.Group), types.StringValue(out.Method)
	m.Path, m.SoapAction, m.Description = types.StringValue(out.Path), types.StringValue(out.SoapAction), types.StringValue(out.Description)
	m.Config, m.ResourceID = reconcileJSON(m.Config, out.Config), types.StringValue(out.ResourceID)
}

func (r *apiOperationResource) body(ctx context.Context, m *apiOperationModel, d *diagSink) map[string]any {
	body := map[string]any{"name": m.Name.ValueString(), "group": m.Group.ValueString(), "method": m.Method.ValueString(),
		"path": m.Path.ValueString(), "soap_action": m.SoapAction.ValueString(), "description": m.Description.ValueString()}
	var parent apiOut
	if err := r.api.Do(ctx, "GET", "/v1/apis/"+m.APIID.ValueString(), nil, &parent); err != nil {
		apiError(d.d, "reading the API", err)
		return body
	}
	body[parent.Direction] = decodeJSON(m.Config, d.d, "config")
	return body
}

func (r *apiOperationResource) find(ctx context.Context, m *apiOperationModel) (*apiOperationOut, error) {
	var parent apiOut
	if err := r.api.Do(ctx, "GET", "/v1/apis/"+m.APIID.ValueString(), nil, &parent); err != nil {
		return nil, err
	}
	for i := range parent.Operations {
		if parent.Operations[i].ID == m.ID.ValueString() {
			return &parent.Operations[i], nil
		}
	}
	return nil, &client.APIError{Status: 404, Detail: "operation not found"}
}

func (r *apiOperationResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan apiOperationModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := r.body(ctx, &plan, &diagSink{&resp.Diagnostics})
	if resp.Diagnostics.HasError() {
		return
	}
	var out apiOperationOut
	if err := r.api.Do(ctx, "POST", "/v1/apis/"+plan.APIID.ValueString()+"/operations", body, &out); err != nil {
		apiError(&resp.Diagnostics, "creating API operation", err)
		return
	}
	plan.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *apiOperationResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state apiOperationModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	out, err := r.find(ctx, &state)
	if err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading API operation", err)
		}
		return
	}
	state.apply(out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *apiOperationResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state apiOperationModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	plan.ID = state.ID
	body := r.body(ctx, &plan, &diagSink{&resp.Diagnostics})
	if resp.Diagnostics.HasError() {
		return
	}
	var out apiOperationOut
	if err := r.api.Do(ctx, "PUT", "/v1/apis/"+plan.APIID.ValueString()+"/operations/"+state.ID.ValueString(), body, &out); err != nil {
		apiError(&resp.Diagnostics, "updating API operation", err)
		return
	}
	plan.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *apiOperationResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state apiOperationModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	err := r.api.Do(ctx, "DELETE", "/v1/apis/"+state.APIID.ValueString()+"/operations/"+state.ID.ValueString(), nil, nil)
	if err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "deleting API operation", err)
	}
}
