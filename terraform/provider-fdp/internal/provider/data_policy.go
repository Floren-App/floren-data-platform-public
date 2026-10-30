package provider

import (
	"context"
	"strings"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/setdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type dataPolicyResource struct{ api *client.Client }

type dataPolicyModel struct {
	ID               types.String `tfsdk:"id"`
	SchemaID         types.String `tfsdk:"schema_id"`
	Table            types.String `tfsdk:"table"`
	Kind             types.String `tfsdk:"kind"`
	Column           types.String `tfsdk:"column"`
	Method           types.String `tfsdk:"method"`
	Expression       types.String `tfsdk:"expression"`
	Normalized       types.String `tfsdk:"normalized_expression"`
	ExemptGroups     types.Set    `tfsdk:"exempt_groups"`
	ExemptPrincipals types.Set    `tfsdk:"exempt_principals"`
	Description      types.String `tfsdk:"description"`
}

type dataPolicyOut struct {
	ID               string   `json:"id"`
	SchemaID         string   `json:"schema_id"`
	Table            string   `json:"table"`
	Kind             string   `json:"kind"`
	Column           string   `json:"column"`
	Method           string   `json:"method"`
	Expression       string   `json:"expression"`
	ExemptGroups     []string `json:"exempt_groups"`
	ExemptPrincipals []string `json:"exempt_principals"`
	Description      string   `json:"description"`
}

func NewDataPolicyResource() resource.Resource { return &dataPolicyResource{} }

func (r *dataPolicyResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_data_policy"
}

func (r *dataPolicyResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	replace := []planmodifier.String{stringplanmodifier.RequiresReplace()}
	resp.Schema = schema.Schema{
		Description: "A column mask or row filter on one table of a governed SQL schema, enforced by the layer's Trino for " +
			"everyone who queries the table except the exempt groups and identities. Column masks: method null, redact, hash, " +
			"partial (last four characters) or expression. Row filters: a condition in expression. Expressions are Trino SQL over " +
			"the table's own columns and may use is_member('<app group name>'). Requires Owner on the schema. Import by ID.",
		Attributes: map[string]schema.Attribute{
			"id":        schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"schema_id": schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "The schema (fdp_schema id)."},
			"table":     schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "Table name, lowercase."},
			"kind":      schema.StringAttribute{Required: true, PlanModifiers: replace, Description: "column_mask or row_filter."},
			"column": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString(""),
				PlanModifiers: replace, Description: "Column masks: the masked column, lowercase."},
			"method": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString(""),
				Description: "Column masks: null, redact, hash, partial or expression."},
			"expression": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString(""),
				Description: "The mask expression (method expression) or the row filter condition."},
			"normalized_expression": schema.StringAttribute{Computed: true,
				Description: "The expression as the platform stores and enforces it (parsed and formatted, comments removed)."},
			"exempt_groups": schema.SetAttribute{ElementType: types.StringType, Optional: true, Computed: true,
				Default: setdefault.StaticValue(setValue(nil)), Description: "App group IDs that see real values / all rows."},
			"exempt_principals": schema.SetAttribute{ElementType: types.StringType, Optional: true, Computed: true,
				Default: setdefault.StaticValue(setValue(nil)), Description: "Identity IDs (people or managed identities) exempted."},
			"description": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
		},
	}
}

