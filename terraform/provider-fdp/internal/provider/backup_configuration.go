package provider

import (
	"context"
	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64default"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type backupConfigurationResource struct{ api *client.Client }
type backupConfigurationModel struct {
	ID                   types.String `tfsdk:"id"`
	Enabled              types.Bool   `tfsdk:"enabled"`
	Bucket               types.String `tfsdk:"bucket"`
	Prefix               types.String `tfsdk:"prefix"`
	EndpointURL          types.String `tfsdk:"endpoint_url"`
	Region               types.String `tfsdk:"region"`
	SourceID             types.String `tfsdk:"source_id"`
	IntervalHours        types.Int64  `tfsdk:"interval_hours"`
	RecoveryPublicKey    types.String `tfsdk:"recovery_public_key"`
	ServerSideEncryption types.String `tfsdk:"server_side_encryption"`
	KMSKeyID             types.String `tfsdk:"kms_key_id"`
	ObjectLockDays       types.Int64  `tfsdk:"object_lock_days"`
}
type backupConfigurationOut struct {
	Enabled              bool   `json:"enabled"`
	Bucket               string `json:"bucket"`
	Prefix               string `json:"prefix"`
	EndpointURL          string `json:"endpoint_url"`
	Region               string `json:"region"`
	SourceID             string `json:"source_id"`
	IntervalHours        int64  `json:"interval_hours"`
	RecoveryPublicKey    string `json:"recovery_public_key"`
	ServerSideEncryption string `json:"server_side_encryption"`
	KMSKeyID             string `json:"kms_key_id"`
	ObjectLockDays       int64  `json:"object_lock_days"`
}

func NewBackupConfigurationResource() resource.Resource { return &backupConfigurationResource{} }
func (r *backupConfigurationResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_backup_configuration"
}
func (r *backupConfigurationResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{Description: "Platform metadata recovery to encrypted S3 archives. Declare once. Customer data and external dependency backups remain customer-operated. Configuration requires active Global Admin. Destroy disables scheduling and retains archives and inventory. Import with ID platform_backups.", Attributes: map[string]schema.Attribute{
		"id":                     schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
		"enabled":                schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false), Description: "Enable scheduled backups."},
		"bucket":                 schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString(""), Description: "Dedicated backup bucket; omit only when source_id supplies it."},
		"prefix":                 schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("fdp-recovery"), Description: "Prefix below the integration base prefix."},
		"endpoint_url":           schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString(""), Description: "HTTPS S3-compatible endpoint. Empty uses AWS S3."},
		"region":                 schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("us-east-1"), Description: "AWS signing region."},
		"source_id":              schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString(""), Description: "Optional existing external S3 integration. Its STS role is narrowed to the backup prefix."},
		"interval_hours":         schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(24), Description: "Hours between backup attempts, 1 through 720."},
		"recovery_public_key":    schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString(""), Description: "RSA public recovery key, minimum 3072 bits. The private key must stay offline."},
		"server_side_encryption": schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString(""), Description: "Optional extra S3 encryption: AES256 or aws:kms. Client-side encryption is mandatory."},
		"kms_key_id":             schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString(""), Description: "S3 KMS key, only with aws:kms."},
		"object_lock_days":       schema.Int64Attribute{Optional: true, Computed: true, Default: int64default.StaticInt64(0), Description: "Optional COMPLIANCE retention. Bucket Object Lock must already be configured. Zero omits lock headers."},
	}}
}
func (r *backupConfigurationResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}
func (r *backupConfigurationResource) ImportState(ctx context.Context, _ resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resp.Diagnostics.Append(resp.State.Set(ctx, &backupConfigurationModel{ID: types.StringValue("platform_backups")})...)
}
func (m *backupConfigurationModel) apply(out *backupConfigurationOut) {
	m.ID = types.StringValue("platform_backups")
	m.Enabled = types.BoolValue(out.Enabled)
	m.Bucket = types.StringValue(out.Bucket)
	m.Prefix = types.StringValue(out.Prefix)
	m.EndpointURL = types.StringValue(out.EndpointURL)
	m.Region = types.StringValue(out.Region)
	m.SourceID = types.StringValue(out.SourceID)
	m.IntervalHours = types.Int64Value(out.IntervalHours)
	m.RecoveryPublicKey = types.StringValue(out.RecoveryPublicKey)
	m.ServerSideEncryption = types.StringValue(out.ServerSideEncryption)
	m.KMSKeyID = types.StringValue(out.KMSKeyID)
	m.ObjectLockDays = types.Int64Value(out.ObjectLockDays)
}
func (r *backupConfigurationResource) put(ctx context.Context, m *backupConfigurationModel, d *diagSink) {
	body := map[string]any{
		"enabled":                m.Enabled.ValueBool(),
		"bucket":                 m.Bucket.ValueString(),
		"prefix":                 m.Prefix.ValueString(),
		"endpoint_url":           m.EndpointURL.ValueString(),
		"region":                 m.Region.ValueString(),
		"source_id":              m.SourceID.ValueString(),
		"interval_hours":         m.IntervalHours.ValueInt64(),
		"recovery_public_key":    m.RecoveryPublicKey.ValueString(),
		"server_side_encryption": m.ServerSideEncryption.ValueString(),
		"kms_key_id":             m.KMSKeyID.ValueString(),
		"object_lock_days":       m.ObjectLockDays.ValueInt64(),
	}
	var out backupConfigurationOut
	if err := r.api.Do(ctx, "PUT", "/v1/backups/configuration", body, &out); err != nil {
		apiError(d.d, "saving backup configuration", err)
		return
	}
	m.apply(&out)
}
func (r *backupConfigurationResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m backupConfigurationModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var out backupConfigurationOut
	if err := r.api.Do(ctx, "GET", "/v1/backups/configuration", nil, &out); err != nil {
		apiError(&resp.Diagnostics, "reading backup configuration", err)
		return
	}
	m.apply(&out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}
func (r *backupConfigurationResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m backupConfigurationModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &m, &diagSink{&resp.Diagnostics})
	if !resp.Diagnostics.HasError() {
		resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
	}
}
func (r *backupConfigurationResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m backupConfigurationModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, &m, &diagSink{&resp.Diagnostics})
	if !resp.Diagnostics.HasError() {
		resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
	}
}
func (r *backupConfigurationResource) Delete(ctx context.Context, _ resource.DeleteRequest, resp *resource.DeleteResponse) {
	if err := r.api.Do(ctx, "DELETE", "/v1/backups/configuration", nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "disabling backup scheduling", err)
	}
}
