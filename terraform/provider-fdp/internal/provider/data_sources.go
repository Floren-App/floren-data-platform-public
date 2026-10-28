package provider

import (
	"context"
	"strings"

	"github.com/floren/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// ---- fdp_layer (lookup) ---------------------------------------------------------------------

type layerDataSource struct{ api *client.Client }

type layerLookup struct {
	ID   types.String `tfsdk:"id"`
	Name types.String `tfsdk:"name"`
}

func NewLayerDataSource() datasource.DataSource { return &layerDataSource{} }

func (d *layerDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_layer"
}

func (d *layerDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{Description: "Look up a layer by name (case-insensitive), e.g. \"Non-Prod\".",
		Attributes: map[string]schema.Attribute{"name": schema.StringAttribute{Required: true}, "id": schema.StringAttribute{Computed: true}}}
}

func (d *layerDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	d.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (d *layerDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var m layerLookup
	resp.Diagnostics.Append(req.Config.Get(ctx, &m)...)
	var all []layerOut
	if err := d.api.Do(ctx, "GET", "/v1/layers", nil, &all); err != nil {
		apiError(&resp.Diagnostics, "listing layers", err)
		return
	}
	for _, l := range all {
		if strings.EqualFold(l.Name, m.Name.ValueString()) {
			m.ID, m.Name = types.StringValue(l.ID), types.StringValue(l.Name)
			resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
			return
		}
	}
	resp.Diagnostics.AddError("Layer not found", m.Name.ValueString())
}

// ---- fdp_principal (lookup) ------------------------------------------------------------------

type principalDataSource struct{ api *client.Client }

type principalLookup struct {
	ID   types.String `tfsdk:"id"`
	Name types.String `tfsdk:"name"`
	Kind types.String `tfsdk:"kind"`
}

func NewPrincipalDataSource() datasource.DataSource { return &principalDataSource{} }

func (d *principalDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_principal"
}

func (d *principalDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{Description: "Look up a directory identity (provisioned by SCIM/your IdP) by name.",
		Attributes: map[string]schema.Attribute{
			"name": schema.StringAttribute{Required: true},
			"kind": schema.StringAttribute{Optional: true, Computed: true, Description: "user, group, role or service."},
			"id":   schema.StringAttribute{Computed: true},
		}}
}

func (d *principalDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	d.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (d *principalDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var m principalLookup
	resp.Diagnostics.Append(req.Config.Get(ctx, &m)...)
	var all []struct {
		ID, Name, Kind string
		Active         bool
	}
	if err := d.api.Do(ctx, "GET", "/v1/rbac/directory", nil, &all); err != nil {
		apiError(&resp.Diagnostics, "reading the directory", err)
		return
	}
	for _, p := range all {
		if p.Active && p.Name == m.Name.ValueString() && (m.Kind.IsNull() || m.Kind.ValueString() == p.Kind) {
			m.ID, m.Kind = types.StringValue(p.ID), types.StringValue(p.Kind)
			resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
			return
		}
	}
	resp.Diagnostics.AddError("Directory identity not found", m.Name.ValueString()+" (it must be provisioned and active)")
}