func (r *dataPolicyResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *dataPolicyResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

// apply copies the platform's view into the model. Names are lowercased by the platform; a configured
// name that differs only in case is kept. The platform stores expressions normalized: after a create or
// update the configured expression stays as written; a read replaces it only when the platform's
// normalized expression changed outside configuration (or on import), so formatting never shows as a change.
func (m *dataPolicyModel) apply(out *dataPolicyOut, configured bool) {
	keep := func(current types.String, actual string) types.String {
		if !current.IsNull() && !current.IsUnknown() && strings.EqualFold(current.ValueString(), actual) {
			return current
		}
		return types.StringValue(actual)
	}
	m.ID, m.SchemaID, m.Kind = types.StringValue(out.ID), types.StringValue(out.SchemaID), types.StringValue(out.Kind)
	m.Table, m.Column = keep(m.Table, out.Table), keep(m.Column, out.Column)
	m.Method, m.Description = types.StringValue(out.Method), types.StringValue(out.Description)
	if !configured && (m.Expression.IsNull() || m.Normalized.IsNull() || m.Normalized.ValueString() != out.Expression) {
		m.Expression = types.StringValue(out.Expression)
	}
	m.Normalized = types.StringValue(out.Expression)
	m.ExemptGroups, m.ExemptPrincipals = setValue(out.ExemptGroups), setValue(out.ExemptPrincipals)
}

// ModifyPlan ignores a configured expression that differs from state only in formatting (for example after
// import, when state holds the platform's normalized form): the platform says how it would store it.
func (r *dataPolicyResource) ModifyPlan(ctx context.Context, req resource.ModifyPlanRequest, resp *resource.ModifyPlanResponse) {
	if req.State.Raw.IsNull() || req.Plan.Raw.IsNull() || r.api == nil {
		return
	}
	var plan, state dataPolicyModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() || plan.Expression.IsUnknown() || plan.Expression.ValueString() == "" ||
		plan.Expression.Equal(state.Expression) {
		return
	}
	var out struct {
		Expression string `json:"expression"`
	}
	if err := r.api.Do(ctx, "POST", "/v1/data-policies/normalize", map[string]any{"expression": plan.Expression.ValueString()}, &out); err != nil {
		return // an invalid expression is reported by the apply itself
	}
	if out.Expression == state.Normalized.ValueString() {
		resp.Diagnostics.Append(resp.Plan.SetAttribute(ctx, path.Root("expression"), state.Expression)...)
		resp.Diagnostics.Append(resp.Plan.SetAttribute(ctx, path.Root("normalized_expression"), state.Normalized)...)
	}
}

func (r *dataPolicyResource) change(ctx context.Context, m *dataPolicyModel, d *diagSink) map[string]any {
	return map[string]any{"method": m.Method.ValueString(), "expression": m.Expression.ValueString(),
		"exempt_groups": stringSet(ctx, m.ExemptGroups, d.d), "exempt_principals": stringSet(ctx, m.ExemptPrincipals, d.d),
		"description": m.Description.ValueString()}
}

func (r *dataPolicyResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan dataPolicyModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body := r.change(ctx, &plan, &diagSink{&resp.Diagnostics})
	body["schema_id"], body["table"], body["kind"], body["column"] =
		plan.SchemaID.ValueString(), plan.Table.ValueString(), plan.Kind.ValueString(), plan.Column.ValueString()
	var out dataPolicyOut
	if err := r.api.Do(ctx, "POST", "/v1/data-policies", body, &out); err != nil {
		apiError(&resp.Diagnostics, "creating data policy", err)
		return
	}
	plan.apply(&out, true)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *dataPolicyResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state dataPolicyModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	var out dataPolicyOut
	if err := r.api.Do(ctx, "GET", "/v1/data-policies/"+state.ID.ValueString(), nil, &out); err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading data policy", err)
		}
		return
	}
	state.apply(&out, false)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *dataPolicyResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state dataPolicyModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out dataPolicyOut
	if err := r.api.Do(ctx, "PUT", "/v1/data-policies/"+state.ID.ValueString(),
		r.change(ctx, &plan, &diagSink{&resp.Diagnostics}), &out); err != nil {
		apiError(&resp.Diagnostics, "updating data policy", err)
		return
	}
	plan.apply(&out, true)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *dataPolicyResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state dataPolicyModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.api.Do(ctx, "DELETE", "/v1/data-policies/"+state.ID.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "deleting data policy", err)
	}
}
