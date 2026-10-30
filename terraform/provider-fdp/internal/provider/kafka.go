package provider

import (
	"context"
	"fmt"
	"time"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64default"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type kafkaResource struct {
	api     *client.Client
	cluster bool
}
type kafkaClusterModel struct {
	ID      types.String `tfsdk:"id"`
	LayerID types.String `tfsdk:"layer_id"`
}
type kafkaTopicModel struct {
	ID         types.String `tfsdk:"id"`
	LayerID    types.String `tfsdk:"layer_id"`
	ClusterID  types.String `tfsdk:"cluster_id"`
	Name       types.String `tfsdk:"name"`
	Partitions types.Int64  `tfsdk:"partitions"`
	Replicas   types.Int64  `tfsdk:"replication_factor"`
	Retention  types.Int64  `tfsdk:"retention_ms"`
	Cleanup    types.String `tfsdk:"cleanup_policy"`
	Phase      types.String `tfsdk:"phase"`
}

func NewKafkaClusterResource() resource.Resource { return &kafkaResource{cluster: true} }
func NewKafkaTopicResource() resource.Resource   { return &kafkaResource{} }
func (r *kafkaResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	suffix := "_kafka_topic"
	if r.cluster {
		suffix = "_kafka_cluster"
	}
	resp.TypeName = req.ProviderTypeName + suffix
}
func (r *kafkaResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	if r.cluster {
		resp.Schema = schema.Schema{Description: "Register a layer's managed Kafka for topic IAM. Enable infrastructure.kafka on fdp_layer first. Destroy unregisters metadata only; brokers and storage are retained. Import by ID.", Attributes: map[string]schema.Attribute{
			"id":       schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"layer_id": schema.StringAttribute{Required: true, PlanModifiers: replaceString},
		}}
		return
	}
	resp.Schema = schema.Schema{Description: "A governed managed Kafka topic. Create/update waits up to 15 minutes for Strimzi readiness; destroy deletes its messages. Import by ID. Grants use fdp_role_assignment.", Attributes: map[string]schema.Attribute{
		"id":                 schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
		"layer_id":           schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
		"cluster_id":         schema.StringAttribute{Required: true, PlanModifiers: replaceString},
		"name":               schema.StringAttribute{Required: true, PlanModifiers: replaceString},
		"partitions":         schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(3)},
		"replication_factor": schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(1), Description: "At least 3 for multi-broker clusters. Replica reassignment is a separate controlled operation."},
		"retention_ms":       schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(604800000)},
		"cleanup_policy":     schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("delete")},
		"phase":              schema.StringAttribute{Computed: true},
	}}
}
func (r *kafkaResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}
func (r *kafkaResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}
func (m *kafkaTopicModel) body() map[string]any {
	return map[string]any{"partitions": m.Partitions.ValueInt64(), "replication_factor": m.Replicas.ValueInt64(), "retention_ms": m.Retention.ValueInt64(), "cleanup_policy": m.Cleanup.ValueString()}
}
func (m *kafkaTopicModel) apply(out resourceOut) {
	m.ID, m.Name, m.ClusterID = types.StringValue(out.ID), types.StringValue(out.Name), types.StringValue(out.ParentID)
	m.LayerID = optional(out.LayerID)
	number := func(key string) types.Int64 { n, _ := out.Config[key].(float64); return types.Int64Value(int64(n)) }
	m.Partitions, m.Replicas, m.Retention = number("partitions"), number("replication_factor"), number("retention_ms")
	m.Cleanup = types.StringValue(fmt.Sprint(out.Config["cleanup_policy"]))
	m.Phase = types.StringValue(fmt.Sprint(out.Config["phase"]))
}
func (r *kafkaResource) wait(ctx context.Context, id string) (resourceOut, error) {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Minute)
	defer cancel()
	for {
		var out resourceOut
		if err := r.api.Do(ctx, "GET", "/v1/kafka/topics/"+id, nil, &out); err != nil {
			return out, err
		}
		if out.Config["phase"] == "Ready" {
			return out, nil
		}
		if out.Config["phase"] == "Failed" {
			return out, fmt.Errorf("KafkaTopic failed; inspect its Strimzi status")
		}
		select {
		case <-ctx.Done():
			return out, fmt.Errorf("topic not ready: %w", ctx.Err())
		case <-time.After(2 * time.Second):
		}
	}
}
func (r *kafkaResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	if r.cluster {
		var plan kafkaClusterModel
		resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
		if resp.Diagnostics.HasError() {
			return
		}
		var out resourceOut
		if err := r.api.Do(ctx, "POST", "/v1/kafka/clusters/"+plan.LayerID.ValueString(), nil, &out); err != nil {
			apiError(&resp.Diagnostics, "registering Kafka", err)
			return
		}
		plan.ID = types.StringValue(out.ID)
		resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
		return
	}
	var plan kafkaTopicModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var cluster, out resourceOut
	if err := r.api.Do(ctx, "GET", "/v1/kafka/clusters/"+plan.ClusterID.ValueString(), nil, &cluster); err != nil {
		apiError(&resp.Diagnostics, "reading Kafka cluster", err)
		return
	}
	body := plan.body()
	body["name"] = plan.Name.ValueString()
	body["layer_id"] = cluster.LayerID
	if err := r.api.Do(ctx, "POST", "/v1/kafka/topics", body, &out); err != nil {
		apiError(&resp.Diagnostics, "creating topic", err)
		return
	}
	// Retain the ID even if readiness times out, so an apply can recover without duplicate creation.
	plan.apply(out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
	out, err := r.wait(ctx, out.ID)
	if err != nil {
		apiError(&resp.Diagnostics, "waiting for KafkaTopic readiness", err)
		return
	}
	plan.apply(out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}
func (r *kafkaResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	if r.cluster {
		var state kafkaClusterModel
		resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
		if resp.Diagnostics.HasError() {
			return
		}
		var out resourceOut
		if err := r.api.Do(ctx, "GET", "/v1/kafka/clusters/"+state.ID.ValueString(), nil, &out); err != nil {
			if !gone(ctx, err, resp) {
				apiError(&resp.Diagnostics, "reading Kafka", err)
			}
			return
		}
		state.LayerID = optional(out.LayerID)
		resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
		return
	}
	var state kafkaTopicModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out resourceOut
	if err := r.api.Do(ctx, "GET", "/v1/kafka/topics/"+state.ID.ValueString(), nil, &out); err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading Kafka topic", err)
		}
		return
	}
	state.apply(out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}
func (r *kafkaResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	if r.cluster {
		resp.Diagnostics.AddError("Immutable Kafka layer", "Replace the cluster registration to change its layer.")
		return
	}
	var plan, state kafkaTopicModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out resourceOut
	if err := r.api.Do(ctx, "PUT", "/v1/kafka/topics/"+state.ID.ValueString(), plan.body(), &out); err != nil {
		apiError(&resp.Diagnostics, "updating Kafka topic", err)
		return
	}
	plan.apply(out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
	out, err := r.wait(ctx, out.ID)
	if err != nil {
		apiError(&resp.Diagnostics, "waiting for KafkaTopic readiness", err)
		return
	}
	plan.apply(out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}
func (r *kafkaResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var id string
	root := "/v1/kafka/topics/"
	if r.cluster {
		var state kafkaClusterModel
		resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
		id = state.ID.ValueString()
		root = "/v1/kafka/clusters/"
	} else {
		var state kafkaTopicModel
		resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
		id = state.ID.ValueString()
	}
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.api.Do(ctx, "DELETE", root+id, nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "deleting Kafka resource", err)
		return
	}
	if !r.cluster {
		if err := r.waitDeleted(ctx, id); err != nil {
			apiError(&resp.Diagnostics, "waiting for Kafka topic deletion", err)
		}
	}
}

func (r *kafkaResource) waitDeleted(ctx context.Context, id string) error {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Minute)
	defer cancel()
	for {
		var out struct {
			Phase string `json:"phase"`
		}
		if err := r.api.Do(ctx, "GET", "/v1/kafka/topics/"+id+"/deletion", nil, &out); err != nil {
			if client.IsNotFound(err) {
				return nil
			}
			return err
		}
		if out.Phase == "Deleted" {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
}
