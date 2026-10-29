package provider

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework-timeouts/resource/timeouts"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type packageResource struct{ api *client.Client }

type packageModel struct {
	ID       types.String   `tfsdk:"id"`
	Kind     types.String   `tfsdk:"kind"`
	Name     types.String   `tfsdk:"name"`
	Version  types.String   `tfsdk:"version"`
	Origin   types.String   `tfsdk:"origin"`
	Timeouts timeouts.Value `tfsdk:"timeouts"`
}

type packageImportOut struct {
	ID     string `json:"id"`
	Status string `json:"status"`
	Log    string `json:"log"`
}

type packageVersionOut struct {
	Version string `json:"version"`
	Yanked  bool   `json:"yanked"`
}

type packageDetailOut struct {
	Name     string              `json:"name"`
	Origin   string              `json:"origin"`
	Latest   *string             `json:"latest"`
	Versions []packageVersionOut `json:"versions"`
}

func NewPackageResource() resource.Resource { return &packageResource{} }

func (r *packageResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_package"
}

func (r *packageResource) Schema(ctx context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	replace := []planmodifier.String{stringplanmodifier.RequiresReplace()}
	resp.Schema = schema.Schema{
		Description: "A public package in the platform's package repository: apply imports it from PyPI or CRAN with its missing " +
			"dependencies (Python: built packages only) and waits until the import is done. If the version disappears from the " +
			"repository, the next apply imports it again. A new version imports that one; earlier versions stay. Destroy only " +
			"stops declaring it: imported files are kept, since programs may depend on them. Import as kind/name/version.",
		Attributes: map[string]schema.Attribute{
			"id":   schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"kind": schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "python (from PyPI) or r (from CRAN)."},
			"name": schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "The package name, e.g. pandas or dplyr."},
			"version": schema.StringAttribute{Optional: true, Computed: true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
				Description:   "Exact version, e.g. 2.2.3. Unset: the newest at the first apply, then kept (set it to upgrade)."},
			"origin":   schema.StringAttribute{Computed: true, PlanModifiers: keepComputed, Description: "pypi or cran."},
			"timeouts": timeouts.Attributes(ctx, timeouts.Opts{Create: true, Update: true}),
		},
	}
}

func (r *packageResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *packageResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	parts := strings.SplitN(req.ID, "/", 3)
	if len(parts) != 3 {
		resp.Diagnostics.AddError("Invalid import ID", "use kind/name/version, e.g. python/pandas/2.2.3")
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), req.ID)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("kind"), parts[0])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("name"), parts[1])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("version"), parts[2])...)
}

func (r *packageResource) detail(ctx context.Context, kind, name string) (*packageDetailOut, error) {
	var out packageDetailOut
	if err := r.api.Do(ctx, "GET", "/v1/packages/"+kind+"/"+name, nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

func hasVersion(out *packageDetailOut, version string) bool {
	for _, v := range out.Versions {
		if v.Version == version && !v.Yanked {
			return true
		}
	}
	return false
}

// bring imports the package (or a version of it) unless it is already there, waiting for the import.
func (r *packageResource) bring(ctx context.Context, m *packageModel, timeout time.Duration) error {
	kind, name, version := m.Kind.ValueString(), m.Name.ValueString(), ""
	if !m.Version.IsNull() && !m.Version.IsUnknown() {
		version = m.Version.ValueString()
	}
	if existing, err := r.detail(ctx, kind, name); err == nil && version != "" && hasVersion(existing, version) && existing.Origin != "internal" {
		return r.settle(ctx, m, version)
	}
	requirement := name
	if version != "" {
		requirement = name + "==" + version
	}
	var job packageImportOut
	if err := r.api.Do(ctx, "POST", "/v1/packages/imports", map[string]any{"kind": kind, "requirement": requirement}, &job); err != nil {
		return err
	}
	deadline := time.Now().Add(timeout)
	for job.Status != "done" {
		if job.Status == "failed" {
			return fmt.Errorf("importing %s failed:\n%s", requirement, job.Log)
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("importing %s did not finish within %s (status %s)", requirement, timeout, job.Status)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(3 * time.Second):
		}
		if err := r.api.Do(ctx, "GET", "/v1/packages/imports/"+job.ID, nil, &job); err != nil {
			return err
		}
	}
	return r.settle(ctx, m, version)
}

// settle records what is in the repository: the requested version, or the newest when none was requested.
func (r *packageResource) settle(ctx context.Context, m *packageModel, version string) error {
	out, err := r.detail(ctx, m.Kind.ValueString(), m.Name.ValueString())
	if err != nil {
		return err
	}
	if version == "" {
		if out.Latest == nil {
			return fmt.Errorf("%s has no installable version after the import", m.Name.ValueString())
		}
		version = *out.Latest
	}
	if !hasVersion(out, version) {
		return fmt.Errorf("%s %s is not in the repository after the import", m.Name.ValueString(), version)
	}
	m.Version, m.Origin = types.StringValue(version), types.StringValue(out.Origin)
	m.ID = types.StringValue(m.Kind.ValueString() + "/" + m.Name.ValueString() + "/" + version)
	return nil
}

func (r *packageResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan packageModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	timeout, diags := plan.Timeouts.Create(ctx, 30*time.Minute)
	resp.Diagnostics.Append(diags...)
	if err := r.bring(ctx, &plan, timeout); err != nil {
		apiError(&resp.Diagnostics, "importing package", err)
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *packageResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state packageModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	out, err := r.detail(ctx, state.Kind.ValueString(), state.Name.ValueString())
	if err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading package", err)
		}
		return
	}
	if !hasVersion(out, state.Version.ValueString()) {
		resp.State.RemoveResource(ctx) // deleted or yanked: the next apply imports it again
		return
	}
	state.Origin = types.StringValue(out.Origin)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *packageResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan packageModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	timeout, diags := plan.Timeouts.Update(ctx, 30*time.Minute)
	resp.Diagnostics.Append(diags...)
	if err := r.bring(ctx, &plan, timeout); err != nil {
		apiError(&resp.Diagnostics, "importing package version", err)
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *packageResource) Delete(ctx context.Context, _ resource.DeleteRequest, resp *resource.DeleteResponse) {
	resp.Diagnostics.AddWarning("Package files kept",
		"The package is no longer declared, but its imported files stay in the repository (programs may use them). "+
			"Yank or delete versions in the app if they should go.")
}
