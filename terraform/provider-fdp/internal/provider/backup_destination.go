package provider

import (
	"context"
	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64default"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type backupDestinationResource struct{ api *client.Client }
type backupDestinationModel struct {
	ID                   types.String `tfsdk:"id"`
	Name                 types.String `tfsdk:"name"`
	Bucket               types.String `tfsdk:"bucket"`
	Prefix               types.String `tfsdk:"prefix"`
	EndpointUrl          types.String `tfsdk:"endpoint_url"`
	Region               types.String `tfsdk:"region"`
	SourceID             types.String `tfsdk:"source_id"`
	RecoveryKeyRef       types.String `tfsdk:"recovery_key_ref"`
	ServerSideEncryption types.String `tfsdk:"server_side_encryption"`
	KmsKeyID             types.String `tfsdk:"kms_key_id"`
	ObjectLockDays       types.Int64  `tfsdk:"object_lock_days"`
}
type backupDestinationOut struct {
	ID                   string `json:"id"`
	Name                 string `json:"name"`
	Bucket               string `json:"bucket"`
	Prefix               string `json:"prefix"`
	EndpointUrl          string `json:"endpoint_url"`
	Region               string `json:"region"`
	SourceID             string `json:"source_id"`
	RecoveryKeyRef       string `json:"recovery_key_ref"`
	ServerSideEncryption string `json:"server_side_encryption"`
	KmsKeyID             string `json:"kms_key_id"`
	ObjectLockDays       int64  `json:"object_lock_days"`
}

func NewBackupDestinationResource() resource.Resource { return &backupDestinationResource{} }
func (r *backupDestinationResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_backup_destination"
}
func (r *backupDestinationResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}
func (r *backupDestinationResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{Description: `Reusable S3 backup destination. Recovery keys and credentials stay in the installation-selected vault; recovery_key_ref names an existing secret. Registration requires active Global Admin. Destroy retains archives and fails while any plan still refers to it.`, Attributes: map[string]schema.Attribute{
		"id":                     schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
		"name":                   schema.StringAttribute{Required: true},
		"bucket":                 schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
		"prefix":                 schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("backups")},
		"endpoint_url":           schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
		"region":                 schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("us-east-1")},
		"source_id":              schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
		"recovery_key_ref":       schema.StringAttribute{Required: true},
		"server_side_encryption": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
		"kms_key_id":             schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("")},
		"object_lock_days":       schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(0)},
	}}
}
func (r *backupDestinationResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}
func (m *backupDestinationModel) apply(ctx context.Context, out *backupDestinationOut, d *diagSink) {
	m.ID = types.StringValue(out.ID)
	m.Name = types.StringValue(out.Name)
	m.Bucket = types.StringValue(out.Bucket)
	m.Prefix = types.StringValue(out.Prefix)
	m.EndpointUrl = types.StringValue(out.EndpointUrl)
	m.Region = types.StringValue(out.Region)
	m.SourceID = types.StringValue(out.SourceID)
	m.RecoveryKeyRef = types.StringValue(out.RecoveryKeyRef)
	m.ServerSideEncryption = types.StringValue(out.ServerSideEncryption)
	m.KmsKeyID = types.StringValue(out.KmsKeyID)
	m.ObjectLockDays = types.Int64Value(out.ObjectLockDays)
}
func (r *backupDestinationResource) put(ctx context.Context, m *backupDestinationModel, create bool, d *diagSink) {
	body := map[string]any{
		"name":                   m.Name.ValueString(),
		"bucket":                 m.Bucket.ValueString(),
		"prefix":                 m.Prefix.ValueString(),
		"endpoint_url":           m.EndpointUrl.ValueString(),
		"region":                 m.Region.ValueString(),
		"source_id":              m.SourceID.ValueString(),
		"recovery_key_ref":       m.RecoveryKeyRef.ValueString(),
		"server_side_encryption": m.ServerSideEncryption.ValueString(),
		"kms_key_id":             m.KmsKeyID.ValueString(),
		"object_lock_days":       m.ObjectLockDays.ValueInt64(),
	}
	method, endpoint := "PUT", "/v1/backups/destinations/"+m.ID.ValueString()
	if create {
		method, endpoint = "POST", "/v1/backups/destinations"
	}
	var out backupDestinationOut
	if err := r.api.Do(ctx, method, endpoint, body, &out); err != nil {
		apiError(d.d, "saving backup_destination", err)
		return
	}
	m.apply(ctx, &out, d)
}
func (r *backupDestinationResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m backupDestinationModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &m, true, &diagSink{&resp.Diagnostics})
	if !resp.Diagnostics.HasError() {
		resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
	}
}
func (r *backupDestinationResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m backupDestinationModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &m, false, &diagSink{&resp.Diagnostics})
	if !resp.Diagnostics.HasError() {
		resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
	}
}
func (r *backupDestinationResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m backupDestinationModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out backupDestinationOut
	if err := r.api.Do(ctx, "GET", "/v1/backups/destinations/"+m.ID.ValueString(), nil, &out); err != nil {
		if client.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		apiError(&resp.Diagnostics, "reading backup_destination", err)
		return
	}
	m.apply(ctx, &out, &diagSink{&resp.Diagnostics})
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}
func (r *backupDestinationResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m backupDestinationModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.api.Do(ctx, "DELETE", "/v1/backups/destinations/"+m.ID.ValueString(), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "removing backup_destination", err)
	}
}
