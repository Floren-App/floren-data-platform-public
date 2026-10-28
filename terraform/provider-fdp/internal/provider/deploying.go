package provider

import (
	"context"
	"fmt"
	"time"

	"github.com/floren/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework-timeouts/resource/timeouts"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/mapdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// ---- repositories -------------------------------------------------------------------------------

type repositoryResource struct{ api *client.Client }

type repositoryModel struct {
	ID                 types.String `tfsdk:"id"`
	Name               types.String `tfsdk:"name"`
	URL                types.String `tfsdk:"url"`
	DefaultBranch      types.String `tfsdk:"default_branch"`
	Description        types.String `tfsdk:"description"`
	AllowedGroups      types.List   `tfsdk:"allowed_groups"`
	CredentialsWO      types.Map    `tfsdk:"credentials_wo"`
	CredentialsVersion types.Int64  `tfsdk:"credentials_wo_version"`
	HasCredentials     types.Bool   `tfsdk:"has_credentials"`
}

type repoOut struct {
	ID             string   `json:"id"`
	Name           string   `json:"name"`
	URL            string   `json:"url"`
	DefaultBranch  string   `json:"default_branch"`
	Description    string   `json:"description"`
	AllowedGroups  []string `json:"allowed_groups"`
	HasCredentials bool     `json:"has_credentials"`
}

func NewRepositoryResource() resource.Resource { return &repositoryResource{} }

func (r *repositoryResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_repository"
}

func (r *repositoryResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "A Git repository programs are deployed from. Only the control plane talks to Git. Import by name.",
		Attributes: map[string]schema.Attribute{
			"id":             schema.StringAttribute{Computed: true, PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"name":           schema.StringAttribute{Required: true, PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"url":            schema.StringAttribute{Required: true, Description: "https:// clone URL (http:// for an internal Git server)."},
			"default_branch": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("main")},
			"description":    schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"allowed_groups": schema.ListAttribute{ElementType: types.StringType, Optional: true, Computed: true,
				Default: listdefault.StaticValue(listValue(nil)), Description: "Who may deploy from it; empty = everyone."},
			"credentials_wo": schema.MapAttribute{ElementType: types.StringType, Optional: true, Sensitive: true, WriteOnly: true,
				Description: "{username, token} with read access. Write-only: never stored in state; sent on create and when credentials_wo_version changes."},
			"credentials_wo_version": schema.Int64Attribute{Optional: true},
			"has_credentials":        schema.BoolAttribute{Computed: true},
		},
	}
}

func (r *repositoryResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *repositoryResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("name"), req, resp)
}

func (m *repositoryModel) apply(out *repoOut) {
	m.ID, m.Name, m.URL = types.StringValue(out.ID), types.StringValue(out.Name), types.StringValue(out.URL)
	m.DefaultBranch, m.Description = types.StringValue(out.DefaultBranch), types.StringValue(out.Description)
	m.AllowedGroups, m.HasCredentials = listValue(out.AllowedGroups), types.BoolValue(out.HasCredentials)
	m.CredentialsWO = types.MapNull(types.StringType)
}

func (r *repositoryResource) put(ctx context.Context, plan, config *repositoryModel, send bool, d *diagSink) {
	body := map[string]any{"name": plan.Name.ValueString(), "url": plan.URL.ValueString(),
		"default_branch": plan.DefaultBranch.ValueString(), "description": plan.Description.ValueString(),
		"allowed_groups": stringList(ctx, plan.AllowedGroups, d.d)}
	if send {
		if s := stringMap(ctx, config.CredentialsWO, d.d); len(s) > 0 {
			body["secret"] = s
		}
	}
	var out repoOut
	if err := r.api.Do(ctx, "PUT", "/v1/admin/repos/"+plan.Name.ValueString(), body, &out); err != nil {
		apiError(d.d, "saving repository", err)
		return
	}
	plan.apply(&out)
}

func findRepo(ctx context.Context, api *client.Client, name string) (*repoOut, error) {
	var all []repoOut
	if err := api.Do(ctx, "GET", "/v1/admin/repos", nil, &all); err != nil {
		return nil, err
	}
	for i := range all {
		if all[i].Name == name {
			return &all[i], nil
		}
	}
	return nil, &client.APIError{Status: 404, Detail: "repository not found"}
}

