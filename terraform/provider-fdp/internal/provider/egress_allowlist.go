package provider

import (
	"context"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"net/url"
	"sort"
	"strings"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// egressAllowlistID is the only instance: the allowlist belongs to the installation.
const egressAllowlistID = "egress"

type egressAllowlistResource struct{ api *client.Client }

type egressAllowlistModel struct {
	ID      types.String `tfsdk:"id"`
	LayerID types.String `tfsdk:"layer_id"`
	Hosts   types.Map    `tfsdk:"hosts"`
}

type egressHost struct {
	Host        string `json:"host"`
	Description string `json:"description"`
}

type egressAllowlistOut struct {
	Hosts   []egressHost `json:"hosts"`
	LayerID *string      `json:"layer_id"`
}

func NewEgressAllowlistResource() resource.Resource { return &egressAllowlistResource{} }

func (r *egressAllowlistResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_egress_allowlist"
}

func (r *egressAllowlistResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "A global HTTPS host ceiling or one layer's editor host list (Helm egressProxy.enabled). " +
			"Both must allow a destination. New connections require a signed workspace ticket and verified source address. " +
			"Private destinations and public package indexes remain blocked. Destroy empties this scope's list. " +
			"Import as egress (global ceiling) or egress:LAYER_ID.",
		Attributes: map[string]schema.Attribute{
			"id":       schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"layer_id": schema.StringAttribute{Optional: true, PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}, Description: "Layer policy inside the global host ceiling. Omit only for the explicit global ceiling (Owner required). Import as egress:LAYER_ID."},
			"hosts": schema.MapAttribute{ElementType: types.StringType, Required: true,
				Description: "Host name (lowercase) to a description: \"github.com\", or \"*.githubusercontent.com\" for its subdomains."},
		},
	}
}

func (r *egressAllowlistResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *egressAllowlistResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	m := egressAllowlistModel{ID: types.StringValue(req.ID), LayerID: types.StringNull(), Hosts: types.MapNull(types.StringType)}
	if req.ID != egressAllowlistID {
		prefix, layer, ok := strings.Cut(req.ID, ":")
		if !ok || prefix != "egress" || layer == "" {
			resp.Diagnostics.AddError("Invalid import ID", "Use egress or egress:LAYER_ID")
			return
		}
		m.LayerID = types.StringValue(layer)
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}
func (m *egressAllowlistModel) endpoint() string {
	if !m.LayerID.IsNull() && m.LayerID.ValueString() != "" {
		return "/v1/admin/settings/egress?layer_id=" + url.QueryEscape(m.LayerID.ValueString())
	}
	return "/v1/admin/settings/egress"
}

func (m *egressAllowlistModel) apply(out *egressAllowlistOut) {
	hosts := map[string]string{}
	for _, h := range out.Hosts {
		hosts[h.Host] = h.Description
	}
	m.ID, m.Hosts = types.StringValue(egressAllowlistID), mapValue(hosts)
	m.LayerID = permissionString(out.LayerID)
	if out.LayerID != nil {
		m.ID = types.StringValue(egressAllowlistID + ":" + *out.LayerID)
	}
}

func (r *egressAllowlistResource) put(ctx context.Context, hosts map[string]string, m *egressAllowlistModel, d *diagSink) {
	names := make([]string, 0, len(hosts))
	for host := range hosts {
		if host != strings.ToLower(host) {
			d.d.AddError("Invalid host name", "write host names in lowercase: "+host)
			return
		}
		names = append(names, host)
	}
	sort.Strings(names)
	entries := make([]egressHost, 0, len(names))
	for _, host := range names {
		entries = append(entries, egressHost{Host: host, Description: hosts[host]})
	}
	var out egressAllowlistOut
	if err := r.api.Do(ctx, "PUT", m.endpoint(), map[string]any{"hosts": entries}, &out); err != nil {
		apiError(d.d, "saving the egress allowlist", err)
		return
	}
	if m != nil {
		m.apply(&out)
	}
}

func (r *egressAllowlistResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan egressAllowlistModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, stringMap(ctx, plan.Hosts, &resp.Diagnostics), &plan, &diagSink{&resp.Diagnostics})
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *egressAllowlistResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state egressAllowlistModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var out egressAllowlistOut
	if err := r.api.Do(ctx, "GET", state.endpoint(), nil, &out); err != nil {
		apiError(&resp.Diagnostics, "reading the egress allowlist", err)
		return
	}
	state.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *egressAllowlistResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan egressAllowlistModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, stringMap(ctx, plan.Hosts, &resp.Diagnostics), &plan, &diagSink{&resp.Diagnostics})
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *egressAllowlistResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state egressAllowlistModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, map[string]string{}, &state, &diagSink{&resp.Diagnostics})
}
