package provider

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Floren-App/terraform-provider-fdp/internal/client"
)

func TestKafkaWaitsDoNotMistakeAcceptanceForReadinessOrDeletion(t *testing.T) {
	for _, deletion := range []bool{false, true} {
		t.Run(map[bool]string{false: "readiness", true: "deletion"}[deletion], func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				phase := "Pending"
				body := map[string]any{"id": "topic", "config": map[string]any{"phase": phase}}
				if deletion {
					body = map[string]any{"phase": "Deleting"}
				}
				json.NewEncoder(w).Encode(body)
				cancel()
			}))
			defer server.Close()
			api, _ := client.New(client.Config{Endpoint: server.URL, Token: "test"})
			resource := kafkaResource{api: api}
			var err error
			if deletion {
				err = resource.waitDeleted(ctx, "topic")
			} else {
				_, err = resource.wait(ctx, "topic")
			}
			if err == nil {
				t.Fatal("Accepted is not complete: pending reconciliation must not succeed")
			}
		})
	}
}

func TestKafkaWaitsRequireActualReadyAndDeletedStatus(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/kafka/topics/topic/deletion" {
			json.NewEncoder(w).Encode(map[string]string{"phase": "Deleted"})
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"id": "topic", "config": map[string]string{"phase": "Ready"}})
	}))
	defer server.Close()
	api, _ := client.New(client.Config{Endpoint: server.URL, Token: "test"})
	resource := kafkaResource{api: api}
	if _, err := resource.wait(context.Background(), "topic"); err != nil {
		t.Fatal(err)
	}
	if err := resource.waitDeleted(context.Background(), "topic"); err != nil {
		t.Fatal(err)
	}
}