func (r *repositoryResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan, config repositoryModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if _, err := findRepo(ctx, r.api, plan.Name.ValueString()); err == nil {
		resp.Diagnostics.AddError("Repository already exists", "Import it instead: tofu import <address> "+plan.Name.ValueString())
		return
	}
	r.put(ctx, &plan, &config, true, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *repositoryResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state repositoryModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	out, err := findRepo(ctx, r.api, state.Name.ValueString())
	if err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading repository", err)
		}
		return
	}
	state.apply(out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *repositoryResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state, config repositoryModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	r.put(ctx, &plan, &config, !plan.CredentialsVersion.Equal(state.CredentialsVersion), &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *repositoryResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state repositoryModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.api.Do(ctx, "DELETE", "/v1/admin/repos/"+state.Name.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "deleting repository", err)
	}
}

// ---- workloads -------------------------------------------------------------------------------

type workloadResource struct{ api *client.Client }

type workloadModel struct {
	ID                 types.String   `tfsdk:"id"`
	RepositoryID       types.String   `tfsdk:"repository_id"`
	Branch             types.String   `tfsdk:"branch"`
	Program            types.String   `tfsdk:"program"`
	Slug               types.String   `tfsdk:"slug"`
	LayerID            types.String   `tfsdk:"layer_id"`
	FolderID           types.String   `tfsdk:"folder_id"`
	Title              types.String   `tfsdk:"title"`
	Warehouse          types.String   `tfsdk:"warehouse"`
	ExecutionMode      types.String   `tfsdk:"execution_mode"`
	RunAs              types.String   `tfsdk:"run_as"`
	AutoUpdate         types.Bool     `tfsdk:"auto_update"`
	Env                types.Map      `tfsdk:"env"`
	Secrets            types.Map      `tfsdk:"secrets"`
	Kind               types.String   `tfsdk:"kind"`
	Status             types.String   `tfsdk:"status"`
	DeployedSHA        types.String   `tfsdk:"deployed_sha"`
	URL                types.String   `tfsdk:"url"`
	ResourceID         types.String   `tfsdk:"resource_id"`
	ServicePrincipalID types.String   `tfsdk:"service_principal_id"`
	Timeouts           timeouts.Value `tfsdk:"timeouts"`
}

type workloadOut struct {
	ID                 string            `json:"id"`
	Repo               string            `json:"repo"`
	Slug               string            `json:"slug"`
	Title              string            `json:"title"`
	Kind               string            `json:"kind"`
	Program            string            `json:"program"`
	Branch             string            `json:"branch"`
	Warehouse          string            `json:"warehouse"`
	ExecutionMode      string            `json:"execution_mode"`
	RunAs              *string           `json:"run_as"`
	AutoUpdate         bool              `json:"auto_update"`
	Env                map[string]string `json:"env"`
	Secrets            map[string]string `json:"secrets"`
	Status             string            `json:"status"`
	DeployedSHA        *string           `json:"deployed_sha"`
	URL                *string           `json:"url"`
	ResourceID         *string           `json:"resource_id"`
	ServicePrincipalID *string           `json:"service_principal_id"`
	FolderID           *string           `json:"folder_id"`
	LayerID            *string           `json:"layer_id"`
	SyncError          *string           `json:"sync_error"`
	AccessIssue        map[string]any    `json:"access_issue"`
	Events             []struct {
		Kind    string `json:"kind"`
		Message string `json:"message"`
	} `json:"events"`
}

func NewWorkloadResource() resource.Resource { return &workloadResource{} }

func (r *workloadResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_workload"
}

