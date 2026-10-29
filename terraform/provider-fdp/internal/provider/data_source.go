package provider

import (
	"context"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type dataSourceResource struct{ api *client.Client }

type dataSourceModel struct {
	ID                  types.String `tfsdk:"id"`
	Name                types.String `tfsdk:"name"`
	Kind                types.String `tfsdk:"kind"`
	Description         types.String `tfsdk:"description"`
	LayerID             types.String `tfsdk:"layer_id"`
	Config              JSONValue    `tfsdk:"config"`
	Egress              types.List   `tfsdk:"egress"`
	SecretWO            types.Map    `tfsdk:"secret_wo"`
	ProvisionerSecretWO types.Map    `tfsdk:"provisioner_secret_wo"`
	CredentialsVersion  types.Int64  `tfsdk:"credentials_wo_version"`
	ResourceID          types.String `tfsdk:"resource_id"`
}

type dataSourceOut struct {
	ID          string         `json:"id"`
	Name        string         `json:"name"`
	Kind        string         `json:"kind"`
	Description string         `json:"description"`
	Config      map[string]any `json:"config"`
	Egress      []string       `json:"egress"`
	ResourceID  *string        `json:"resource_id"`
}

func NewDataSourceResource() resource.Resource { return &dataSourceResource{} }

func (r *dataSourceResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_data_source"
}

func (r *dataSourceResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	writeOnly := "Write-only: never stored in state or returned by the API. Sent on create and whenever credentials_wo_version changes."
	resp.Schema = schema.Schema{
		Description: "A storage integration (PostgreSQL, Iceberg tables on S3, or S3 files) in one layer. Import by name.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{Computed: true, PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"resource_id": schema.StringAttribute{Computed: true, PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
				Description: "The data source as an RBAC object, for fdp_role_assignment on the whole source."},
			"name": schema.StringAttribute{Required: true, PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"kind": schema.StringAttribute{Required: true, Description: "postgres, iceberg or s3.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"description": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"layer_id": schema.StringAttribute{Optional: true, Computed: true, Description: "Defaults to Non-Prod. Cannot change.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplaceIfConfigured(), stringplanmodifier.UseStateForUnknown()}},
			"config": schema.StringAttribute{Optional: true, Computed: true, CustomType: JSONType{}, PlanModifiers: jsonPlan, Description: "Location settings as JSON (jsonencode), e.g. host/port/database/catalog. " +
				"Only the keys you set are compared."},
			"egress": schema.ListAttribute{ElementType: types.StringType, Optional: true, Computed: true,
				Default: listdefault.StaticValue(listValue(nil)), Description: "Network ranges workloads may reach for this source."},
			"secret_wo": schema.MapAttribute{ElementType: types.StringType, Optional: true, Sensitive: true, WriteOnly: true,
				Description: "Engine/native credential. " + writeOnly},
			"provisioner_secret_wo": schema.MapAttribute{ElementType: types.StringType, Optional: true, Sensitive: true, WriteOnly: true,
				Description: "Access-setup credential. " + writeOnly},
			"credentials_wo_version": schema.Int64Attribute{Optional: true, Description: "Change to send the write-only credentials again."},
		},
	}
}

func (r *dataSourceResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *dataSourceResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("name"), req, resp)
}

func (r *dataSourceResource) find(ctx context.Context, name string) (*dataSourceOut, error) {
	var all []dataSourceOut
	if err := r.api.Do(ctx, "GET", "/v1/admin/datasources", nil, &all); err != nil {
		return nil, err
	}
	for i := range all {
		if all[i].Name == name {
			return &all[i], nil
		}
	}
	return nil, &client.APIError{Status: 404, Detail: "data source not found"}
}

func (m *dataSourceModel) apply(out *dataSourceOut) {
	m.ResourceID = optional(out.ResourceID)
	m.ID, m.Name, m.Kind = types.StringValue(out.ID), types.StringValue(out.Name), types.StringValue(out.Kind)
	m.Description, m.Egress = types.StringValue(out.Description), listValue(out.Egress)
	if layer, ok := out.Config["layer_id"].(string); ok {
		m.LayerID = types.StringValue(layer)
	}
	m.Config = reconcileJSON(m.Config, withoutLayer(out.Config))
}

func withoutLayer(config map[string]any) map[string]any {
	out := map[string]any{}
	for k, v := range config {
		if k != "layer_id" && k != "environment_id" {
			out[k] = v
		}
	}
	return out
}

func (r *dataSourceResource) put(ctx context.Context, plan, config *dataSourceModel, sendSecrets bool, d *diagSink) {
	cfg := decodeJSON(plan.Config, d.d, "config")
	if !plan.LayerID.IsNull() && !plan.LayerID.IsUnknown() {
		cfg["layer_id"] = plan.LayerID.ValueString()
	}
	body := map[string]any{"name": plan.Name.ValueString(), "kind": plan.Kind.ValueString(),
		"description": plan.Description.ValueString(), "config": cfg, "egress": stringList(ctx, plan.Egress, d.d)}
	if sendSecrets {
		if s := stringMap(ctx, config.SecretWO, d.d); len(s) > 0 {
			body["secret"] = s
		}
		if s := stringMap(ctx, config.ProvisionerSecretWO, d.d); len(s) > 0 {
			body["provisioner_secret"] = s
		}
	}
	if d.d.HasError() {
		return
	}
	var out dataSourceOut
	if err := r.api.Do(ctx, "PUT", "/v1/admin/datasources/"+plan.Name.ValueString(), body, &out); err != nil {
		apiError(d.d, "saving data source", err)
		return
	}
	plan.apply(&out)
	plan.SecretWO, plan.ProvisionerSecretWO = types.MapNull(types.StringType), types.MapNull(types.StringType)
}

func (r *dataSourceResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan, config dataSourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if config.Config.IsNull() {
		resp.Diagnostics.AddAttributeError(pathRoot("config"), "Missing location", "config is required: jsonencode({...}) with the settings this kind needs.")
	}
	if resp.Diagnostics.HasError() {
		return
	}
	if _, err := r.find(ctx, plan.Name.ValueString()); err == nil {
		resp.Diagnostics.AddError("Data source already exists", "Import it instead: tofu import <address> "+plan.Name.ValueString())
		return
	}
	r.put(ctx, &plan, &config, true, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *dataSourceResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state dataSourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	out, err := r.find(ctx, state.Name.ValueString())
	if err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading data source", err)
		}
		return
	}
	state.apply(out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *dataSourceResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state, config dataSourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &plan, &config, !plan.CredentialsVersion.Equal(state.CredentialsVersion), &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *dataSourceResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state dataSourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.api.Do(ctx, "DELETE", "/v1/admin/datasources/"+state.Name.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "deleting data source (the storage itself is never touched)", err)
	}
}
