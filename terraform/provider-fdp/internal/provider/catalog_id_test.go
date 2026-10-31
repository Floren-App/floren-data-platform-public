package provider

import "testing"

// The platform derives catalog IDs from the resource (and for tables the object path); Terraform must match.
func TestCatalogIDsMatchThePlatform(t *testing.T) {
	if got := catalogObjectID("abc123def456", "sales"); got != "8f4367d53522e649fb5f07c96f655b63" {
		t.Fatalf("table catalog ID: %s", got)
	}
	if got := catalogObjectID("abc123def456", ""); got != "8d3ae833bc6c44c995ff5c93cf986fa4" {
		t.Fatalf("resource catalog ID: %s", got)
	}
}