func (r *workloadResource) Schema(ctx context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	replace := []planmodifier.String{stringplanmodifier.RequiresReplace()}
	computed := []planmodifier.String{stringplanmodifier.UseStateForUnknown()}
	resp.Schema = schema.Schema{
		Description: "A program deployed from a repository branch (job, app, dbt or Airflow), kept in sync with the " +
			"branch. Apply waits until it is live. Promotion, branch protection and execution-identity rules still apply. Import by ID.",
		Attributes: map[string]schema.Attribute{
			"id":            schema.StringAttribute{Computed: true, PlanModifiers: computed},
			"repository_id": schema.StringAttribute{Required: true, PlanModifiers: replace},
			"branch":        schema.StringAttribute{Required: true, PlanModifiers: replace},
			"program":       schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "Program name in fdp.json."},
			"slug":          schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "Address name: /p/<slug>/."},
			"layer_id": schema.StringAttribute{Optional: true, Computed: true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplaceIfConfigured(), stringplanmodifier.UseStateForUnknown()}},
			"folder_id": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("workloads"),
				PlanModifiers: replace, Description: "Folder whose permissions it inherits."},
			"title":          schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: computed},
			"warehouse":      schema.StringAttribute{Optional: true, Computed: true, PlanModifiers: computed},
			"execution_mode": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("caller")},
			"run_as":         schema.StringAttribute{Optional: true, Description: "Service account to run as; empty runs as the deployer."},
			"auto_update":    schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true)},
			"env": schema.MapAttribute{ElementType: types.StringType, Optional: true, Computed: true,
				Default: mapdefault.StaticValue(mapValue(nil))},
			"secrets": schema.MapAttribute{ElementType: types.StringType, Optional: true, Computed: true,
				Default: mapdefault.StaticValue(mapValue(nil)), Description: "Environment variable -> secret reference (never values)."},
			"kind":                 schema.StringAttribute{Computed: true, PlanModifiers: computed},
			"status":               schema.StringAttribute{Computed: true},
			"deployed_sha":         schema.StringAttribute{Computed: true},
			"url":                  schema.StringAttribute{Computed: true, PlanModifiers: computed},
			"resource_id":          schema.StringAttribute{Computed: true, PlanModifiers: computed},
			"service_principal_id": schema.StringAttribute{Computed: true, PlanModifiers: computed},
			"timeouts":             timeouts.Attributes(ctx, timeouts.Opts{Create: true, Update: true}),
		},
	}
}

