package provider

import (
	"context"
	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
	"testing"
)

func TestComplianceScopeChangesPlanReplacementInsteadOfChangingKnownID(t *testing.T) {
	ctx := context.Background()
	response := resource.SchemaResponse{}
	(&complianceRoleResource{}).Schema(ctx, resource.SchemaRequest{}, &response)
	nonNull := tftypes.NewValue(tftypes.Object{AttributeTypes: map[string]tftypes.Type{}}, map[string]tftypes.Value{})
	before := types.SetValueMust(types.StringType, []attr.Value{types.StringValue("old-scope")})
	after := types.SetValueMust(types.StringType, []attr.Value{types.StringValue("new-scope")})
	for _, name := range []string{"resource_ids", "layers", "classifications", "group_ids"} {
		t.Run(name, func(t *testing.T) {
			attribute := response.Schema.Attributes[name].(schema.SetAttribute)
			for _, changed := range []bool{false, true} {
				planned := before
				if changed {
					planned = after
				}
				request := planmodifier.SetRequest{State: tfsdk.State{Raw: nonNull}, Plan: tfsdk.Plan{Raw: nonNull}, StateValue: before, PlanValue: planned}
				result := planmodifier.SetResponse{PlanValue: planned}
				for _, modifier := range attribute.PlanModifiers {
					modifier.PlanModifySet(ctx, request, &result)
				}
				if result.Diagnostics.HasError() || result.RequiresReplace != changed {
					t.Fatalf("changed=%v must plan replacement=%v: %+v", changed, changed, result)
				}
			}
		})
	}
}
