package provider

import (
	"context"
	"regexp"
	"sort"
	"strings"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/mapdefault"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

const packageAllowlistID = "package_allowlist"

var pythonSeparators = regexp.MustCompile(`[-_.]+`)

type packageAllowlistResource struct{ api *client.Client }

type packageAllowlistModel struct {
	ID      types.String `tfsdk:"id"`
	Enabled types.Bool   `tfsdk:"enabled"`
	Python  types.Map    `tfsdk:"python"`
	R       types.Map    `tfsdk:"r"`
}

type allowedPackage struct {
	Name        string `json:"name"`
	Description string `json:"description"`
}

type packageAllowlistOut struct {
	Enabled bool             `json:"enabled"`
	Python  []allowedPackage `json:"python"`
	R       []allowedPackage `json:"r"`
}

func NewPackageAllowlistResource() resource.Resource { return &packageAllowlistResource{} }

func (r *packageAllowlistResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_package_allowlist"
}

func (r *packageAllowlistResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	empty := mapdefault.StaticValue(mapValue(nil))
	resp.Schema = schema.Schema{
		Description: "Which public packages may be imported from PyPI and CRAN (declare once). While enabled, imports from the app, " +
			"the API and fdp_package may only request a listed package; its dependencies come along. Packages published " +
			"in the platform are not affected. Destroy turns it off. Import with ID \"package_allowlist\".",
		Attributes: map[string]schema.Attribute{
			"id":      schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"enabled": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true)},
			"python": schema.MapAttribute{ElementType: types.StringType, Optional: true, Computed: true, Default: empty,
				Description: "Package name (canonical: lowercase, \"-\" between words) to a description; \"scikit-*\" for a family."},
			"r": schema.MapAttribute{ElementType: types.StringType, Optional: true, Computed: true, Default: empty,
				Description: "CRAN package name (as written on CRAN, e.g. DBI) to a description; \"tidy*\" for a family."},
		},
	}
}

func (r *packageAllowlistResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *packageAllowlistResource) ImportState(ctx context.Context, _ resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resp.Diagnostics.Append(resp.State.Set(ctx, &packageAllowlistModel{ID: types.StringValue(packageAllowlistID),
		Enabled: types.BoolNull(), Python: types.MapNull(types.StringType), R: types.MapNull(types.StringType)})...)
}

func toMap(rows []allowedPackage) types.Map {
	values := map[string]string{}
	for _, row := range rows {
		values[row.Name] = row.Description
	}
	return mapValue(values)
}

func (m *packageAllowlistModel) apply(out *packageAllowlistOut) {
	m.ID, m.Enabled, m.Python, m.R = types.StringValue(packageAllowlistID), types.BoolValue(out.Enabled), toMap(out.Python), toMap(out.R)
}

func entries(values map[string]string) []allowedPackage {
	names := make([]string, 0, len(values))
	for name := range values {
		names = append(names, name)
	}
	sort.Strings(names)
	rows := make([]allowedPackage, 0, len(names))
	for _, name := range names {
		rows = append(rows, allowedPackage{Name: name, Description: values[name]})
	}
	return rows
}

func (r *packageAllowlistResource) put(ctx context.Context, m *packageAllowlistModel, d *diagSink) {
	python, rPackages := stringMap(ctx, m.Python, d.d), stringMap(ctx, m.R, d.d)
	for name := range python {
		base := strings.TrimSuffix(name, "*")
		if canonical := strings.ToLower(pythonSeparators.ReplaceAllString(base, "-")); canonical != base {
			d.d.AddError("Python package name not canonical", "write "+canonical+" instead of "+base+" (lowercase, \"-\" between words)")
			return
		}
	}
	body := map[string]any{"enabled": m.Enabled.ValueBool(), "python": entries(python), "r": entries(rPackages)}
	var out packageAllowlistOut
	if err := r.api.Do(ctx, "PUT", "/v1/packages/allowlist", body, &out); err != nil {
		apiError(d.d, "saving the package allowlist", err)
		return
	}
	m.apply(&out)
}

func (r *packageAllowlistResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan packageAllowlistModel
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

func (r *packageAllowlistResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state packageAllowlistModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var out packageAllowlistOut
	if err := r.api.Do(ctx, "GET", "/v1/packages/allowlist", nil, &out); err != nil {
		apiError(&resp.Diagnostics, "reading the package allowlist", err)
		return
	}
	state.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *packageAllowlistResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan packageAllowlistModel
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

func (r *packageAllowlistResource) Delete(ctx context.Context, _ resource.DeleteRequest, resp *resource.DeleteResponse) {
	if err := r.api.Do(ctx, "DELETE", "/v1/packages/allowlist", nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "turning the package allowlist off", err)
	}
}