func (r *workloadResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *workloadResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func optional(s *string) types.String {
	if s == nil {
		return types.StringNull()
	}
	return types.StringValue(*s)
}

func (m *workloadModel) apply(out *workloadOut) {
	m.ID, m.Slug, m.Program, m.Branch = types.StringValue(out.ID), types.StringValue(out.Slug), types.StringValue(out.Program), types.StringValue(out.Branch)
	m.Title, m.Warehouse, m.ExecutionMode = types.StringValue(out.Title), types.StringValue(out.Warehouse), types.StringValue(out.ExecutionMode)
	m.AutoUpdate, m.Kind, m.Status = types.BoolValue(out.AutoUpdate), types.StringValue(out.Kind), types.StringValue(out.Status)
	m.Env, m.Secrets = mapValue(out.Env), mapValue(out.Secrets)
	if out.RunAs != nil && *out.RunAs != "" {
		m.RunAs = types.StringValue(*out.RunAs)
	} else if !m.RunAs.IsNull() && m.RunAs.ValueString() == "" {
		m.RunAs = types.StringValue("")
	} else {
		m.RunAs = types.StringNull()
	}
	m.DeployedSHA, m.URL, m.ResourceID = optional(out.DeployedSHA), optional(out.URL), optional(out.ResourceID)
	m.ServicePrincipalID, m.LayerID = optional(out.ServicePrincipalID), optional(out.LayerID)
	if out.FolderID != nil && *out.FolderID != "" {
		m.FolderID = types.StringValue(*out.FolderID)
	}
}

func (r *workloadResource) wait(ctx context.Context, id string, timeout time.Duration) (*workloadOut, error) {
	deadline := time.Now().Add(timeout)
	for {
		var out workloadOut
		if err := r.api.Do(ctx, "GET", "/v1/workloads/"+id, nil, &out); err != nil {
			return nil, err
		}
		switch out.Status {
		case "live":
			return &out, nil
		case "failed":
			reason := "deployment failed"
			if out.SyncError != nil && *out.SyncError != "" {
				reason = *out.SyncError
			} else if msg, ok := out.AccessIssue["message"].(string); ok {
				reason = msg
			} else {
				for i := len(out.Events) - 1; i >= 0; i-- {
					if out.Events[i].Message != "" {
						reason = out.Events[i].Message
						break
					}
				}
			}
			return &out, fmt.Errorf("%s did not go live: %s", out.Slug, reason)
		}
		if time.Now().After(deadline) {
			return &out, fmt.Errorf("%s was not live within %s (status %s)", out.Slug, timeout, out.Status)
		}
		select {
		case <-ctx.Done():
			return &out, ctx.Err()
		case <-time.After(3 * time.Second):
		}
	}
}

func (r *workloadResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan workloadModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	timeout, diags := plan.Timeouts.Create(ctx, 20*time.Minute)
	resp.Diagnostics.Append(diags...)
	body := map[string]any{"repo_id": plan.RepositoryID.ValueString(), "branch": plan.Branch.ValueString(),
		"program": plan.Program.ValueString(), "slug": plan.Slug.ValueString(), "execution_mode": plan.ExecutionMode.ValueString(),
		"folder_id":   plan.FolderID.ValueString(),
		"auto_update": plan.AutoUpdate.ValueBool(), "env": stringMap(ctx, plan.Env, &resp.Diagnostics),
		"secrets": stringMap(ctx, plan.Secrets, &resp.Diagnostics)}
	for key, value := range map[string]types.String{"layer_id": plan.LayerID, "title": plan.Title, "warehouse": plan.Warehouse, "run_as": plan.RunAs} {
		if !value.IsNull() && !value.IsUnknown() && value.ValueString() != "" {
			body[key] = value.ValueString()
		}
	}
	var out workloadOut
	if err := r.api.Do(ctx, "POST", "/v1/workloads", body, &out); err != nil {
		apiError(&resp.Diagnostics, "deploying", err)
		return
	}
	plan.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...) // tracked even if it fails to go live
	final, err := r.wait(ctx, out.ID, timeout)
	if final != nil {
		plan.apply(final)
	}
	if err != nil {
		apiError(&resp.Diagnostics, "deploying workload", err)
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *workloadResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state workloadModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var out workloadOut
	if err := r.api.Do(ctx, "GET", "/v1/workloads/"+state.ID.ValueString(), nil, &out); err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading workload", err)
		}
		return
	}
	if out.Status == "stopped" {
		resp.State.RemoveResource(ctx) // undeployed outside configuration: plan deploys it again
		return
	}
	state.apply(&out)
	if state.RepositoryID.IsNull() || state.RepositoryID.ValueString() == "" {
		// After import: the API names the repository; resolve it to the ID configuration refers to.
		if repo, err := findRepo(ctx, r.api, out.Repo); err == nil {
			state.RepositoryID = types.StringValue(repo.ID)
		}
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *workloadResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state workloadModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	timeout, diags := plan.Timeouts.Update(ctx, 20*time.Minute)
	resp.Diagnostics.Append(diags...)
	body := map[string]any{"execution_mode": plan.ExecutionMode.ValueString(), "auto_update": plan.AutoUpdate.ValueBool(),
		"env": stringMap(ctx, plan.Env, &resp.Diagnostics), "secrets": stringMap(ctx, plan.Secrets, &resp.Diagnostics),
		"run_as": plan.RunAs.ValueString()}
	if !plan.Title.IsUnknown() && !plan.Title.IsNull() {
		body["title"] = plan.Title.ValueString()
	}
	if !plan.Warehouse.IsUnknown() && !plan.Warehouse.IsNull() {
		body["warehouse"] = plan.Warehouse.ValueString()
	}
	var out workloadOut
	if err := r.api.Do(ctx, "PATCH", "/v1/workloads/"+state.ID.ValueString(), body, &out); err != nil {
		apiError(&resp.Diagnostics, "updating workload", err)
		return
	}
	plan.ID = state.ID
	final, err := r.wait(ctx, state.ID.ValueString(), timeout)
	if final != nil {
		plan.apply(final)
	}
	if err != nil {
		apiError(&resp.Diagnostics, "redeploying workload", err)
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *workloadResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state workloadModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.api.Do(ctx, "DELETE", "/v1/workloads/"+state.ID.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "undeploying", err)
	}
}
