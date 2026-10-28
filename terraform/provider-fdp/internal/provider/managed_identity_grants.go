package provider

import (
	"context"
	"fmt"
	"net/url"
	"time"

	"github.com/floren/terraform-provider-fdp/internal/client"
)

// Only assignments declared here are owned; unrelated grants are never replaced.
func (r *workloadResource) reconcileIdentityGrants(ctx context.Context, principal string, old, wanted map[string]string) error {
	if len(old)+len(wanted) == 0 {
		return nil
	}
	if principal == "" {
		return fmt.Errorf("the workload has no managed identity")
	}
	// Install replacement access before withdrawing old access.
	for resource, role := range wanted {
		if err := r.api.Do(ctx, "PUT", "/v1/rbac/resources/"+url.PathEscape(resource)+"/assignments", map[string]any{"principal_id": principal, "role": role}, nil); err != nil {
			return err
		}
	}
	for resource := range old {
		if _, keep := wanted[resource]; keep {
			continue
		}
		if err := r.api.Do(ctx, "DELETE", "/v1/rbac/resources/"+url.PathEscape(resource)+"/assignments/services/"+url.PathEscape(principal), nil, nil); err != nil && !client.IsNotFound(err) {
			return err
		}
	}
	return nil
}

func (r *workloadResource) readIdentityGrants(ctx context.Context, principal string, declared map[string]string) (map[string]string, error) {
	actual := map[string]string{}
	for resource := range declared {
		var assignments []assignmentOut
		if err := r.api.Do(ctx, "GET", "/v1/rbac/resources/"+url.PathEscape(resource)+"/assignments", nil, &assignments); err != nil {
			if client.IsNotFound(err) {
				continue
			}
			return nil, err
		}
		for _, a := range assignments {
			if !a.Inherited && a.PrincipalID != nil && *a.PrincipalID == principal {
				actual[resource] = a.Role
			}
		}
	}
	return actual, nil
}

func (r *workloadResource) waitWithIdentityGrants(ctx context.Context, id string, timeout time.Duration, granted bool) (*workloadOut, error) {
	start := time.Now()
	out, err := r.wait(ctx, id, timeout)
	// Initial registration starts deployment concurrently. If it reached the data
	// check before its grants were saved, retry once through the normal sync API.
	// Never retry unrelated application failures or bypass the server's checks.
	if err != nil && granted && out != nil && out.Status == "failed" && len(out.AccessIssue) > 0 && time.Since(start) < timeout {
		if syncErr := r.api.Do(ctx, "POST", "/v1/workloads/"+id+"/sync", nil, nil); syncErr != nil {
			return out, syncErr
		}
		// Sync schedules asynchronously; wait until the previous failure is cleared.
		for time.Since(start) < timeout {
			if err := r.api.Do(ctx, "GET", "/v1/workloads/"+id, nil, out); err != nil {
				return out, err
			}
			if out.Status != "failed" {
				return r.wait(ctx, id, timeout-time.Since(start))
			}
			select {
			case <-ctx.Done():
				return out, ctx.Err()
			case <-time.After(time.Second):
			}
		}
	}
	return out, err
}
