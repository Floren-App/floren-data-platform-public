package provider

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework-timeouts/resource/timeouts"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type layerResource struct{ api *client.Client }

type layerModel struct {
	ID             types.String   `tfsdk:"id"`
	Name           types.String   `tfsdk:"name"`
	Description    types.String   `tfsdk:"description"`
	Workspaces     types.Bool     `tfsdk:"workspaces"`
	Infrastructure JSONValue      `tfsdk:"infrastructure"`
	Generation     types.Int64    `tfsdk:"infrastructure_generation"`
	Phase          types.String   `tfsdk:"phase"`
	Production     types.Bool     `tfsdk:"production"`
	Timeouts       timeouts.Value `tfsdk:"timeouts"`
}

type layerOut struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Description string `json:"description"`
	Workspaces  bool   `json:"workspaces"`
	Production  bool   `json:"production"`
}

type operationOut struct {
	Generation int64  `json:"generation"`
	Phase      string `json:"phase"`
	Reason     string `json:"reason"`
	Message    string `json:"message"`
}

type infrastructureOut struct {
	Generation int64          `json:"generation"`
	Spec       map[string]any `json:"spec"`
	Operation  *operationOut  `json:"operation"`
}

func NewLayerResource() resource.Resource { return &layerResource{} }

func (r *layerResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_layer"
}

func (r *layerResource) Schema(ctx context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "An execution layer (e.g. Dev, Test, Prod). The platform has no built-in layers: they all come from " +
			"configuration. With `infrastructure`, the layer controller provisions its isolated namespace, Trino and artifact " +
			"storage; apply waits until it is Ready. A layer that still holds work, data or retained storage is never deleted: " +
			"destroy is refused until it is emptied and retired. Choose the default layer with fdp_default_layer.",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Computed: true, PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"name":        schema.StringAttribute{Required: true},
			"description": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
			"workspaces": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true),
				Description: "Whether people may create workspaces in this layer."},
			"infrastructure": schema.StringAttribute{Optional: true, Computed: true, CustomType: JSONType{}, PlanModifiers: jsonPlan,
				Description: "Layer infrastructure as JSON (jsonencode), see schemas/layer-infrastructure.schema.json. " +
					"Only the keys you set are compared; server defaults do not cause drift. Once enabled it cannot be removed."},
			"infrastructure_generation": schema.Int64Attribute{Computed: true},
			"phase":                     schema.StringAttribute{Computed: true, Description: "Provisioning phase of the current generation."},
			"production": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false),
				Description: "A production layer: personal and group secrets are not usable here; work uses service account secrets."},
			"timeouts": timeouts.Attributes(ctx, timeouts.Opts{Create: true, Update: true, Delete: true}),
		},
	}
}

