package provider

import (
	"context"
	"os"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/provider"
	"github.com/hashicorp/terraform-plugin-framework/provider/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type fdpProvider struct{ version string }

type providerModel struct {
	Endpoint      types.String `tfsdk:"endpoint"`
	Token         types.String `tfsdk:"token"`
	TokenURL      types.String `tfsdk:"token_url"`
	ClientID      types.String `tfsdk:"client_id"`
	ClientSecret  types.String `tfsdk:"client_secret"`
	CAFile        types.String `tfsdk:"ca_file"`
	ActiveGroupID types.String `tfsdk:"owner_group_id"`
}

func New(version string) func() provider.Provider {
	return func() provider.Provider { return &fdpProvider{version: version} }
}

func (p *fdpProvider) Metadata(_ context.Context, _ provider.MetadataRequest, resp *provider.MetadataResponse) {
	resp.TypeName, resp.Version = "fdp", p.version
}

func (p *fdpProvider) Schema(_ context.Context, _ provider.SchemaRequest, resp *provider.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Configures Floren Data Platform through its API. Authenticate with a token, or with OAuth client " +
			"credentials of an automation identity (for example the chart's fdp-automation client).",
		Attributes: map[string]schema.Attribute{
			"endpoint":       schema.StringAttribute{Optional: true, Description: "API base URL, e.g. https://fdp.example.com/api. Env: FDP_ENDPOINT."},
			"token":          schema.StringAttribute{Optional: true, Sensitive: true, Description: "Bearer token. Env: FDP_TOKEN."},
			"token_url":      schema.StringAttribute{Optional: true, Description: "OAuth token endpoint for client credentials. Env: FDP_TOKEN_URL."},
			"client_id":      schema.StringAttribute{Optional: true, Description: "Env: FDP_CLIENT_ID."},
			"client_secret":  schema.StringAttribute{Optional: true, Sensitive: true, Description: "Env: FDP_CLIENT_SECRET."},
			"ca_file":        schema.StringAttribute{Optional: true, Description: "PEM bundle to trust for the API's TLS certificate. Env: FDP_CA_FILE."},
			"owner_group_id": schema.StringAttribute{Optional: true, Description: "App group that owns created objects. Env: FDP_OWNER_GROUP_ID."},
		},
	}
}

func pick(value types.String, env string) string {
	if !value.IsNull() && !value.IsUnknown() && value.ValueString() != "" {
		return value.ValueString()
	}
	return os.Getenv(env)
}

func (p *fdpProvider) Configure(ctx context.Context, req provider.ConfigureRequest, resp *provider.ConfigureResponse) {
	var cfg providerModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &cfg)...)
	if resp.Diagnostics.HasError() {
		return
	}
	c, err := client.New(client.Config{
		Endpoint: pick(cfg.Endpoint, "FDP_ENDPOINT"), Token: pick(cfg.Token, "FDP_TOKEN"),
		TokenURL: pick(cfg.TokenURL, "FDP_TOKEN_URL"), ClientID: pick(cfg.ClientID, "FDP_CLIENT_ID"),
		ClientSecret: pick(cfg.ClientSecret, "FDP_CLIENT_SECRET"), CAFile: pick(cfg.CAFile, "FDP_CA_FILE"),
		ActiveGroupID: pick(cfg.ActiveGroupID, "FDP_OWNER_GROUP_ID"),
	})
	if err != nil {
		resp.Diagnostics.AddError("Invalid Floren Data Platform provider configuration", err.Error())
		return
	}
	resp.ResourceData, resp.DataSourceData = c, c
}

func (p *fdpProvider) Resources(context.Context) []func() resource.Resource {
	return []func() resource.Resource{
		NewLayerResource, NewComputePoolResource, NewDataSourceResource, NewSchemaResource, NewAppGroupResource,
		NewKafkaClusterResource, NewKafkaTopicResource,
		NewAppGroupMemberResource, NewRoleAssignmentResource, NewRepositoryResource, NewWorkloadResource,
		NewFolderResource, NewServiceAccountResource, NewSecretResource, NewAPIResource, NewAPIOperationResource,
		NewGovernanceResource, NewSLAResource, NewMetricResource, NewWorkspaceSettingsResource, NewDefaultLayerResource, NewDefaultWarehouseResource, NewWorkspaceTemplateResource,
		NewAppProfileResource, NewNotificationWebhookResource, NewAlertChannelResource, NewAlertRuleResource, NewAccessFormResource, NewAccessPolicyResource, NewComplianceRoleResource, NewClassificationsResource, NewColumnClassificationResource, NewDataProductResource, NewProductConsumerResource,
		NewEgressAllowlistResource, NewRuntimeResource, NewQueryLogSettingsResource, NewPackageAllowlistResource, NewPackageResource, NewDataPolicyResource,
	}
}

func (p *fdpProvider) DataSources(context.Context) []func() datasource.DataSource {
	return []func() datasource.DataSource{NewLayerDataSource, NewPrincipalDataSource}
}
