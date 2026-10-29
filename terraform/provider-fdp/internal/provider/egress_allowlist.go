package provider

import (
	"context"
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
	ID    types.String `tfsdk:"id"`
	Hosts types.Map    `tfsdk:"hosts"`
}

type egressHost struct {
	Host        string `json:"host"`
	Description string `json:"description"`
}

type egressAllowlistOut struct {
	Hosts []egressHost `json:"hosts"`
}

func NewEgressAllowlistResource() resource.Resource { return &egressAllowlistResource{} }

func (r *egressAllowlistResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_egress_allowlist"
}

func (r *egressAllowlistResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Host names VS Code and RStudio workspaces may reach over HTTPS, through the platform's egress proxy " +
			"(declare it once; Helm egressProxy.enabled). Everything else on the internet stays unreachable, and names " +
			"resolving to private addresses are refused. Changes apply to running workspaces within seconds. " +
			"Destroy empties the list. Import with ID \"egress\".",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"hosts": schema.MapAttribute{ElementType: types.StringType, Required: true,
				Description: "Host name (lowercase) to a description: \"github.com\", or \"*.githubusercontent.com\" for its subdomains."},
		},
	}
}

func (r *egressAllowlistResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *egressAllowlistResource) ImportState(ctx context.Context, _ resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resp.Diagnostics.Append(resp.State.Set(ctx, &egressAllowlistModel{ID: types.StringValue(egressAllowlistID),
		Hosts: types.MapNull(types.StringType)})...)
}

func (m *egressAllowlistModel) apply(out *egressAllowlistOut) {
	hosts := map[string]string{}
	for _, h := range out.Hosts {
		hosts[h.Host] = h.Description
	}
	m.ID, m.Hosts = types.StringValue(egressAllowlistID), mapValue(hosts)
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
	if err := r.api.Do(ctx, "PUT", "/v1/admin/settings/egress", map[string]any{"hosts": entries}, &out); err != nil {
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
	if err := r.api.Do(ctx, "GET", "/v1/admin/settings/egress", nil, &out); err != nil {
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

func (r *egressAllowlistResource) Delete(ctx context.Context, _ resource.DeleteRequest, resp *resource.DeleteResponse) {
	r.put(ctx, map[string]string{}, nil, &diagSink{&resp.Diagnostics})
}
