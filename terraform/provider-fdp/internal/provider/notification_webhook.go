package provider

import (
	"context"
	"net/url"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/boolplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/setdefault"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// A notification webhook: where the platform posts its events (reviews waiting, access ending, ...).
type notificationWebhookResource struct{ api *client.Client }

type notificationWebhookModel struct {
	ID              types.String `tfsdk:"id"`
	Name            types.String `tfsdk:"name"`
	URL             types.String `tfsdk:"url"`
	Events          types.Set    `tfsdk:"events"`
	Enabled         types.Bool   `tfsdk:"enabled"`
	SecretWO        types.String `tfsdk:"secret_wo"`
	SecretWOVersion types.Int64  `tfsdk:"secret_wo_version"`
	HasSecret       types.Bool   `tfsdk:"has_secret"`
}

type notificationWebhookOut struct {
	ID        string   `json:"id"`
	Name      string   `json:"name"`
	URL       string   `json:"url"`
	Events    []string `json:"events"`
	Enabled   bool     `json:"enabled"`
	HasSecret bool     `json:"has_secret"`
}

func NewNotificationWebhookResource() resource.Resource { return &notificationWebhookResource{} }

func (r *notificationWebhookResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_notification_webhook"
}

func (r *notificationWebhookResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "A webhook the platform posts notifications to as JSON: a chat channel, or automation that sends email. " +
			"Each message has event, subject, text, link (into the app), recipients (username and email of who should act) and, " +
			"for compliance cases, a case summary; form answers are never included. Headers: X-FDP-Event, X-FDP-Delivery and, " +
			"with a secret, X-FDP-Signature (sha256=<HMAC-SHA256 of the body>). Events: review.waiting, review.due, " +
			"review.overdue, case.approved, case.declined, case.changes_requested, case.comment, membership.waiting, " +
			"access.renewal_ready, access.ending, access.ended, access.deadline, access.removed. Import by name.",
		Attributes: map[string]schema.Attribute{
			"id":   schema.StringAttribute{Computed: true, PlanModifiers: keepComputed},
			"name": schema.StringAttribute{Required: true},
			"url":  schema.StringAttribute{Required: true, Description: "https:// (or http:// inside your network)."},
			"events": schema.SetAttribute{ElementType: types.StringType, Optional: true, Computed: true,
				Default: setdefault.StaticValue(setValue(nil)), Description: "Events to send; empty sends all."},
			"enabled": schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true)},
			"secret_wo": schema.StringAttribute{Optional: true, Sensitive: true, WriteOnly: true,
				Description: "Signing secret (at least 16 characters), kept in the platform's secret store. Write-only: never " +
					"stored in state or returned by the API. Sent on create and whenever secret_wo_version changes; leave it " +
					"out and change secret_wo_version to remove the secret."},
			"secret_wo_version": schema.Int64Attribute{Optional: true, Description: "Change to send secret_wo again."},
			"has_secret":        schema.BoolAttribute{Computed: true, PlanModifiers: []planmodifier.Bool{boolplanmodifier.UseStateForUnknown()}},
		},
	}
}

func (r *notificationWebhookResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	r.api = configured(req.ProviderData, &resp.Diagnostics)
}

func (r *notificationWebhookResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("name"), req, resp)
}

func (m *notificationWebhookModel) apply(out *notificationWebhookOut) {
	m.ID, m.Name, m.URL = types.StringValue(out.ID), types.StringValue(out.Name), types.StringValue(out.URL)
	m.Events, m.Enabled, m.HasSecret = setValue(out.Events), types.BoolValue(out.Enabled), types.BoolValue(out.HasSecret)
	m.SecretWO = types.StringNull()
}

// put saves the webhook. With sendSecret, the configured secret is sent (or removed when none is configured).
func (r *notificationWebhookResource) put(ctx context.Context, current string, m, config *notificationWebhookModel, sendSecret bool, d *diagSink) {
	body := map[string]any{"name": m.Name.ValueString(), "url": m.URL.ValueString(),
		"events": stringSet(ctx, m.Events, d.d), "enabled": m.Enabled.ValueBool()}
	if sendSecret {
		if !config.SecretWO.IsNull() && config.SecretWO.ValueString() != "" {
			body["secret"] = config.SecretWO.ValueString()
		} else {
			body["remove_secret"] = true
		}
	}
	if d.d.HasError() {
		return
	}
	var out notificationWebhookOut
	if err := r.api.Do(ctx, "PUT", "/v1/admin/webhooks/"+url.PathEscape(current), body, &out); err != nil {
		apiError(d.d, "saving notification webhook", err)
		return
	}
	m.apply(&out)
}

func (r *notificationWebhookResource) find(ctx context.Context, id, name string) (*notificationWebhookOut, error) {
	var listed struct {
		Webhooks []notificationWebhookOut `json:"webhooks"`
	}
	if err := r.api.Do(ctx, "GET", "/v1/admin/webhooks", nil, &listed); err != nil {
		return nil, err
	}
	for i := range listed.Webhooks {
		if (id != "" && listed.Webhooks[i].ID == id) || (id == "" && listed.Webhooks[i].Name == name) {
			return &listed.Webhooks[i], nil
		}
	}
	return nil, &client.APIError{Status: 404, Detail: "notification webhook not found"}
}

func (r *notificationWebhookResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan, config notificationWebhookModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.put(ctx, plan.Name.ValueString(), &plan, &config, !config.SecretWO.IsNull(), &diagSink{&resp.Diagnostics})
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *notificationWebhookResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state notificationWebhookModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	id := ""
	if !state.ID.IsNull() && !state.ID.IsUnknown() {
		id = state.ID.ValueString()
	}
	out, err := r.find(ctx, id, state.Name.ValueString())
	if err != nil {
		if !gone(ctx, err, resp) {
			apiError(&resp.Diagnostics, "reading notification webhook", err)
		}
		return
	}
	state.apply(out)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *notificationWebhookResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state, config notificationWebhookModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}
	send := !plan.SecretWOVersion.Equal(state.SecretWOVersion)
	r.put(ctx, state.Name.ValueString(), &plan, &config, send, &diagSink{&resp.Diagnostics}) // a new name renames it
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *notificationWebhookResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state notificationWebhookModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if err := r.api.Do(ctx, "DELETE", "/v1/admin/webhooks/"+url.PathEscape(state.Name.ValueString()), nil, nil); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "deleting notification webhook", err)
	}
}