func (r *layerResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *layerResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func (r *layerResource) find(ctx context.Context, id string) (*layerOut, error) {
	var all []layerOut
	if err := r.api.Do(ctx, "GET", "/v1/layers", nil, &all); err != nil {
		return nil, err
	}
	for i := range all {
		if all[i].ID == id {
			return &all[i], nil
		}
	}
	return nil, &client.APIError{Status: 404, Detail: "layer not found"}
}

func (r *layerResource) infrastructure(ctx context.Context, id string) (*infrastructureOut, error) {
	var out infrastructureOut
	err := r.api.Do(ctx, "GET", "/v1/admin/layers/"+id+"/infrastructure", nil, &out)
	return &out, err
}

// apply sends the declared infrastructure if it differs from the server, then waits for reconciliation.
func (r *layerResource) apply(ctx context.Context, m *layerModel, timeout time.Duration) error {
	if m.Infrastructure.IsNull() || m.Infrastructure.ValueString() == "" {
		return nil
	}
	var want map[string]any
	if err := json.Unmarshal([]byte(m.Infrastructure.ValueString()), &want); err != nil {
		return fmt.Errorf("infrastructure is not valid JSON: %w", err)
	}
	current, err := r.infrastructure(ctx, m.ID.ValueString())
	if err != nil {
		return err
	}
	if !jsonSubset(want, current.Spec) || current.Generation == 0 {
		// Merge onto the current spec so keys the user does not manage keep their values.
		merged := deepMerge(current.Spec, want)
		if err := r.api.Do(ctx, "PUT", "/v1/admin/layers/"+m.ID.ValueString()+"/infrastructure",
			map[string]any{"expected_generation": current.Generation, "spec": merged}, nil); err != nil {
			return err
		}
	}
	deadline := time.Now().Add(timeout)
	for {
		current, err = r.infrastructure(ctx, m.ID.ValueString())
		if err != nil {
			return err
		}
		if op := current.Operation; op != nil && op.Generation == current.Generation {
			switch op.Phase {
			case "Ready", "InfrastructureReady":
				return nil
			case "Retired":
				if retired, _ := want["retired"].(bool); retired {
					return nil // compute scaled to zero; storage retained until an operator removes it
				}
			case "Failed":
				return fmt.Errorf("layer provisioning failed (%s): %s. Fix the configuration and apply again, or retry with POST /v1/admin/layers/%s/infrastructure/retry", op.Reason, op.Message, m.ID.ValueString())
			}
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("layer was not Ready within %s; the controller keeps reconciling and the next apply waits again", timeout)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(5 * time.Second):
		}
	}
}

func deepMerge(base, overlay map[string]any) map[string]any {
	out := map[string]any{}
	for k, v := range base {
		out[k] = v
	}
	for k, v := range overlay {
		if child, ok := v.(map[string]any); ok {
			if existing, ok := out[k].(map[string]any); ok {
				out[k] = deepMerge(existing, child)
				continue
			}
		}
		out[k] = v
	}
	return out
}

func (r *layerResource) refresh(ctx context.Context, m *layerModel) error {
	layer, err := r.find(ctx, m.ID.ValueString())
	if err != nil {
		return err
	}
	m.Name, m.Description = types.StringValue(layer.Name), types.StringValue(layer.Description)
	m.Workspaces, m.Production = types.BoolValue(layer.Workspaces), types.BoolValue(layer.Production)
	infra, err := r.infrastructure(ctx, layer.ID)
	if err != nil {
		return err
	}
	m.Generation = types.Int64Value(infra.Generation)
	m.Phase = types.StringValue("Unmanaged")
	if infra.Operation != nil {
		m.Phase = types.StringValue(infra.Operation.Phase)
	}
	if infra.Generation > 0 {
		m.Infrastructure = reconcileJSON(m.Infrastructure, infra.Spec)
	} else if m.Infrastructure.IsUnknown() {
		m.Infrastructure = jsonNull()
	}
	return nil
}

func (r *layerResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan layerModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	timeout, diags := plan.Timeouts.Create(ctx, 30*time.Minute)
	resp.Diagnostics.Append(diags...)
	var created layerOut
	if err := r.api.Do(ctx, "POST", "/v1/admin/layers", map[string]any{"name": plan.Name.ValueString(),
		"description": plan.Description.ValueString(), "workspaces": plan.Workspaces.ValueBool(),
		"production": plan.Production.ValueBool()}, &created); err != nil {
		apiError(&resp.Diagnostics, "creating layer (import an existing layer by ID instead)", err)
		return
	}
	plan.ID = types.StringValue(created.ID)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...) // keep the ID even if provisioning fails
	if err := r.apply(ctx, &plan, timeout); err != nil {
		apiError(&resp.Diagnostics, "provisioning layer infrastructure", err)
	}
	if err := r.refresh(ctx, &plan); err != nil {
		apiError(&resp.Diagnostics, "reading layer", err)
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *layerResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state layerModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.refresh(ctx, &state); err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading layer", err)
		}
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *layerResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state layerModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	plan.ID = state.ID
	if !state.Infrastructure.IsNull() && state.Generation.ValueInt64() > 0 && plan.Infrastructure.IsNull() {
		resp.Diagnostics.AddError("Layer infrastructure cannot be removed",
			"Managed infrastructure is never disabled by an update; keep the infrastructure attribute (retire the layer explicitly instead).")
		return
	}
	timeout, diags := plan.Timeouts.Update(ctx, 30*time.Minute)
	resp.Diagnostics.Append(diags...)
	if err := r.api.Do(ctx, "PUT", "/v1/admin/layers/"+plan.ID.ValueString(), map[string]any{"name": plan.Name.ValueString(),
		"description": plan.Description.ValueString(), "workspaces": plan.Workspaces.ValueBool(),
		"production": plan.Production.ValueBool()}, nil); err != nil {
		apiError(&resp.Diagnostics, "updating layer", err)
		return
	}
	if err := r.apply(ctx, &plan, timeout); err != nil {
		apiError(&resp.Diagnostics, "provisioning layer infrastructure", err)
	}
	if err := r.refresh(ctx, &plan); err != nil {
		apiError(&resp.Diagnostics, "reading layer", err)
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *layerResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state layerModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	id := state.ID.ValueString()
	timeout, diags := state.Timeouts.Delete(ctx, 30*time.Minute)
	resp.Diagnostics.Append(diags...)
	// A layer with its own infrastructure is retired first: compute scales to zero, its data stays.
	if infra, err := r.infrastructure(ctx, id); err == nil && infra.Generation > 0 {
		enabled, _ := infra.Spec["enabled"].(bool)
		retired, _ := infra.Spec["retired"].(bool)
		if enabled && !retired {
			if err := r.retire(ctx, id, infra.Generation, timeout); err != nil {
				apiError(&resp.Diagnostics, "retiring layer before deletion", err)
				return
			}
		}
	} else if err != nil && client.IsNotFound(err) {
		return
	}
	err := r.api.Do(ctx, "DELETE", "/v1/admin/layers/"+id, nil, nil)
	if err == nil || client.IsNotFound(err) {
		return
	}
	var apiErr *client.APIError
	if errors.As(err, &apiErr) && apiErr.Status == 409 && strings.Contains(apiErr.Detail, "retained storage") {
		// Never drop data: the retired layer stays until an operator removes its storage on purpose.
		resp.Diagnostics.AddWarning("Layer retired and kept", state.Name.ValueString()+" is retired (no compute); it still exists "+
			"because its storage is retained: "+apiErr.Detail+". It is no longer managed by this configuration.")
		return
	}
	apiError(&resp.Diagnostics, "deleting layer", err)
}

func (r *layerResource) retire(ctx context.Context, id string, generation int64, timeout time.Duration) error {
	if err := r.api.Do(ctx, "POST", "/v1/admin/layers/"+id+"/infrastructure/retire",
		map[string]any{"expected_generation": generation}, nil); err != nil {
		return err
	}
	deadline := time.Now().Add(timeout)
	for {
		current, err := r.infrastructure(ctx, id)
		if err != nil {
			return err
		}
		if op := current.Operation; op != nil && op.Generation == current.Generation {
			switch op.Phase {
			case "Retired":
				return nil
			case "Failed":
				return fmt.Errorf("retiring failed (%s): %s", op.Reason, op.Message)
			}
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("layer was not retired within %s", timeout)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(5 * time.Second):
		}
	}
}
