// Floren Data Platform provider for OpenTofu (and Terraform-compatible tools): declarative layers, storage, IAM,
// compute and workloads through the same validated API as the desktop app.
package main

import (
	"context"
	"flag"
	"log"

	"github.com/floren/terraform-provider-fdp/internal/provider"
	"github.com/hashicorp/terraform-plugin-framework/providerserver"
)

var version = "dev"

func main() {
	var debug bool
	flag.BoolVar(&debug, "debug", false, "run with support for debuggers")
	flag.Parse()
	err := providerserver.Serve(context.Background(), provider.New(version), providerserver.ServeOpts{
		Address: "registry.opentofu.org/floren/fdp",
		Debug:   debug,
	})
	if err != nil {
		log.Fatal(err)
	}
}
